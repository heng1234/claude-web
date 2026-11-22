"""Workbench HTTP API, workflow execution, and optional text model providers."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from claude_web.workbench import MODES, MAX_BYTES, ImportPreviewChanged, Workbench, canonical_cwd, identifier, scope_key

CONFIG_KINDS = {"profiles", "commands", "workflows"}
ACTIVE = {"queued", "running", "waiting_approval", "waiting_evidence", "waiting_permission"}


class WorkbenchRuntime:
    def __init__(self, server):
        self.server = server
        self.store = Workbench(server._DATA_DIR / "workbench")
        self.jobs = {}
        self.processes = {}
        self.approvals = {}
        self.provider_streams = {}
        self.provider_sessions = set()
        self.semaphore = asyncio.Semaphore(2)

    async def shutdown(self):
        for task in list(self.jobs.values()):
            task.cancel()
        if self.jobs:
            await asyncio.gather(*list(self.jobs.values()), return_exceptions=True)
        for stream in list(self.provider_streams.values()):
            await stream.aclose()

    def public_provider(self, record):
        return {k: v for k, v in record.items() if k != "key_ref"} | {"has_key": bool(record.get("key_ref"))}

    def save_provider(self, fields):
        key = identifier(fields.get("id") or uuid.uuid4().hex)
        existing = next((v for v in self.store.list("providers") if v["id"] == key), {})
        url = str(fields.get("base_url") or "").strip().rstrip("/")
        parsed = urlparse(url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("服务地址需为 HTTPS，本地模型可用 localhost HTTP")
        key_ref = existing.get("key_ref", "")
        credential = str(fields.get("api_key") or "")
        protocol = fields.get("protocol") or existing.get("protocol") or "openai"
        if protocol not in {"openai", "anthropic"}:
            raise ValueError("协议需为 openai 或 anthropic")
        record = {"id": key, "protocol": protocol, "name": str(fields.get("name") or "").strip()[:120], "base_url": url,
                  "model": str(fields.get("model") or "").strip()[:160], "key_ref": key_ref}
        if not record["name"] or not record["model"]:
            raise ValueError("请填写服务名称和模型 ID")
        if credential:
            record["key_ref"] = self.server._get_secret_store().store_secret(credential, "CCW provider: " + key)
        self.store.put("providers", key, record)
        if credential and existing.get("key_ref"):
            self.server._get_secret_store().delete_secret(existing["key_ref"])
        return self.public_provider(record)

    async def provider_completion(self, provider, model, prompt, max_tokens=128):
        """Short auxiliary requests must use the session's selected provider."""
        key = self.server._get_secret_store().resolve_secret(provider["key_ref"]) if provider.get("key_ref") else ""
        protocol = provider.get("protocol", "openai")
        if protocol == "anthropic":
            headers = {"anthropic-version": "2023-06-01"}
            if key:
                headers.update({"x-api-key": key, "Authorization": "Bearer " + key})
            endpoint = provider["base_url"] + ("/messages" if provider["base_url"].endswith("/v1") else "/v1/messages")
        else:
            headers = {"Authorization": "Bearer " + key} if key else {}
            endpoint = provider["base_url"] + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens, "stream": False}
        async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), follow_redirects=False) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("error"):
            raise ValueError("服务返回错误")
        if protocol == "anthropic":
            reply = "".join(str(part.get("text") or "") for part in data.get("content", []) if isinstance(part, dict) and part.get("type") == "text")
        else:
            reply = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        if not reply.strip():
            raise ValueError("未收到文本回答")
        return {"reply": reply, "usage": data.get("usage") or {}, "model": model, "protocol": protocol}

    def provider_binding(self, session_id):
        return next((row for row in self.store.list("session_providers") if row["id"] == session_id), None)

    def session_model(self, session_id):
        binding = self.provider_binding(session_id)
        if not binding:
            return None
        try:
            provider = self.store.get("providers", binding["provider_id"])
        except ValueError:
            provider = {}
        return {"service": provider.get("name") or binding["provider_id"], "model": binding.get("model") or "", "source": "binding"}

    async def session_completion(self, session_id, prompt, max_tokens=128):
        binding = self.provider_binding(session_id)
        if not binding:
            raise ValueError("会话未绑定模型服务")
        provider = self.store.get("providers", binding["provider_id"])
        return await self.provider_completion(provider, binding.get("model") or provider["model"], prompt, max_tokens)

    async def provider_chat(self, req, session_id, cwd, existing_events, provider_id):
        if req.workspace_mode != "chat":
            raise HTTPException(422, "兼容模型当前用于 Chat 文本会话；代码和浏览器工具使用 Claude 原生运行时")
        if req.images:
            raise HTTPException(422, "兼容模型当前仅支持文本，请移除图片")
        if session_id in self.provider_sessions:
            raise HTTPException(409, "此会话已有模型任务正在运行")
        provider = self.store.get("providers", provider_id)
        model = req.model or provider["model"]
        messages = []
        if req.system_prompt:
            messages.append({"role": "system", "content": req.system_prompt})
        for event in existing_events[-100:]:
            if event.get("type") == "user_input":
                messages.append({"role": "user", "content": str(event.get("full_text") or event.get("text") or "")})
            elif event.get("type") == "assistant":
                content = (event.get("message") or {}).get("content") or []
                text = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                if text:
                    messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": req.message})
        key = self.server._get_secret_store().resolve_secret(provider["key_ref"]) if provider["key_ref"] else ""
        self.server._stopped_sessions.discard(session_id)
        self.server.upsert_session(session_id, self.server.derive_title(req.display_message or req.message), cwd, "chat")
        self.store.put("session_providers", session_id, {"id": session_id, "provider_id": provider_id, "model": model})
        self.provider_sessions.add(session_id)

        async def generate():
            text, usage, success = "", {}, False
            finish_reason = None
            turn_id = uuid.uuid4().hex
            message_id = "compatible-" + turn_id
            assistant_saved = False
            encode = lambda value: "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"
            try:
                await self.server.async_append_event(session_id, {"type": "user_input", "text": req.display_message or req.message, "full_text": req.message, "ts": time.time()})
                yield encode({"type": "meta", "session_id": session_id, "turn_id": turn_id, "runtime": "openai_compatible", "provider": provider["name"], "session_model": {"service": provider["name"], "model": model, "source": "binding"}})
                yield encode({"type": "stream_event", "event": {"type": "message_start", "message": {"id": message_id, "role": "assistant"}}})
                yield encode({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}})
                async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False) as client:
                    async with client.stream("POST", provider["base_url"] + "/chat/completions", headers={"Authorization": "Bearer " + key} if key else {},
                                             json={"model": model, "messages": messages, "stream": True}) as response:
                        response.raise_for_status()
                        self.provider_streams[session_id] = response
                        async for line in response.aiter_lines():
                            if session_id in self.server._stopped_sessions:
                                break
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                success = finish_reason == "stop" or (finish_reason is None and bool(text))
                                break
                            frame = json.loads(data)
                            if frame.get("error"):
                                raise ValueError("模型服务返回流式错误")
                            usage = frame.get("usage") or usage
                            choices = frame.get("choices") or []
                            if not choices:
                                continue
                            delta = (choices[0].get("delta") or {}).get("content") or ""
                            if isinstance(delta, str) and delta:
                                text += delta
                                yield encode({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}}})
                            if choices[0].get("finish_reason"):
                                finish_reason = choices[0]["finish_reason"]
                                success = finish_reason == "stop"
                yield encode({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
                yield encode({"type": "stream_event", "event": {"type": "message_stop"}})
                if text:
                    event = {"type": "assistant", "uuid": message_id, "message": {"id": message_id, "role": "assistant", "content": [{"type": "text", "text": text}]}}
                    await self.server.async_append_event(session_id, event)
                    assistant_saved = True
                    yield encode(event)
                success = success and session_id not in self.server._stopped_sessions
                result = {"type": "result", "subtype": "success" if success else "error_during_execution", "is_error": not success,
                          "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}, "model": model}
                if not success:
                    result["result"] = ("模型输出达到长度限制，回答尚未完成" if finish_reason == "length" else "模型响应未正常结束，请检查服务返回或停止状态")
                await self.server.async_append_event(session_id, result)
                await self.server.async_record_usage(session_id, result)
                yield encode(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                success = False
                message = f"兼容模型请求失败（{type(exc).__name__}），请检查服务地址、模型和凭据"
                await self.server.async_append_event(session_id, {"type": "error", "message": message})
                yield encode({"type": "error", "message": message})
            finally:
                try:
                    if text and not assistant_saved:
                        await self.server.async_append_event(session_id, {"type": "assistant", "uuid": message_id, "message": {"id": message_id, "role": "assistant", "content": [{"type": "text", "text": text}]}})
                finally:
                    self.provider_streams.pop(session_id, None)
                    self.provider_sessions.discard(session_id)
            yield encode({"type": "done", "success": success, "turn_id": turn_id})
        return StreamingResponse(generate(), media_type="text/event-stream")

    async def collect_prompt(self, task, value):
        profile = task.get("profile") or {}
        req = self.server.ChatRequest(
            message=value, session_id=task["session_id"], cwd=task["cwd"],
            workspace_mode=MODES[profile.get("mode", "code")]["workspace_mode"],
            work_mode=profile.get("mode", "code"), model=profile.get("model") or None,
            effort=profile.get("effort") or "high", permission_mode=profile.get("permission_mode", "default"),
            system_prompt=profile.get("system_prompt") or None, provider_id=profile.get("provider_id") or None,
            extension_ids=profile.get("extension_ids"),
        )
        req._workbench_literal_input = True
        response = await self.server._chat_response(req)
        text, error, usage, done = "", "", {}, False
        changed_files = []
        pending = ""
        async for chunk in response.body_iterator:
            pending += chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            while "\n\n" in pending:
                frame, pending = pending.split("\n\n", 1)
                line = next((line[6:] for line in frame.splitlines() if line.startswith("data: ")), "")
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") == "permission_request":
                    self.save_task(task, status="waiting_permission", pending_permission=obj.get("tool_name") or "工具请求")
                elif task.get("status") == "waiting_permission":
                    self.save_task(task, status="running", pending_permission="")
                if obj.get("type") == "assistant":
                    text += self.server._agent_sdk_assistant_text(obj) + "\n"
                if obj.get("type") in {"error", "permission_error"}:
                    error = str(obj.get("message") or "Agent 执行失败")
                if obj.get("type") == "result":
                    usage = obj.get("usage") or usage
                    changed_files = obj.get("changed_files") or changed_files
                    if obj.get("is_error"):
                        error = error or str(obj.get("result") or "Agent 执行失败")
                if obj.get("type") == "done":
                    done = obj.get("success") is not False
        if error or not done:
            raise ValueError(error or "任务未返回成功结束状态")
        return {"text": text[-30000:], "usage": usage, "changed_files": changed_files}

    def save_task(self, task, **updates):
        task.update(updates, updated_at=time.time())
        self.store.put("tasks", task["id"], task)

    def launch(self, task):
        task_id = task["id"]
        if task_id in self.jobs:
            raise ValueError("任务仍在运行")
        if len(self.jobs) >= 100:
            raise ValueError("任务队列已满")
        self.save_task(task, status="queued", error="")
        job = asyncio.create_task(self.run(task))
        self.jobs[task_id] = job
        job.add_done_callback(lambda _job: self.jobs.pop(task_id, None))

    async def run(self, task):
        task_id = task["id"]
        try:
            async with self.semaphore:
                self.save_task(task, status="running", error="")
                steps = task["workflow"]["steps"]
                for index in range(task.get("next_step", 0), len(steps)):
                    task["results"] = [result for result in task["results"] if result["step"] < index]
                    step = steps[index]
                    self.save_task(task, next_step=index, current_step=step["name"])
                    started = time.time()
                    if step["type"] == "command":
                        approval = asyncio.Event()
                        self.approvals[task_id] = approval
                        self.save_task(task, status="waiting_approval", pending_command=step["value"])
                        await approval.wait()
                        self.approvals.pop(task_id, None)
                        self.save_task(task, status="running", pending_command="")
                        result = await self.server._run_validation_command(step["value"], task["cwd"], step["timeout"], on_process=lambda p: self.processes.__setitem__(task_id, p) if p is not None else None, start_new_session=True)
                        self.processes.pop(task_id, None)
                        if result.get("returncode") != 0:
                            task["results"].append({"step": index, "name": step["name"], "type": "command", "result": result})
                            raise ValueError("命令执行失败：" + str(result.get("stderr") or result.get("stdout"))[-4000:])
                    elif step["type"] == "prompt":
                        result = await asyncio.wait_for(self.collect_prompt(task, step["value"].replace("{{input}}", task.get("input", ""))), timeout=step.get("timeout", 120))
                    elif step["type"] == "browser_validation":
                        # Reuse the existing evidence registry. An external
                        # executor must submit actual evidence before proceeding.
                        owner, separator, recipe_id = step["value"].partition("/")
                        if not separator:
                            raise ValueError("浏览器步骤需填写拥有配方的 session_id/recipe_id")
                        registry = self.server._get_code_browser_validation_registry()
                        recipe = registry.get_recipe(owner, recipe_id)
                        if canonical_cwd(recipe["canonical_cwd"]) != task["cwd"]:
                            raise ValueError("浏览器验收配方不属于当前项目")
                        run = registry.create_run(owner, recipe_id)
                        self.save_task(task, status="waiting_evidence", browser_run={"session_id": owner, "run_id": run["id"]})
                        deadline = time.monotonic() + step.get("timeout", 120)
                        while run["status"] in {"queued", "running"}:
                            if time.monotonic() >= deadline:
                                raise ValueError("等待浏览器验收证据超时；请在原会话的验收面板中查看")
                            await asyncio.sleep(0.5)
                            run = registry.get_run(owner, run["id"])
                        if run["status"] != "passed" or not run.get("evidence"):
                            raise ValueError("浏览器验收未通过：" + str(run.get("reason") or run["status"]))
                        result = {"browser_run": run}
                        self.save_task(task, status="running")
                    else:
                        result = {"text": self.report(task)}
                    task["results"].append({"step": index, "name": step["name"], "type": step["type"], "result": result, "duration_ms": int((time.time() - started) * 1000)})
                    self.save_task(task, next_step=index + 1)
                self.save_task(task, status="completed", current_step="", report=self.report(task))
        except asyncio.CancelledError:
            process = self.processes.pop(task_id, None)
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            await self.server.stop_chat(task["session_id"])
            self.save_task(task, status="cancelled", error="任务已停止")
        except Exception as exc:
            try:
                await self.server.stop_chat(task["session_id"])
            except Exception:
                pass
            self.save_task(task, status="failed", error=(str(exc) or type(exc).__name__)[:4000])
        finally:
            self.approvals.pop(task_id, None)

    def report(self, task):
        parts = ["# " + task["name"], "", "项目：" + task["cwd"], ""]
        for row in task["results"]:
            if row.get("type") == "report":
                continue
            result = row["result"]
            parts.extend(["## " + row["name"], "", result.get("text") or result.get("stdout") or json.dumps(result, ensure_ascii=False), ""])
        return "\n".join(parts)

    def task_overview(self):
        tasks = sorted(self.store.list("tasks"), key=lambda t: t["updated_at"], reverse=True)[:200]
        summaries = []
        for task in tasks:
            usage = {"input_tokens": 0, "output_tokens": 0}
            changed_files = []
            for row in task.get("results", []):
                result = row.get("result") or {}
                for key in usage:
                    usage[key] += int((result.get("usage") or {}).get(key) or 0)
                changed_files.extend(result.get("changed_files") or [])
            summaries.append({k: v for k, v in task.items() if k not in {"results", "report", "input", "profile"}} | {"usage": usage, "changed_file_count": len(changed_files)})
        sessions = self.server._agent_sdk_running_sessions | self.provider_sessions | set(self.server._running_processes)
        with self.server.db_connect() as conn:
            live = [{"session_id": sid, "name": (row["title"] if row else sid), "cwd": (row["cwd"] if row else ""), "status": "running", "kind": "session"}
                    for sid in sessions if (row := conn.execute("SELECT title,cwd FROM sessions WHERE id=?", (sid,)).fetchone()) is not None]
        loops = [self.server._agent_loop_job_summary(job) for job in self.server._agent_loop_jobs.values()]
        return {"tasks": summaries, "sessions": live, "agent_loops": loops}


def create_router(server):
    runtime = WorkbenchRuntime(server)
    store = runtime.store
    router = APIRouter(prefix="/api/workbench", tags=["workbench"])

    async def guard(request: Request):
        server._require_local_admin(request)
        # A local/private client address is not proof of browser intent. In
        # particular a hostile page can POST a multipart ZIP to localhost.
        if (request.headers.get("sec-fetch-site") or "").lower() == "cross-site":
            raise HTTPException(403, "same-origin request required")
        origin = request.headers.get("origin")
        if origin:
            source, target = urlparse(origin), request.url
            try:
                source_port = source.port or (443 if source.scheme == "https" else 80)
                target_port = target.port or (443 if target.scheme == "https" else 80)
            except ValueError as exc:
                raise HTTPException(403, "same-origin request required") from exc
            if source.scheme != target.scheme or source.hostname != target.hostname or source_port != target_port:
                raise HTTPException(403, "same-origin request required")

    # All endpoints, including reads, expose local filesystem/process capabilities.
    from fastapi import Depends
    router.dependencies.append(Depends(guard))

    def checked(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/catalog")
    async def catalog(cwd: str = "", session_id: str = ""):
        effective = checked(store.effective, cwd, session_id)
        return {"modes": MODES, "scope_context": {"project": str(Path(cwd).expanduser().resolve()) if cwd else "", "session": session_id},
                "extensions": store.list("extensions"), "bindings": store.list("bindings"),
                "effective": effective, **{kind: store.list(kind) for kind in CONFIG_KINDS},
                "workflows": checked(store.available_workflows, cwd, session_id),
                "providers": [runtime.public_provider(p) for p in store.list("providers")], "routes": store.list("routes")}

    @router.get("/commands")
    async def command_catalog(cwd: str = "", session_id: str = ""):
        rows = [{"id": c["id"], "name": c["name"], "mode": c["mode"], "native": False} for c in store.list("commands")]
        for extension in checked(store.effective, cwd, session_id):
            if extension["binding"]["components"].get("commands"):
                rows.extend({"id": extension["id"] + ":" + Path(path).stem, "name": extension["name"] + " / " + Path(path).stem, "mode": "code", "native": True}
                            for path in extension["inventory"]["commands"])
        return {"commands": rows}

    @router.get("/marketplace")
    async def marketplace():
        return {"plugins": store.marketplace(), "sources": store.list("sources")}

    @router.put("/marketplace/sources")
    async def source_save(payload: dict):
        source_id = checked(identifier, payload.get("id") or uuid.uuid4().hex)
        url = str(payload.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise HTTPException(400, "目录地址需为 HTTPS JSON，不含嵌入凭据")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024:
                            raise ValueError("扩展目录超过 1 MB")
            blob = json.loads(data)
            entries = blob.get("plugins")
            if not isinstance(entries, list) or len(entries) > 200:
                raise ValueError("目录需包含 plugins 列表，最多 200 项")
            plugins = []
            for entry in entries:
                plugin_id = identifier(entry.get("id") or entry.get("name"))
                source = entry.get("source")
                if isinstance(source, dict):
                    if source.get("source") == "github":
                        repo = str(source.get("repo") or "")
                        if not __import__("re").fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                            raise ValueError("无效的 GitHub 仓库")
                        source = "https://github.com/" + repo + ".git"
                    else:
                        source = source.get("url")
                source = str(source or "")
                source_url = urlparse(source)
                if source_url.scheme != "https" or not source_url.hostname or source_url.username or source_url.password:
                    raise ValueError("每项插件需指定独立的 HTTPS Git 来源")
                plugins.append({"id": plugin_id, "name": str(entry.get("display_name") or entry.get("name") or plugin_id)[:120],
                                "description": str(entry.get("description") or "")[:2000], "version": str(entry.get("version") or ""),
                                "source": source, "source_type": "git", "subdir": str(entry.get("subdir") or "")})
            return store.put("sources", source_id, {"id": source_id, "name": str(payload.get("name") or blob.get("name") or source_id), "url": url, "plugins": plugins})
        except Exception as exc:
            raise HTTPException(400, "目录加载失败：" + str(exc)) from exc

    @router.delete("/marketplace/sources/{source_id}")
    async def source_delete(source_id: str):
        store.delete("sources", source_id)
        return {"ok": True}

    @router.put("/marketplace/ratings/{plugin_id}")
    async def rating(plugin_id: str, payload: dict):
        stars = payload.get("stars")
        if type(stars) is not int or not 0 <= stars <= 5:
            raise HTTPException(400, "本机评分需为 0–5")
        return store.put("ratings", checked(identifier, plugin_id), {"id": plugin_id, "stars": stars})

    @router.get("/selection/{session_id}")
    async def session_selection(session_id: str):
        mode = next((x for x in store.list("session_modes") if x["id"] == session_id), {})
        provider = next((x for x in store.list("session_providers") if x["id"] == session_id), {})
        return {**mode, "provider_id": provider.get("provider_id", ""), "session_model": runtime.session_model(session_id)}

    @router.post("/extensions/preview")
    async def preview(payload: dict):
        try:
            return await asyncio.to_thread(store.preview, str(payload.get("source") or ""), payload.get("source_type", "local"), subdir=str(payload.get("subdir") or ""))
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/extensions/upload")
    async def upload(file: UploadFile = File(...)):
        data = await file.read(MAX_BYTES + 1)
        try:
            return await asyncio.to_thread(store.preview, "", "zip", data=data)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/extensions/install")
    async def install(payload: dict):
        return checked(store.install, payload.get("preview_id"))

    @router.post("/extensions/{plugin_id}/binding")
    async def binding(plugin_id: str, payload: dict):
        return checked(store.bind, plugin_id, payload.get("enabled") is True, payload.get("scope", "global"), payload.get("cwd", ""), payload.get("session_id", ""), payload.get("components"))

    @router.post("/extensions/{plugin_id}/update")
    async def update(plugin_id: str):
        record = checked(store.get, "extensions", plugin_id)
        if record["source_type"] == "zip" and not record["source"]:
            raise HTTPException(400, "上传安装的 ZIP 请重新上传新版")
        try:
            result = await asyncio.to_thread(store.preview, record["source"], record["source_type"], subdir=record.get("subdir", ""))
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        if result["id"] != plugin_id:
            raise HTTPException(400, "更新包标识与原扩展不一致")
        return result

    @router.post("/extensions/{plugin_id}/rollback")
    async def rollback(plugin_id: str):
        return checked(store.rollback, plugin_id)

    @router.delete("/extensions/{plugin_id}")
    async def uninstall(plugin_id: str):
        checked(store.uninstall, plugin_id)
        return {"ok": True}

    @router.put("/configs/{kind}")
    async def save_config(kind: str, payload: dict):
        if kind not in CONFIG_KINDS:
            raise HTTPException(404, "未知配置类型")
        if payload.get("provider_id"):
            provider = checked(store.get, "providers", payload["provider_id"])
            if provider.get("protocol", "openai") == "openai" and payload.get("mode", "code") != "chat":
                raise HTTPException(400, "OpenAI 兼容服务仅用于 Chat，请选 Chat 或 Anthropic 兼容服务")
        return checked(store.save_config, kind, payload)

    @router.delete("/configs/{kind}/{record_id}")
    async def delete_config(kind: str, record_id: str):
        if kind not in CONFIG_KINDS:
            raise HTTPException(404, "未知配置类型")
        store.delete(kind, record_id)
        return {"ok": True}

    @router.put("/providers")
    async def provider_save(payload: dict):
        return checked(runtime.save_provider, payload)

    @router.post("/providers/{provider_id}/test")
    async def provider_test(provider_id: str, payload: dict = None):
        provider = checked(store.get, "providers", provider_id)
        model = str((payload or {}).get("model") or provider["model"]).strip()[:160]
        try:
            result = await runtime.provider_completion(provider, model, "Reply with OK.", max_tokens=16)
            return {"ok": True, **result, "reply": result["reply"][:200]}
        except httpx.HTTPStatusError as exc:
            raise HTTPException(502, f"模型推理测试失败：服务返回 HTTP {exc.response.status_code}，请检查模型 ID、Key 和服务地址") from exc
        except Exception as exc:
            raise HTTPException(502, "模型推理测试失败：" + type(exc).__name__) from exc

    @router.delete("/providers/{provider_id}")
    async def provider_delete(provider_id: str):
        provider = checked(store.get, "providers", provider_id)
        if any(p.get("provider_id") == provider_id for p in store.list("session_providers")):
            raise HTTPException(409, "仍有会话绑定此服务；保留服务可继续历史会话")
        if any(p.get("provider_id") == provider_id for p in store.list("profiles") + store.list("commands") + store.list("routes")):
            raise HTTPException(409, "请先移除配置档、快捷命令和路由对此服务的引用")
        if provider.get("key_ref"):
            server._get_secret_store().delete_secret(provider["key_ref"])
        store.delete("providers", provider_id)
        return {"ok": True}

    @router.put("/routes/{mode}")
    async def route_save(mode: str, payload: dict):
        if mode not in MODES:
            raise HTTPException(400, "未知工作模式")
        provider_id = str(payload.get("provider_id") or "")
        if provider_id:
            provider = checked(store.get, "providers", provider_id)
            if mode != "chat" and provider.get("protocol", "openai") == "openai":
                raise HTTPException(400, "OpenAI 兼容接口仅支持 Chat；代码工具可选择 Anthropic 兼容服务")
        return store.put("routes", mode, {"id": mode, "provider_id": provider_id, "model": str(payload.get("model") or "")[:160]})

    @router.get("/export")
    async def export():
        return Response(json.dumps(store.export_bundle(), ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="ccw-desk-workspace.json"'})

    @router.get("/export.zip")
    async def export_zip():
        return Response(store.export_zip(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="ccw-desk-workspace.zip"'})

    def process_import(payload, preview, revision):
        try:
            result = store.import_bundle(payload, preview=preview, revision=revision)
        except ImportPreviewChanged as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return result if preview else {"imported": result}

    @router.post("/import-zip")
    async def import_zip(file: UploadFile = File(...), preview: bool = False, x_workbench_import_revision: str = Header(default=None)):
        import io
        import zipfile
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(400, "配置包超过 32 MB")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.getinfo("ccw-workspace.json")
                if info.file_size > 2 * 1024 * 1024:
                    raise ValueError("配置内容超过 2 MB")
                payload = json.loads(archive.read(info))
        except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            raise HTTPException(400, "配置包导入失败：" + str(exc)) from exc
        return process_import(payload, preview, x_workbench_import_revision)

    @router.post("/import")
    async def import_bundle(payload: dict, preview: bool = False, x_workbench_import_revision: str = Header(default=None)):
        return process_import(payload, preview, x_workbench_import_revision)

    @router.get("/tasks")
    async def tasks():
        return runtime.task_overview()

    @router.get("/tasks/{task_id}")
    async def task_detail(task_id: str):
        return checked(store.get, "tasks", task_id)

    @router.post("/tasks")
    async def start_task(payload: dict):
        workflow_id = str(payload.get("workflow_id") or "")
        workflow = next((w for w in checked(store.available_workflows, str(payload.get("cwd") or ""), str(payload.get("session_id") or "")) if w["id"] == workflow_id), None)
        if workflow is None:
            raise HTTPException(400, "工作流不存在或所属扩展未启用")
        profile_id = str(payload.get("profile_id") or workflow.get("profile_id") or "")
        profile = checked(store.get, "profiles", profile_id) if profile_id else {"mode": "code", "permission_mode": "default"}
        cwd = checked(canonical_cwd, payload.get("cwd") or profile.get("cwd") or "")
        task = {"id": uuid.uuid4().hex, "name": workflow["name"], "workflow": workflow, "profile": profile, "cwd": cwd,
                "session_id": str(uuid.uuid4()), "input": str(payload.get("input") or "")[:40000], "next_step": 0,
                "results": [], "status": "queued", "created_at": time.time(), "updated_at": time.time(), "error": ""}
        checked(runtime.launch, task)
        return task

    @router.post("/tasks/{task_id}/{action}")
    async def task_action(task_id: str, action: str):
        task = checked(store.get, "tasks", task_id)
        if action == "approve":
            event = runtime.approvals.get(task_id)
            if event is None or task["status"] != "waiting_approval":
                raise HTTPException(409, "任务当前没有待审批命令")
            event.set()
        elif action == "cancel":
            if task["status"] not in ACTIVE:
                raise HTTPException(409, "此任务已结束，无需停止")
            if task_id in runtime.jobs:
                job = runtime.jobs[task_id]
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                if store.get("tasks", task_id)["status"] in ACTIVE:
                    runtime.save_task(task, status="cancelled", error="任务已停止")
            else:
                runtime.save_task(task, status="cancelled")
        elif action == "resume":
            if task["status"] not in {"failed", "cancelled", "interrupted"}:
                raise HTTPException(409, "只能继续失败或已中断的任务")
            checked(runtime.launch, task)
        else:
            raise HTTPException(404, "未知任务操作")
        return checked(store.get, "tasks", task_id)

    @router.get("/tasks/{task_id}/report")
    async def task_report(task_id: str):
        task = checked(store.get, "tasks", task_id)
        return Response(runtime.report(task), media_type="text/markdown", headers={"Content-Disposition": 'attachment; filename="ccw-task-report.md"'})

    return router, runtime
