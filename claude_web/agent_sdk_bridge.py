"""Async framed client for the Claude Agent SDK Node bridge.

The browser never talks to the SDK directly. One Node daemon owns the native
Claude Query objects while FastAPI routes their raw SDK events into the
existing SSE and history pipeline.

Commands sent to Node remain newline-delimited JSON for compatibility with the
daemon's stdin parser. Responses use a 4-byte big-endian length prefix followed
by one UTF-8 JSON payload. Framing avoids asyncio's line-reader limit for large
tool results, images, and SDK events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional


_log = logging.getLogger("claude_web.agent_sdk")

BRIDGE_PROTOCOL_VERSION = 2
DEFAULT_SUBPROCESS_STREAM_LIMIT = 16 * 1024 * 1024
MIN_SUBPROCESS_STREAM_LIMIT = 1024 * 1024
MAX_SUBPROCESS_STREAM_LIMIT = 64 * 1024 * 1024
DEFAULT_MAX_FRAME_SIZE = 64 * 1024 * 1024
MIN_MAX_FRAME_SIZE = 1024 * 1024
MAX_MAX_FRAME_SIZE = 256 * 1024 * 1024


class AgentSdkBridgeError(RuntimeError):
    pass


@dataclass
class AgentSdkTurn:
    request_id: str
    session_key: str
    queue: "asyncio.Queue[dict]"

    async def events(self) -> AsyncIterator[dict]:
        while True:
            item = await self.queue.get()
            yield item
            if item.get("type") == "done":
                return


class AgentSdkBridge:
    def __init__(self, daemon_path: Optional[Path] = None) -> None:
        self.daemon_path = daemon_path or Path(__file__).with_name("agent_bridge") / "daemon.mjs"
        self.process: Optional[asyncio.subprocess.Process] = None
        self.sdk_info: Optional[dict] = None
        self.last_error = ""
        self._start_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._ready: Optional[asyncio.Future] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._queues: Dict[str, "asyncio.Queue[dict]"] = {}
        self._turn_sessions: Dict[str, str] = {}
        self._responses: Dict[str, asyncio.Future] = {}
        self._transport_failed = False
        self._stopping = False

    @staticmethod
    def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(value, maximum))

    @property
    def subprocess_stream_limit(self) -> int:
        return self._bounded_env_int(
            "CLAUDE_WEB_AGENT_BRIDGE_STREAM_LIMIT",
            DEFAULT_SUBPROCESS_STREAM_LIMIT,
            MIN_SUBPROCESS_STREAM_LIMIT,
            MAX_SUBPROCESS_STREAM_LIMIT,
        )

    @property
    def max_frame_size(self) -> int:
        return self._bounded_env_int(
            "CLAUDE_WEB_AGENT_BRIDGE_MAX_FRAME_SIZE",
            DEFAULT_MAX_FRAME_SIZE,
            MIN_MAX_FRAME_SIZE,
            MAX_MAX_FRAME_SIZE,
        )

    @property
    def enabled(self) -> bool:
        return os.environ.get("CLAUDE_WEB_CODE_RUNTIME", "agent-sdk").strip().lower() not in {
            "cli", "legacy", "off", "disabled"
        }

    @property
    def running(self) -> bool:
        return (
            self.process is not None
            and self.process.returncode is None
            and self.sdk_info is not None
            and not self._transport_failed
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    @property
    def process_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def ensure_started(self) -> bool:
        if not self.enabled:
            self.last_error = "Claude Agent SDK runtime disabled by CLAUDE_WEB_CODE_RUNTIME"
            return False
        if self.running:
            return True
        async with self._start_lock:
            if self.running:
                return True
            await self._start()
            return self.running

    async def _start(self) -> None:
        await self._terminate_process()
        node = os.environ.get("CLAUDE_WEB_NODE") or shutil.which("node")
        if not node:
            self.last_error = "Node.js not found; Claude Agent SDK Code runtime cannot start"
            return
        if not self.daemon_path.exists():
            self.last_error = f"Claude Agent SDK bridge missing: {self.daemon_path}"
            return
        self.last_error = ""
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._transport_failed = False
        self._stopping = False
        try:
            self.process = await asyncio.create_subprocess_exec(
                node,
                str(self.daemon_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.daemon_path.parent),
                limit=self.subprocess_stream_limit,
            )
            self._reader_task = asyncio.create_task(self._read_stdout())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            ready = await asyncio.wait_for(asyncio.shield(self._ready), timeout=12.0)
            protocol = int(ready.get("protocol") or 0)
            if protocol != BRIDGE_PROTOCOL_VERSION:
                raise AgentSdkBridgeError(
                    f"Unsupported Claude Agent SDK bridge protocol {protocol}; "
                    f"expected {BRIDGE_PROTOCOL_VERSION}"
                )
            self.sdk_info = ready.get("sdk") or {}
            self.last_error = ""
            _log.info("Claude Agent SDK bridge ready: %s", self.sdk_info)
        except Exception as exc:
            self.last_error = str(exc)
            _log.warning("Claude Agent SDK bridge unavailable: %s", exc)
            await self._terminate_process()

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        unexpected_exit = False
        try:
            while True:
                try:
                    header = await process.stdout.readexactly(4)
                except asyncio.IncompleteReadError as exc:
                    if not exc.partial:
                        unexpected_exit = not self._stopping
                        break
                    raise AgentSdkBridgeError("Claude Agent SDK bridge emitted a truncated frame header") from exc
                frame_size = int.from_bytes(header, "big")
                if frame_size <= 0 or frame_size > self.max_frame_size:
                    raise AgentSdkBridgeError(
                        f"Claude Agent SDK bridge frame size {frame_size} exceeds the "
                        f"configured limit {self.max_frame_size}"
                    )
                try:
                    raw = await process.stdout.readexactly(frame_size)
                except asyncio.IncompleteReadError as exc:
                    raise AgentSdkBridgeError(
                        f"Claude Agent SDK bridge frame was truncated "
                        f"({len(exc.partial)}/{frame_size} bytes)"
                    ) from exc
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise AgentSdkBridgeError("Claude Agent SDK bridge emitted invalid framed JSON") from exc
                payload_type = payload.get("type")
                request_id = str(payload.get("id") or "")
                if payload_type == "ready":
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(payload)
                    continue
                if payload_type == "fatal":
                    self.last_error = str(payload.get("message") or "Agent SDK bridge failed")
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_exception(AgentSdkBridgeError(self.last_error))
                    continue
                queue = self._queues.get(request_id)
                if queue is not None:
                    try:
                        queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        self._queues.pop(request_id, None)
                        session_key = self._turn_sessions.pop(request_id, "")
                        while not queue.empty():
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        queue.put_nowait({
                            "type": "error",
                            "message": "Claude Agent SDK event buffer overflowed; the stalled turn was interrupted",
                        })
                        queue.put_nowait({"type": "done", "success": False})
                        if session_key:
                            asyncio.create_task(self._best_effort_interrupt(session_key))
                        continue
                    if payload_type == "done":
                        self._queues.pop(request_id, None)
                        self._turn_sessions.pop(request_id, None)
                    continue
                future = self._responses.pop(request_id, None)
                if future is not None and not future.done():
                    if payload_type == "error":
                        future.set_exception(AgentSdkBridgeError(str(payload.get("message") or "Agent SDK request failed")))
                    else:
                        future.set_result(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            unexpected_exit = True
            self.last_error = str(exc)
            self._transport_failed = True
            _log.exception("Claude Agent SDK bridge reader failed")
        finally:
            if unexpected_exit and not self.last_error:
                self.last_error = "Claude Agent SDK bridge exited unexpectedly"
                self._transport_failed = True
            message = self.last_error or "Claude Agent SDK bridge exited"
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(AgentSdkBridgeError(message))
            for queue in list(self._queues.values()):
                self._force_fail_queue(queue, message)
            self._queues.clear()
            self._turn_sessions.clear()
            for future in list(self._responses.values()):
                if not future.done():
                    future.set_exception(AgentSdkBridgeError(message))
            self._responses.clear()
            if unexpected_exit:
                # Let this reader finish before process teardown waits on it.
                # The expected-process guard prevents this cleanup task from
                # touching a replacement daemon started in the meantime.
                asyncio.create_task(self._terminate_process(expected_process=process))

    @staticmethod
    def _force_fail_queue(queue: "asyncio.Queue[dict]", message: str) -> None:
        terminal = (
            {"type": "error", "message": message, "code": "bridge_transport_lost"},
            {"type": "done", "success": False},
        )
        while queue.qsize() > max(0, queue.maxsize - len(terminal)):
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        for item in terminal:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                break

    async def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                _log.debug("Agent SDK: %s", raw.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # stderr is diagnostic-only. A giant/non-delimited diagnostic must
            # not take down the framed stdout transport or leak a task error.
            _log.warning("Claude Agent SDK bridge stderr reader stopped: %s", exc)

    async def _write(self, payload: dict) -> None:
        if not self.running or self.process is None or self.process.stdin is None:
            raise AgentSdkBridgeError(self.last_error or "Claude Agent SDK bridge is not running")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self.process.stdin.write(data)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AgentSdkBridgeError("Claude Agent SDK bridge connection closed") from exc

    async def open_turn(self, session_key: str, params: dict, timeout: float = 12.0) -> AgentSdkTurn:
        if not await self.ensure_started():
            raise AgentSdkBridgeError(self.last_error or "Claude Agent SDK is unavailable")
        request_id = str(uuid.uuid4())
        # Bound native streaming events so a disconnected/slow browser applies
        # backpressure to the bridge instead of growing Python memory forever.
        queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=1024)
        self._queues[request_id] = queue
        self._turn_sessions[request_id] = session_key
        try:
            await self._write({"id": request_id, "method": "send", "params": {**params, "sessionKey": session_key}})
            accepted = await asyncio.wait_for(queue.get(), timeout=timeout)
        except Exception:
            self._queues.pop(request_id, None)
            self._turn_sessions.pop(request_id, None)
            raise
        if accepted.get("type") == "error":
            self._queues.pop(request_id, None)
            self._turn_sessions.pop(request_id, None)
            raise AgentSdkBridgeError(str(accepted.get("message") or "Claude Agent SDK rejected the turn"))
        if accepted.get("type") != "accepted":
            self._queues.pop(request_id, None)
            self._turn_sessions.pop(request_id, None)
            raise AgentSdkBridgeError(f"Unexpected Agent SDK response: {accepted.get('type')}")
        return AgentSdkTurn(request_id=request_id, session_key=session_key, queue=queue)

    async def _best_effort_interrupt(self, session_key: str) -> None:
        try:
            await self.interrupt(session_key)
        except Exception:
            pass

    async def abandon_turn(self, turn: AgentSdkTurn) -> None:
        """Detach a cancelled SSE consumer before asking the daemon to stop."""
        self._queues.pop(turn.request_id, None)
        self._turn_sessions.pop(turn.request_id, None)
        await self._best_effort_interrupt(turn.session_key)

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = 8.0) -> dict:
        if not await self.ensure_started():
            raise AgentSdkBridgeError(self.last_error or "Claude Agent SDK is unavailable")
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._responses.pop(request_id, None)

    async def interrupt(self, session_key: str) -> None:
        await self.request("interrupt", {"sessionKey": session_key})

    async def context_usage(
        self,
        session_key: str,
        params: Optional[dict] = None,
        timeout: float = 180.0,
    ) -> dict:
        return await self.request(
            "context",
            {**(params or {}), "sessionKey": session_key},
            timeout=timeout,
        )

    async def set_model(
        self,
        session_key: str,
        model: Optional[str],
        *,
        runtime_epoch: str = "",
    ) -> dict:
        return await self.request(
            "set_model",
            {"sessionKey": session_key, "model": model, "runtimeEpoch": runtime_epoch},
            timeout=15.0,
        )

    async def set_permission_mode(
        self,
        session_key: str,
        permission_mode: str,
        *,
        runtime_epoch: str = "",
    ) -> dict:
        return await self.request(
            "set_permission_mode",
            {
                "sessionKey": session_key,
                "permissionMode": permission_mode,
                "runtimeEpoch": runtime_epoch,
            },
            timeout=15.0,
        )

    async def pending_permissions(self, session_key: str) -> dict:
        return await self.request(
            "pending_permissions",
            {"sessionKey": session_key},
            timeout=8.0,
        )

    async def fork_session(
        self,
        source_session_id: str,
        *,
        cwd: str = "",
        up_to_message_id: str = "",
        title: str = "",
    ) -> dict:
        return await self.request(
            "fork_session",
            {
                "sourceSessionId": source_session_id,
                "cwd": cwd,
                "upToMessageId": up_to_message_id,
                "title": title,
            },
            timeout=30.0,
        )

    async def session_messages(
        self,
        session_id: str,
        *,
        cwd: str = "",
        limit: int = 0,
    ) -> List[dict]:
        response = await self.request(
            "session_messages",
            {"sessionId": session_id, "cwd": cwd, "limit": limit},
            timeout=30.0,
        )
        messages = response.get("messages") or []
        return messages if isinstance(messages, list) else []

    async def rewind_files(
        self,
        session_key: str,
        user_message_id: str,
        params: Optional[dict] = None,
        *,
        dry_run: bool = False,
        timeout: float = 45.0,
    ) -> dict:
        return await self.request(
            "rewind_files",
            {
                **(params or {}),
                "sessionKey": session_key,
                "userMessageId": user_message_id,
                "dryRun": bool(dry_run),
            },
            timeout=timeout,
        )

    async def respond_permission(
        self,
        session_key: str,
        approval_id: str,
        *,
        allow: bool,
        use_suggestions: bool = False,
        updated_input: Optional[dict] = None,
        message: str = "",
        interrupt: bool = False,
    ) -> dict:
        payload = {
            "sessionKey": session_key,
            "approvalId": approval_id,
            "allow": bool(allow),
            "useSuggestions": bool(use_suggestions),
            "message": message,
            "interrupt": bool(interrupt),
        }
        if updated_input is not None:
            payload["updatedInput"] = updated_input
        return await self.request("permission_response", payload, timeout=15.0)

    async def close_session(self, session_key: str) -> None:
        await self.request("close_session", {"sessionKey": session_key})

    async def restart(self, reason: str = "Claude Agent SDK bridge restart requested") -> bool:
        """Replace the daemon and all in-memory runtimes with a clean bridge.

        Native Claude sessions remain persisted by the SDK and are resumed on
        the next request. The method deliberately never replays a user turn.
        """
        self.last_error = reason
        self._transport_failed = True
        async with self._start_lock:
            await self._start()
            return self.running

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "process_alive": self.process_alive,
            "reader_alive": self._reader_task is not None and not self._reader_task.done(),
            "protocol": BRIDGE_PROTOCOL_VERSION,
            "sdk": self.sdk_info,
            "error": self.last_error or None,
        }

    async def _terminate_process(self, *, expected_process: Optional[asyncio.subprocess.Process] = None) -> None:
        async with self._terminate_lock:
            if expected_process is not None and self.process is not expected_process:
                return
            process = self.process
            self.process = None
            self.sdk_info = None
            current = asyncio.current_task()
            cancelled_tasks = []
            for task in (self._reader_task, self._stderr_task):
                if task is not None and task is not current and not task.done():
                    task.cancel()
                    cancelled_tasks.append(task)
            self._reader_task = None
            self._stderr_task = None
            if cancelled_tasks:
                # Do not let an old reader's finally block race with the next
                # daemon's _ready future or event queues.
                await asyncio.gather(*cancelled_tasks, return_exceptions=True)
            if process is None or process.returncode is not None:
                return
            try:
                process.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    return
                await process.wait()

    async def shutdown(self) -> None:
        self._stopping = True
        if self.running:
            try:
                await self.request("shutdown", timeout=2.0)
            except Exception:
                pass
        await self._terminate_process()
