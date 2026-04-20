import asyncio
import json
import os
import signal
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
HISTORY_DIR = BASE_DIR / "history"
UPLOADS_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "claude-web.db"

HISTORY_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_UPLOAD_MB = 20
IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".cache", ".idea", ".vscode"}

app = FastAPI(title="Claude Code Web")

_running_processes: Dict[str, asyncio.subprocess.Process] = {}


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        ensure_column(conn, "sessions", "pinned", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "sessions", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "sessions", "tags", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "sessions", "manual_title", "INTEGER NOT NULL DEFAULT 0")


init_db()


def upsert_session(session_id: str, title: str, cwd: str) -> None:
    now = time.time()
    with db_connect() as conn:
        row = conn.execute(
            "SELECT title, manual_title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sessions (id, title, cwd, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, title, cwd, now, now),
            )
        else:
            new_title = row["title"]
            if not row["manual_title"] and not new_title:
                new_title = title
            conn.execute(
                "UPDATE sessions SET title = ?, cwd = ?, updated_at = ? WHERE id = ?",
                (new_title, cwd, now, session_id),
            )


def append_event(session_id: str, event: dict) -> None:
    path = HISTORY_DIR / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events(session_id: str) -> List[dict]:
    path = HISTORY_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    events: List[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def summarize_text_from_events(events: List[dict]) -> str:
    parts: List[str] = []
    for ev in events:
        if ev.get("type") == "user_input":
            parts.append(ev.get("text", ""))
        elif ev.get("type") == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n".join(parts)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    images: Optional[List[str]] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    display_message: Optional[str] = None
    permission_mode: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None


class PromptRequest(BaseModel):
    name: str
    content: str


class SessionPatch(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    tags: Optional[str] = None


class ForkRequest(BaseModel):
    event_index: int
    new_text: Optional[str] = None


class RestoreRequest(BaseModel):
    event_index: int


def build_args(
    message: str,
    session_id: str,
    resume: bool,
    model: Optional[str],
    system_prompt: Optional[str],
    permission_mode: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
) -> List[str]:
    args = [
        "claude",
        "-p", message,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if resume:
        args += ["--resume", session_id]
    else:
        args += ["--session-id", session_id]
    if model:
        args += ["--model", model]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if permission_mode and permission_mode in ("default", "acceptEdits", "bypassPermissions", "plan"):
        args += ["--permission-mode", permission_mode]
    if allowed_tools:
        args += ["--allowed-tools", ",".join(allowed_tools)]
    if disallowed_tools:
        args += ["--disallowed-tools", ",".join(disallowed_tools)]
    return args


def compose_message(message: str, images: Optional[List[str]]) -> str:
    if not images:
        return message
    lines = [message.rstrip(), "", "[附加图片]"]
    for p in images:
        lines.append(f"- {p}")
    return "\n".join(lines)


async def _git_run(cwd: str, *args: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        return stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


async def create_git_checkpoint(cwd: str) -> Optional[dict]:
    if not cwd or not os.path.isdir(cwd):
        return None
    git_dir = await _git_run(cwd, "rev-parse", "--git-dir")
    if git_dir is None:
        return None
    head = await _git_run(cwd, "rev-parse", "HEAD")
    if head is None:
        return None
    stash = await _git_run(cwd, "stash", "create", f"claude-web-checkpoint-{int(time.time())}")
    return {"type": "git", "head": head, "stash": stash or ""}


async def restore_git_checkpoint(cwd: str, cp: dict) -> bool:
    if not cp or cp.get("type") != "git" or not cwd:
        return False
    head = cp.get("head")
    stash = cp.get("stash") or ""
    if not head:
        return False
    if await _git_run(cwd, "reset", "--hard", head) is None:
        return False
    await _git_run(cwd, "clean", "-fd")
    if stash:
        await _git_run(cwd, "stash", "apply", stash)
    return True


def format_context_snippet(events: List[dict], max_chars: int = 6000) -> str:
    lines: List[str] = []
    total = 0
    for ev in events:
        t = ev.get("type")
        if t == "user_input":
            text = (ev.get("text") or "").strip()
            if text:
                chunk = f"用户: {text}"
                lines.append(chunk)
                total += len(chunk)
        elif t == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        chunk = f"助手: {text[:600]}"
                        lines.append(chunk)
                        total += len(chunk)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    chunk = f"(助手调用了工具: {name})"
                    lines.append(chunk)
                    total += len(chunk)
        if total > max_chars:
            lines.append("...（历史已截断）")
            break
    return "\n\n".join(lines)


def derive_title(message: str) -> str:
    text = message.strip().replace("\n", " ")
    return text[:60] if text else "未命名会话"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    is_new = req.session_id is None
    session_id = req.session_id or str(uuid.uuid4())
    work_dir = req.cwd or os.path.expanduser("~")
    full_message = compose_message(req.message, req.images)
    display_text = req.display_message if req.display_message is not None else req.message

    checkpoint = await create_git_checkpoint(work_dir)

    user_event = {
        "type": "user_input",
        "text": display_text,
        "images": req.images or [],
        "ts": time.time(),
        "checkpoint": checkpoint,
    }
    append_event(session_id, user_event)
    upsert_session(session_id, derive_title(display_text), work_dir)

    async def generate():
        meta = {
            "type": "meta",
            "session_id": session_id,
            "cwd": work_dir,
            "has_checkpoint": checkpoint is not None,
        }
        yield f"data: {json.dumps(meta)}\n\n"

        args = build_args(
            full_message, session_id,
            resume=not is_new,
            model=req.model,
            system_prompt=req.system_prompt,
            permission_mode=req.permission_mode,
            allowed_tools=req.allowed_tools,
            disallowed_tools=req.disallowed_tools,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                limit=16 * 1024 * 1024,
            )
        except FileNotFoundError:
            err_event = {"type": "error", "message": "claude CLI not found in PATH"}
            append_event(session_id, err_event)
            yield f"data: {json.dumps(err_event)}\n\n"
            return

        _running_processes[session_id] = process
        try:
            assert process.stdout is not None
            while True:
                try:
                    raw = await process.stdout.readline()
                except ValueError as e:
                    err_event = {"type": "error", "message": f"stdout line too large: {e}"}
                    append_event(session_id, err_event)
                    yield f"data: {json.dumps(err_event)}\n\n"
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = {"type": "raw", "text": line}
                t = obj.get("type")
                if t != "stream_event" and not (t == "system" and obj.get("subtype", "").startswith("hook_")):
                    append_event(session_id, obj)
                yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

            rc = await process.wait()
            err = b""
            if process.stderr is not None:
                err = await process.stderr.read()
            if rc != 0:
                err_event = {
                    "type": "error",
                    "message": err.decode("utf-8", errors="replace") or f"claude exited with code {rc}",
                }
                append_event(session_id, err_event)
                yield f"data: {json.dumps(err_event)}\n\n"
        finally:
            _running_processes.pop(session_id, None)

        upsert_session(session_id, derive_title(display_text), work_dir)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/stop/{session_id}")
async def stop_chat(session_id: str):
    process = _running_processes.get(session_id)
    if process is None:
        raise HTTPException(status_code=404, detail="no running process for this session")
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass
    stop_event = {"type": "error", "message": "用户中止", "ts": time.time()}
    append_event(session_id, stop_event)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/prepare-fork")
async def prepare_fork(session_id: str, req: ForkRequest):
    events = load_events(session_id)
    user_event_positions = [i for i, e in enumerate(events) if e.get("type") == "user_input"]
    if req.event_index < 0 or req.event_index >= len(user_event_positions):
        raise HTTPException(status_code=400, detail="invalid event_index")

    target_pos = user_event_positions[req.event_index]
    events_before = events[:target_pos]
    original_text = events[target_pos].get("text", "")
    new_text = req.new_text if req.new_text is not None and req.new_text.strip() else original_text

    with db_connect() as conn:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    cwd = row["cwd"] if row else os.path.expanduser("~")

    new_id = str(uuid.uuid4())
    upsert_session(new_id, derive_title(new_text), cwd)

    with db_connect() as conn:
        conn.execute(
            "UPDATE sessions SET tags = ? WHERE id = ?",
            (f"forked-from-{session_id[:8]}", new_id),
        )

    context = format_context_snippet(events_before)
    if context:
        packed_message = (
            "【以下是之前的对话历史，仅作为参考上下文（不要重复回应历史问题）】\n"
            f"{context}\n\n"
            "【请基于以上历史上下文，回应这个新问题】\n"
            f"{new_text}"
        )
    else:
        packed_message = new_text

    return {
        "session_id": new_id,
        "cwd": cwd,
        "sent_message": packed_message,
        "display_message": new_text,
        "forked_from": session_id,
    }


@app.post("/api/sessions/{session_id}/restore-checkpoint")
async def restore_checkpoint(session_id: str, req: RestoreRequest):
    events = load_events(session_id)
    user_event_positions = [i for i, e in enumerate(events) if e.get("type") == "user_input"]
    if req.event_index < 0 or req.event_index >= len(user_event_positions):
        raise HTTPException(status_code=400, detail="invalid event_index")
    ev = events[user_event_positions[req.event_index]]
    cp = ev.get("checkpoint")
    if not cp:
        raise HTTPException(status_code=400, detail="no checkpoint on this turn")

    with db_connect() as conn:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
    cwd = row["cwd"] if row else ""
    if not cwd:
        raise HTTPException(status_code=400, detail="session has no cwd")

    ok = await restore_git_checkpoint(cwd, cp)
    if not ok:
        raise HTTPException(status_code=500, detail="restore failed")
    return {"ok": True, "cwd": cwd}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if file.filename is None:
        raise HTTPException(status_code=400, detail="filename missing")
    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"unsupported type {ext}")

    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"file exceeds {MAX_UPLOAD_MB} MB")

    name = f"{uuid.uuid4().hex}{ext}"
    path = UPLOADS_DIR / name
    path.write_bytes(data)

    return {
        "path": str(path.absolute()),
        "url": f"/uploads/{name}",
        "name": file.filename,
        "size": len(data),
    }


def _row_to_session(r: sqlite3.Row) -> dict:
    tags = [t for t in (r["tags"] or "").split(",") if t]
    return {
        "id": r["id"],
        "title": r["title"] or "未命名会话",
        "cwd": r["cwd"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "pinned": bool(r["pinned"]),
        "archived": bool(r["archived"]),
        "tags": tags,
    }


@app.get("/api/sessions")
async def list_sessions(q: Optional[str] = None, archived: bool = False, tag: Optional[str] = None):
    with db_connect() as conn:
        where = "archived = 1" if archived else "archived = 0"
        rows = conn.execute(
            f"SELECT id, title, cwd, created_at, updated_at, pinned, archived, tags FROM sessions "
            f"WHERE {where} ORDER BY pinned DESC, updated_at DESC LIMIT 500"
        ).fetchall()

    items = [_row_to_session(r) for r in rows]

    if tag:
        items = [i for i in items if tag in i["tags"]]

    if q:
        q_lower = q.lower()
        filtered: List[dict] = []
        for item in items:
            if q_lower in item["title"].lower() or q_lower in ",".join(item["tags"]).lower():
                filtered.append(item)
                continue
            try:
                events = load_events(item["id"])
                content = summarize_text_from_events(events).lower()
                if q_lower in content:
                    filtered.append(item)
            except Exception:
                continue
        items = filtered

    return items


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id, title, cwd, created_at, updated_at, pinned, archived, tags FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    data = _row_to_session(row)
    data["events"] = load_events(session_id)
    return data


@app.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, req: SessionPatch):
    updates: List[str] = []
    params: List = []
    if req.title is not None:
        updates += ["title = ?", "manual_title = 1"]
        params.append(req.title)
    if req.pinned is not None:
        updates.append("pinned = ?")
        params.append(1 if req.pinned else 0)
    if req.archived is not None:
        updates.append("archived = ?")
        params.append(1 if req.archived else 0)
    if req.tags is not None:
        updates.append("tags = ?")
        params.append(req.tags)
    if not updates:
        return {"ok": True}
    params.append(session_id)
    with db_connect() as conn:
        conn.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", params)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    with db_connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    path = HISTORY_DIR / f"{session_id}.jsonl"
    if path.exists():
        path.unlink()
    return {"ok": True}


@app.post("/api/sessions/{session_id}/suggest-title")
async def suggest_title(session_id: str):
    events = load_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="empty session")
    summary = summarize_text_from_events(events)[:3000]
    if not summary.strip():
        raise HTTPException(status_code=400, detail="no textual content")
    prompt = f"根据下面的对话，用中文生成一个不超过15字、不带引号的会话标题（只输出标题本身）：\n\n{summary}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--output-format", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="title generation timeout")
    title = stdout.decode("utf-8", errors="replace").strip().splitlines()[0].strip(' "\'"""''').strip()[:60]
    if not title:
        raise HTTPException(status_code=500, detail="empty title")
    with db_connect() as conn:
        conn.execute("UPDATE sessions SET title = ?, manual_title = 1 WHERE id = ?", (title, session_id))
    return {"title": title}


