"""Persistent extension packages and workspace configuration for CCW Desk.

Packages are immutable snapshots. Installation only stages/copies files; executable
components become available only after an explicit scope/component selection.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
from urllib.parse import urlparse

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
COMPONENTS = ("skills", "agents", "commands", "mcp", "hooks", "workflows")
MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 2000
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".DS_Store"}
MODES = {
    "chat": {"name": "Chat", "workspace_mode": "chat", "readonly": True, "browser": False},
    "code": {"name": "Code", "workspace_mode": "code", "readonly": False, "browser": False},
    "research": {"name": "Research", "workspace_mode": "code", "readonly": True, "browser": True},
    "browser": {"name": "Browser", "workspace_mode": "code", "readonly": True, "browser": True},
    "review": {"name": "Review", "workspace_mode": "code", "readonly": True, "browser": False},
    "automation": {"name": "Automation", "workspace_mode": "code", "readonly": False, "browser": False},
}
WRITE_TOOLS = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Task"]
MODE_PROMPTS = {
    "research": "Research the user's question using available sources. Cite evidence, distinguish facts from inference, and provide a structured report. Do not modify project files.",
    "review": "Review the project or diff for actionable defects. Prioritize correctness and regressions, cite file paths and lines, and describe test gaps. Do not modify files.",
    "browser": "Inspect and validate the requested website using browser tools. Report observed behavior, screenshots and failures. Do not modify project files or perform external submissions without an explicit user request.",
}


def identifier(value):
    value = str(value or "")
    if not ID_RE.fullmatch(value):
        raise ValueError("标识只支持小写字母、数字、连字符和下划线，最多 80 字符")
    return value


def canonical_cwd(value):
    path = Path(str(value)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("项目目录不存在")
    return str(path)


def scope_key(scope, cwd="", session_id=""):
    if scope == "global":
        return "global"
    if scope == "project":
        return "project:" + canonical_cwd(cwd)
    if scope == "session" and session_id and len(session_id) <= 160:
        return "session:" + session_id
    raise ValueError("请选择全局、有效项目或已有会话")


def clean_export(value):
    """Strip credential fields from declarative team configuration exports."""
    if isinstance(value, dict):
        return {k: clean_export(v) for k, v in value.items()
                if not re.search(r"secret|token|password|authorization|api.?key|headers|env$", k, re.I)}
    if isinstance(value, list):
        return [clean_export(item) for item in value]
    if isinstance(value, str) and (value.startswith("cwsecret://") or value.startswith("sk-")):
        return ""
    return value


class ImportPreviewChanged(ValueError):
    pass


class Workbench:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.packages = self.root / "packages"
        self.staging = self.root / "staging"
        self.packages.mkdir(exist_ok=True)
        self.staging.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db_path = self.root / "workbench.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id))")

    def recover_tasks(self):
        """Only the serving process calls this at startup, not read-only clients."""
        with self.lock, self.connect() as conn:
            for row in conn.execute("SELECT id,value FROM records WHERE kind='tasks'").fetchall():
                task = json.loads(row["value"])
                if task.get("status") in {"queued", "running", "waiting_approval", "waiting_evidence", "waiting_permission"}:
                    task.update(status="interrupted", error="服务重启，任务已中断；可从失败步骤继续", updated_at=time.time())
                    conn.execute("UPDATE records SET value=? WHERE kind='tasks' AND id=?", (json.dumps(task), row["id"]))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def list(self, kind):
        with self.connect() as conn:
            return [json.loads(row["value"]) for row in conn.execute("SELECT value FROM records WHERE kind=? ORDER BY id", (kind,))]

    def get(self, kind, key):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise ValueError("记录不存在")
        return json.loads(row["value"])

    def put(self, kind, key, value):
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value", (kind, key, json.dumps(value, ensure_ascii=False)))
        return value

    def delete(self, kind, key):
        with self.lock, self.connect() as conn:
            conn.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    def _copy(self, source, target):
        count, size = 0, 0
        for current, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            relative = Path(current).relative_to(source)
            for name in dirs + files:
                if (Path(current) / name).is_symlink():
                    raise ValueError("扩展不能包含符号链接")
            for name in files:
                if name in SKIP_DIRS:
                    continue
                path = Path(current) / name
                if not path.is_file():
                    raise ValueError("扩展只能包含普通文件")
                size += path.stat().st_size
                count += 1
                if size > MAX_BYTES or count > MAX_FILES:
                    raise ValueError("扩展不能超过 32 MB 或 2000 个文件")
                dest = target / relative / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        return count, size

    def _extract(self, data, target):
        if len(data) > MAX_BYTES:
            raise ValueError("ZIP 超过 32 MB")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_BYTES:
                raise ValueError("ZIP 解压后超过限制")
            for item in entries:
                path = PurePosixPath(item.filename)
                mode = item.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                        or not path.parts or ":" in path.parts[0] or stat.S_ISLNK(mode)):
                    raise ValueError("ZIP 包含不安全路径或符号链接")
                if set(path.parts) & SKIP_DIRS:
                    continue
                dest = target.joinpath(*path.parts)
                if item.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(archive.read(item))
                    dest.chmod(0o644 | (mode & 0o111))
        # GitHub ZIPs usually wrap the package in a repository directory.
        children = list(target.iterdir())
        if len(children) == 1 and children[0].is_dir() and not (target / "ccw-plugin.json").exists():
            child = children[0]
            for entry in list(child.iterdir()):
                entry.rename(target / entry.name)
            child.rmdir()

    def _manifest(self, path):
        native = path / ".claude-plugin/plugin.json"
        ccw = path / "ccw-plugin.json"
        manifest = json.loads((ccw if ccw.is_file() else native).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("插件清单必须为 JSON 对象")
        plugin_id = identifier(manifest.get("id") or manifest.get("name"))
        name = str(manifest.get("display_name") or manifest.get("name") or plugin_id)[:120]
        version = str(manifest.get("version") or "0.0.0")[:80]
        inventory = {}
        for kind in ("skills", "agents", "commands", "workflows"):
            directory = path / kind
            files = sorted(directory.rglob("*")) if directory.is_dir() else []
            inventory[kind] = [str(p.relative_to(path)) for p in files if p.is_file() and
                               (p.name == "SKILL.md" if kind == "skills" else p.suffix in {".md", ".json"})]
        # Materialize native overrides at the standard locations for granular selection.
        for field, filename in (("mcpServers", ".mcp.json"), ("hooks", "hooks/hooks.json")):
            config = manifest.get(field)
            if isinstance(config, str):
                relative = PurePosixPath(config.removeprefix("./"))
                if relative.is_absolute() or ".." in relative.parts or "\\" in config:
                    raise ValueError("清单配置路径必须位于扩展目录内")
                config = json.loads((path / str(relative)).read_text())
            if isinstance(config, dict):
                dest = path / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            elif config is not None:
                raise ValueError(f"{field} 必须为配置对象或包内路径")
        inventory["mcp"] = []
        if (path / ".mcp.json").is_file():
            config = json.loads((path / ".mcp.json").read_text())
            if not isinstance(config, dict):
                raise ValueError("MCP 配置必须为对象")
            servers = config.get("mcpServers", config)
            if not isinstance(servers, dict) or any(not isinstance(value, dict) for value in servers.values()):
                raise ValueError("MCP 服务配置必须为对象")
            inventory["mcp"] = list(servers.keys())
        inventory["hooks"] = ["hooks/hooks.json"] if (path / "hooks/hooks.json").is_file() else []
        for kind in ("skills", "agents", "commands"):
            override = manifest.get(kind)
            if isinstance(override, list):
                for item in override:
                    identifier(item)
                continue
            if override is not None and override != f"./{kind}":
                raise ValueError(f"{kind} 请使用标准 {kind}/ 目录，便于独立启停")
        for filename in inventory["workflows"]:
            self.validate_config("workflows", json.loads((path / filename).read_text()))
        return {"id": plugin_id, "name": name, "version": version,
                "description": str(manifest.get("description") or "")[:2000], "inventory": inventory}

    def preview(self, source="", source_type="local", data=None, subdir=""):
        token = uuid.uuid4().hex
        stage = self.staging / token
        stage.mkdir()
        package = stage / "package"
        package.mkdir()
        try:
            if source_type == "zip":
                if data is None:
                    archive_path = Path(source).expanduser()
                    if archive_path.stat().st_size > MAX_BYTES:
                        raise ValueError("ZIP 超过 32 MB")
                    data = archive_path.read_bytes()
                self._extract(data, package)
            elif source_type == "git":
                parsed = urlparse(source)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("Git 来源必须为不含凭据的 HTTPS 仓库地址")
                clone = stage / "checkout"
                result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "--depth", "1", "--", source, str(clone)], capture_output=True, text=True, timeout=90)
                if result.returncode:
                    raise ValueError("Git 下载失败，请检查仓库地址和访问权限")
                relative = PurePosixPath(subdir or ".")
                if relative.is_absolute() or ".." in relative.parts or "\\" in subdir:
                    raise ValueError("子目录不能超出仓库")
                src = clone.joinpath(*relative.parts)
                if not src.is_dir() or not src.resolve().is_relative_to(clone.resolve()):
                    raise ValueError("插件子目录不存在")
                self._copy(src, package)
                shutil.rmtree(clone)
            elif source_type == "local":
                src = Path(source).expanduser().resolve()
                if not src.is_dir() or src == self.root or src in self.root.parents or self.root in src.parents:
                    raise ValueError("请选择有效的扩展目录，不能选择工作台数据目录")
                self._copy(src, package)
            else:
                raise ValueError("不支持的来源类型")
            info = self._manifest(package)
            files = [p for p in package.rglob("*") if p.is_file()]
            digest = hashlib.sha256()
            for path in sorted(files):
                digest.update(str(path.relative_to(package)).encode())
                digest.update(path.read_bytes())
            info.update(preview_id=token, source=source, source_type=source_type, subdir=subdir,
                        digest=digest.hexdigest(), files=len(files), size=sum(p.stat().st_size for p in files), created_at=time.time())
            (stage / "preview.json").write_text(json.dumps(info), encoding="utf-8")
            return info
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _runtime(self, package, info, selection):
        """Create a filtered, standard Claude plugin; never modify the snapshot."""
        runtime = package.parent / ("runtime-" + "".join("1" if selection.get(k) else "0" for k in COMPONENTS))
        if runtime.is_dir():
            return runtime
        with tempfile.TemporaryDirectory(dir=package.parent, prefix="runtime-stage-") as temp:
            target = Path(temp) / "plugin"
            self._copy(package, target)
            for kind in ("skills", "agents", "commands", "hooks"):
                if not selection.get(kind):
                    shutil.rmtree(target / kind, ignore_errors=True)
            if not selection.get("mcp"):
                (target / ".mcp.json").unlink(missing_ok=True)
            manifest = {"name": info["id"], "version": info["version"], "description": info["description"]}
            (target / ".claude-plugin").mkdir(exist_ok=True)
            (target / ".claude-plugin/plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
            target.rename(runtime)
        return runtime

    def install(self, token):
        if not re.fullmatch(r"[a-f0-9]{32}", str(token)):
            raise ValueError("无效的安装预览")
        with self.lock:
            stage = self.staging / token
            info = json.loads((stage / "preview.json").read_text())
            if time.time() - info["created_at"] > 86400:
                raise ValueError("预览已过期，请重新预览")
            plugin_id = info["id"]
            previous = next((p for p in self.list("extensions") if p["id"] == plugin_id), None)
            if previous and previous["digest"] == info["digest"]:
                shutil.rmtree(stage)
                return previous
            revision = uuid.uuid4().hex
            dest = self.packages / plugin_id / revision
            dest.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(dest)
            record = {**info, "revision": revision, "installed_at": time.time(), "history": []}
            if previous:
                record["history"] = [{k: v for k, v in previous.items() if k != "history"}] + previous.get("history", [])
            return self.put("extensions", plugin_id, record)

    def rollback(self, plugin_id):
        with self.lock:
            record = self.get("extensions", identifier(plugin_id))
            if not record.get("history"):
                raise ValueError("没有可以回滚的版本")
            prior, *rest = record["history"]
            prior["history"] = rest
            return self.put("extensions", plugin_id, prior)

    def uninstall(self, plugin_id):
        # Keep immutable snapshots until shutdown/maintenance so live SDK turns
        # holding a plugin path are not broken by uninstall/update.
        plugin_id = identifier(plugin_id)
        self.get("extensions", plugin_id)
        self.delete("extensions", plugin_id)
        for record in self.list("bindings"):
            if record["plugin_id"] == plugin_id:
                self.delete("bindings", record["id"])

    def bind(self, plugin_id, enabled, scope="global", cwd="", session_id="", components=None):
        self.get("extensions", identifier(plugin_id))
        key = scope_key(scope, cwd, session_id)
        if components is None:
            components = {kind: kind in {"skills", "agents", "commands", "workflows"} for kind in COMPONENTS}
        if not isinstance(components, dict) or set(components) - set(COMPONENTS):
            raise ValueError("未知扩展组件")
        record_id = hashlib.sha256((key + ":" + plugin_id).encode()).hexdigest()
        return self.put("bindings", record_id, {"id": record_id, "plugin_id": plugin_id, "scope": key,
                        "enabled": bool(enabled), "components": {k: components.get(k) is True for k in COMPONENTS}})

    def effective(self, cwd="", session_id="", ids=None):
        keys = ["global"]
        if cwd:
            keys.append("project:" + str(Path(cwd).expanduser().resolve()))
        if session_id:
            keys.append(scope_key("session", session_id=session_id))
        bindings = self.list("bindings")
        items = []
        for extension in self.list("extensions"):
            chosen = None
            for key in keys:
                chosen = next((b for b in bindings if b["scope"] == key and b["plugin_id"] == extension["id"]), chosen)
            if chosen and chosen["enabled"] and (ids is None or extension["id"] in ids):
                items.append({**extension, "binding": chosen})
        return items

    def available_workflows(self, cwd="", session_id=""):
        rows = self.list("workflows")
        for info in self.effective(cwd, session_id):
            if not info["binding"]["components"].get("workflows"):
                continue
            package = self.packages / info["id"] / info["revision"] / "package"
            for relative in info["inventory"]["workflows"]:
                data = self.validate_config("workflows", json.loads((package / relative).read_text()))
                data["id"] = info["id"] + ":" + Path(relative).stem
                data["extension_id"] = info["id"]
                rows.append(data)
        return rows

    def runtime_plugins(self, cwd="", session_id="", ids=None):
        with self.lock:
            result = []
            for info in self.effective(cwd, session_id, ids):
                if not any(info["binding"]["components"].get(kind) and info["inventory"].get(kind)
                           for kind in ("skills", "agents", "commands", "mcp", "hooks")):
                    continue
                package = self.packages / info["id"] / info["revision"] / "package"
                path = self._runtime(package, info, info["binding"]["components"])
                result.append({"type": "local", "path": str(path.resolve())})
            return result

    def validate_config(self, kind, fields):
        if not isinstance(fields, dict):
            raise ValueError("配置必须是对象")
        name = str(fields.get("name") or "").strip()[:120]
        if not name:
            raise ValueError("名称不能为空")
        out = {"id": identifier(fields.get("id") or uuid.uuid4().hex), "name": name}
        if kind in {"profiles", "commands"}:
            mode = fields.get("mode", "code")
            if mode not in MODES:
                raise ValueError("未知工作模式")
            if not isinstance(fields.get("extension_ids", []), list):
                raise ValueError("扩展组合必须为列表")
            out.update(mode=mode, model=str(fields.get("model") or "")[:160], effort=fields.get("effort") or "high",
                       permission_mode=fields.get("permission_mode") or "default", system_prompt=str(fields.get("system_prompt") or "")[:20000],
                       extension_ids=[identifier(v) for v in fields.get("extension_ids", [])],
                       provider_id=str(fields.get("provider_id") or ""), cwd=canonical_cwd(fields["cwd"]) if fields.get("cwd") else "")
            if out["effort"] not in {"low", "medium", "high", "max"} or out["permission_mode"] not in {"default", "acceptEdits", "bypassPermissions", "plan"}:
                raise ValueError("无效的推理强度或权限模式")
            if kind == "commands":
                out["prompt"] = str(fields.get("prompt") or "").strip()[:40000]
                if not out["prompt"]:
                    raise ValueError("命令提示词不能为空")
        elif kind == "workflows":
            steps = fields.get("steps")
            if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
                raise ValueError("工作流需包含 1–30 个步骤")
            validated = []
            for step in steps:
                if not isinstance(step, dict) or step.get("type") not in {"prompt", "command", "report", "browser_validation"}:
                    raise ValueError("不支持的工作流步骤")
                value = str(step.get("value") or step.get("command") or step.get("recipe_id") or "")[:40000]
                if step["type"] != "report" and not value.strip():
                    raise ValueError("步骤内容不能为空")
                validated.append({"type": step["type"], "value": value, "name": str(step.get("name") or step["type"])[:120],
                                  "timeout": max(1, min(600, int(step.get("timeout", 120))))})
            out.update(steps=validated, profile_id=str(fields.get("profile_id") or ""))
        else:
            raise ValueError("不支持的配置类型")
        return out

    def save_config(self, kind, fields):
        return self.put(kind, (record := self.validate_config(kind, fields))["id"], record)

    def marketplace(self):
        root = Path(__file__).with_name("workbench_extensions")
        entries = []
        for path in sorted(root.iterdir()):
            if path.is_dir():
                info = self._manifest(path)
                entries.append({**info, "source": str(path), "source_type": "local", "builtin": True})
        for source in self.list("sources"):
            for plugin in source["plugins"]:
                entries.append({**plugin, "catalog_id": source["id"], "builtin": False})
        ratings = {row["id"]: row["stars"] for row in self.list("ratings")}
        return [{**entry, "local_rating": ratings.get(entry["id"], 0)} for entry in entries]

    def export_zip(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ccw-workspace.json", json.dumps(self.export_bundle(), ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "CCW Desk configuration bundle. Import ccw-workspace.json in Extensions & Workbench. Installed extensions are listed as sources; credentials and executable package contents are not exported.")
        return output.getvalue()

    def export_bundle(self):
        return {"format": "ccw-desk-workspace", "version": 1,
                **{kind: clean_export(self.list(kind)) for kind in ("profiles", "commands", "workflows")},
                "extensions": [{"id": x["id"], "version": x["version"], "source": clean_export(x["source"]), "source_type": x["source_type"]} for x in self.list("extensions")]}

    def import_bundle(self, blob, *, preview=False, revision=None):
        if not isinstance(blob, dict) or blob.get("format") != "ccw-desk-workspace" or blob.get("version") != 1:
            raise ValueError("不是支持的 CCW Desk 工作区配置")
        records = []
        seen = set()
        for kind in ("profiles", "commands", "workflows"):
            rows = blob.get(kind, [])
            if not isinstance(rows, list) or len(records) + len(rows) > 300:
                raise ValueError("配置需按类型提供列表，最多导入 300 项")
            for row in rows:
                if (preview or revision is not None) and (not isinstance(row, dict) or not row.get("id")):
                    raise ValueError("预览导入的配置必须包含标识（id）")
                record = self.validate_config(kind, row)
                key = (kind, record["id"])
                if key in seen:
                    raise ValueError("配置文件包含重复标识：" + record["id"])
                seen.add(key)
                records.append((kind, record))
        with self.lock, self.connect() as conn:
            if not preview:
                conn.execute("BEGIN IMMEDIATE")
            if preview or revision is not None:
                changes, snapshot = [], []
                for kind, record in records:
                    row = conn.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, record["id"])).fetchone()
                    previous = json.loads(row["value"]) if row else None
                    status = "new" if previous is None else "unchanged" if previous == record else "replace"
                    changes.append({"kind": kind, "id": record["id"], "name": record["name"], "status": status})
                    snapshot.append((kind, record, previous))
                current_revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
                if preview:
                    return {"items": changes, "revision": current_revision}
                if revision != current_revision:
                    raise ImportPreviewChanged("配置在预览后已变化，请重新选择文件并确认")
                records = [(kind, record) for (kind, record, previous) in snapshot if previous != record]
            for kind, record in records:
                conn.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value", (kind, record["id"], json.dumps(record)))
        return len(records)

    def resolve_request(self, req, cwd, session_id, *, expand_commands=True, existing_session=False):
        saved = next((r for r in self.list("session_modes") if r["id"] == session_id), {})
        profile = self.get("profiles", req.profile_id) if req.profile_id else saved.get("config", {})
        command = self.get("commands", req.command_id) if req.command_id else {}
        trigger, _, argument = req.message.partition(" ")
        if expand_commands and not command and not req.profile_id and trigger.startswith("/"):
            command = next((c for c in self.list("commands") if c["id"] == trigger[1:]), {})
            if command:
                req.display_message = req.display_message or req.message
                req.message = argument
        config = {**profile, **command}
        explicit_config = bool(req.profile_id or command)
        remembered = saved
        mode = command.get("mode") or req.work_mode or config.get("mode") or remembered.get("mode") or ("code" if req.workspace_mode == "code" else "chat")
        if mode not in MODES:
            raise ValueError("未知工作模式")
        binding = next((r for r in self.list("session_providers") if r["id"] == session_id), {})
        pinned = binding.get("provider_id", "")
        provider = req.provider_id or (config.get("provider_id") if explicit_config else pinned or config.get("provider_id"))
        route = ({"model": saved.get("route_model")} if existing_session and not pinned
                 else next((r for r in self.list("routes") if r["id"] == mode), {}))
        provider = provider or route.get("provider_id", "")
        if provider:
            # The ordinary composer picker contains Claude model aliases. It
            # must never send those aliases to an unrelated provider. A model
            # override must explicitly identify the provider that owns it.
            requested_model = req.model if getattr(req, "model_provider_id", None) == provider else None
            if pinned == provider and not explicit_config:
                model = requested_model or binding.get("model") or config.get("model") or self.get("providers", provider)["model"]
            else:
                model = requested_model or config.get("model") or (route.get("model") if route.get("provider_id") == provider else None) or self.get("providers", provider)["model"]
        else:
            model = (config.get("model") or route.get("model") or req.model) if explicit_config else (req.model or config.get("model") or route.get("model"))
        prompt = config.get("system_prompt", "")
        if config:
            req.model = model
            req.effort = (config.get("effort") or req.effort) if explicit_config else (req.effort or config.get("effort"))
            req.permission_mode = (config.get("permission_mode") or req.permission_mode) if explicit_config else (req.permission_mode or config.get("permission_mode"))
        req.model = model
        if mode in {"research", "review", "browser"}:
            req.disallowed_tools = sorted(set((req.disallowed_tools or []) + WRITE_TOOLS))
            req.permission_mode = "default"
        if mode in {"research", "review", "browser"}:
            req.browser_enabled = MODES[mode]["browser"]
        if command:
            req.message = command["prompt"].replace("{{input}}", req.message) if "{{input}}" in command["prompt"] else command["prompt"] + "\n\n" + req.message
        req.system_prompt = "\n\n".join(dict.fromkeys(p for p in (req.system_prompt, prompt, MODE_PROMPTS.get(mode)) if p))
        extension_ids = req.extension_ids if req.extension_ids is not None else config.get("extension_ids", remembered.get("extension_ids"))
        return {"mode": mode, "provider_id": provider, "extension_ids": extension_ids, "config": config, "route_model": route.get("model") if not provider else None}