@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str):
    events = load_events(session_id)
    if not events:
        raise HTTPException(status_code=404, detail="session not found")
    with db_connect() as conn:
        row = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
    title = row["title"] if row and row["title"] else session_id

    lines: List[str] = [f"# {title}", "", f"_会话 ID: {session_id}_", ""]
    for ev in events:
        t = ev.get("type")
        if t == "user_input":
            lines += ["## 👤 用户", "", ev.get("text", "")]
            for img in ev.get("images", []) or []:
                lines.append(f"![image]({img})")
            lines.append("")
        elif t == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "text":
                    lines += ["## 🤖 Claude", "", block.get("text", ""), ""]
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    lines += [f"### 🔧 工具调用: `{name}`", "", "```json",
                              json.dumps(block.get("input", {}), ensure_ascii=False, indent=2), "```", ""]
        elif t == "user":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if block.get("type") == "tool_result":
                    ct = block.get("content", "")
                    if isinstance(ct, list):
                        ct = "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in ct)
                    lines += ["### 📋 工具结果", "", "```", str(ct)[:5000], "```", ""]

    md = "\n".join(lines)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
    )


@app.get("/api/prompts")
async def list_prompts():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, name, content, created_at FROM prompts ORDER BY created_at DESC"
        ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "content": r["content"], "created_at": r["created_at"]}
        for r in rows
    ]


@app.post("/api/prompts")
async def create_prompt(req: PromptRequest):
    pid = uuid.uuid4().hex
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO prompts (id, name, content, created_at) VALUES (?, ?, ?, ?)",
            (pid, req.name, req.content, time.time()),
        )
    return {"id": pid}


@app.put("/api/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, req: PromptRequest):
    with db_connect() as conn:
        conn.execute(
            "UPDATE prompts SET name = ?, content = ? WHERE id = ?",
            (req.name, req.content, prompt_id),
        )
    return {"ok": True}


@app.delete("/api/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str):
    with db_connect() as conn:
        conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    return {"ok": True}


@app.get("/api/cwds")
async def list_cwds():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT cwd, MAX(updated_at) AS last FROM sessions WHERE cwd <> '' GROUP BY cwd ORDER BY last DESC LIMIT 10"
        ).fetchall()
    return [r["cwd"] for r in rows]


@app.get("/api/tags")
async def list_tags():
    with db_connect() as conn:
        rows = conn.execute("SELECT tags FROM sessions WHERE tags <> '' AND archived = 0").fetchall()
    counts: Dict[str, int] = defaultdict(int)
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                counts[t] += 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


@app.get("/api/stats")
async def stats():
    total_cost = 0.0
    total_duration = 0.0
    total_turns = 0
    daily: Dict[str, Dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "turns": 0})
    tool_counts: Dict[str, int] = defaultdict(int)
    with db_connect() as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
    total_sessions = len(rows)
    for row in rows:
        events = load_events(row["id"])
        for ev in events:
            t = ev.get("type")
            if t == "result":
                cost = float(ev.get("total_cost_usd") or 0)
                dur = float(ev.get("duration_ms") or 0)
                ts = float(ev.get("ts") or time.time())
                total_cost += cost
                total_duration += dur
                total_turns += 1
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                daily[day]["cost"] += cost
                daily[day]["turns"] += 1
            elif t == "assistant":
                content = (ev.get("message") or {}).get("content") or []
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_counts[block.get("name", "?")] += 1
    daily_sorted = sorted(daily.items(), key=lambda x: x[0])
    return {
        "total_cost_usd": round(total_cost, 4),
        "total_duration_ms": total_duration,
        "total_sessions": total_sessions,
        "total_turns": total_turns,
        "daily": [{"date": d, "cost": round(v["cost"], 4), "turns": v["turns"]} for d, v in daily_sorted],
        "tools": sorted(
            [{"name": k, "count": v} for k, v in tool_counts.items()],
            key=lambda x: -x["count"],
        )[:10],
    }


@app.get("/api/files")
async def list_files(cwd: str = Query(...), q: str = Query(default=""), limit: int = Query(default=30)):
    base = Path(os.path.expanduser(cwd)).resolve()
    if not base.exists() or not base.is_dir():
        return []
    q_lower = q.lower()
    results: List[dict] = []
    for root, dirs, files in os.walk(str(base)):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            full = Path(root) / f
            try:
                rel = str(full.relative_to(base))
            except ValueError:
                continue
            if q_lower and q_lower not in rel.lower():
                continue
            results.append({"path": str(full), "rel": rel})
            if len(results) >= limit:
                return results
    return results


@app.get("/api/git")
async def git_status(cwd: str = Query(...)):
    target = os.path.expanduser(cwd)
    if not os.path.isdir(target):
        return {"branch": "", "dirty": 0, "available": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", target, "status", "--porcelain=v1", "--branch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        return {"branch": "", "dirty": 0, "available": False}
    if proc.returncode != 0:
        return {"branch": "", "dirty": 0, "available": False}
    branch = ""
    dirty = 0
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("##"):
            header = line[2:].strip()
            branch = header.split("...")[0].strip()
        else:
            dirty += 1
    return {"branch": branch, "dirty": dirty, "available": True}


app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port)
