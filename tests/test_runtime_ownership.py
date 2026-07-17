import asyncio
import os
import tempfile
import unittest
import uuid
import time
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from claude_web import server
from claude_web.agent_sdk_bridge import AgentSdkTurn


class RuntimeOwnershipTest(unittest.IsolatedAsyncioTestCase):
    def _cleanup_session(self, session_id):
        server._agent_sdk_running_sessions.discard(session_id)
        server._agent_sdk_detached_turn_tasks.pop(session_id, None)
        server._code_validation_processes.pop(session_id, None)
        server._code_validation_stop_requests.discard(session_id)
        server.save_events(session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def test_omitted_workspace_mode_cannot_route_sdk_session_to_cli(self):
        session_id = "runtime-owner-" + uuid.uuid4().hex
        server.upsert_session(session_id, "owner", tempfile.gettempdir() + "/owned-code-project", "code")
        server.set_session_remote_state(session_id, "native-owner", True)
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        try:
            with patch.dict(os.environ, {"CLAUDE_WEB_CODE_RUNTIME": "cli"}):
                with self.assertRaises(HTTPException) as raised:
                    await server._chat_response(server.ChatRequest(message="continue", session_id=session_id))
            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("owned by Claude Agent SDK", str(raised.exception.detail))
            self.assertEqual([], server.load_events(session_id))
        finally:
            self._cleanup_session(session_id)

    async def test_unacknowledged_sdk_turn_restarts_bridge_without_cli_replay(self):
        session_id = "runtime-timeout-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-timeout-project"
        server.upsert_session(session_id, "timeout", cwd, "code")
        checkpoint = {"kind": "git", "ref": "test-checkpoint"}
        try:
            with patch.dict(os.environ, {"CLAUDE_WEB_CODE_RUNTIME": "agent-sdk"}), \
                    patch.object(server, "create_git_checkpoint", AsyncMock(return_value=checkpoint)), \
                    patch.object(server, "git_dirty_signatures", AsyncMock(return_value={})), \
                    patch.object(server, "discard_git_checkpoint", AsyncMock()) as discard, \
                    patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                    patch.object(
                        server._claude_agent_bridge,
                        "open_turn",
                        AsyncMock(side_effect=asyncio.TimeoutError),
                    ), \
                    patch.object(server._claude_agent_bridge, "restart", AsyncMock(return_value=True)) as restart, \
                    patch.object(server._claude_agent_bridge, "close_session", AsyncMock()) as close_session:
                with self.assertRaises(HTTPException) as raised:
                    await server._chat_response(
                        server.ChatRequest(
                            message="run once",
                            session_id=session_id,
                            cwd=cwd,
                            workspace_mode="code",
                        )
                    )
            self.assertEqual(504, raised.exception.status_code)
            self.assertIn("without replaying", str(raised.exception.detail))
            restart.assert_awaited_once()
            close_session.assert_not_awaited()
            discard.assert_awaited_once_with(checkpoint, cwd)
            self.assertEqual([], server.load_events(session_id))
        finally:
            self._cleanup_session(session_id)

    async def test_agent_sdk_install_endpoint_forwards_selected_version(self):
        request = Request({
            "type": "http", "method": "POST", "path": "/api/agent-sdk/install", "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        selected = "0.2.111"
        with tempfile.TemporaryDirectory() as temp:
            staging = server.Path(temp) / "staging"
            staging.mkdir()
            with patch.dict(os.environ, {"CLAUDE_WEB_CODE_RUNTIME": "agent-sdk"}), \
                    patch.object(server, "_is_local_request", return_value=True), \
                    patch.object(
                        server,
                        "install_version",
                        AsyncMock(
                            return_value={
                                "staging": staging,
                                "version": selected,
                                "recommended": False,
                            }
                        ),
                    ) as install, \
                    patch.object(server._claude_agent_bridge, "shutdown", AsyncMock()), \
                    patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                    patch.object(server._claude_agent_bridge, "sdk_info", {"version": selected}), \
                    patch.object(server, "activate_staging", return_value=None), \
                    patch.object(server, "discard_backup"), \
                    patch.object(
                        server,
                        "_agent_sdk_management_status",
                        return_value={"active_version": selected},
                    ):
                result = await server.install_agent_sdk(
                    request,
                    server.AgentSdkInstallRequest(version=f"v{selected}"),
                )
        install.assert_awaited_once_with(selected)
        self.assertTrue(result["ok"])
        self.assertEqual(selected, result["installed_version"])
        self.assertFalse(result["recommended"])

    async def test_agent_sdk_install_endpoint_rejects_non_semver_target(self):
        request = Request({
            "type": "http", "method": "POST", "path": "/api/agent-sdk/install", "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        with patch.object(server, "_is_local_request", return_value=True), \
                patch.object(server, "install_version", AsyncMock()) as install:
            with self.assertRaises(HTTPException) as raised:
                await server.install_agent_sdk(
                    request,
                    server.AgentSdkInstallRequest(version="latest --force"),
                )
        self.assertEqual(400, raised.exception.status_code)
        install.assert_not_awaited()

    async def test_agent_sdk_install_rolls_back_when_bridge_loads_a_different_version(self):
        request = Request({
            "type": "http", "method": "POST", "path": "/api/agent-sdk/install", "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        selected = "0.2.111"
        with tempfile.TemporaryDirectory() as temp:
            staging = server.Path(temp) / "staging"
            staging.mkdir()
            backup = server.Path(temp) / "backup"
            backup.mkdir()
            with patch.dict(os.environ, {"CLAUDE_WEB_CODE_RUNTIME": "agent-sdk"}), \
                    patch.object(server, "_is_local_request", return_value=True), \
                    patch.object(
                        server,
                        "install_version",
                        AsyncMock(
                            return_value={
                                "staging": staging,
                                "version": selected,
                                "recommended": False,
                            }
                        ),
                    ), \
                    patch.object(server._claude_agent_bridge, "shutdown", AsyncMock()), \
                    patch.object(
                        server._claude_agent_bridge,
                        "ensure_started",
                        AsyncMock(side_effect=[True, True]),
                    ), \
                    patch.object(server._claude_agent_bridge, "sdk_info", {"version": "0.2.112"}), \
                    patch.object(server, "activate_staging", return_value=backup), \
                    patch.object(server, "rollback_activation") as rollback, \
                    patch.object(server, "discard_backup"):
                with self.assertRaises(HTTPException) as raised:
                    await server.install_agent_sdk(
                        request,
                        server.AgentSdkInstallRequest(version=selected),
                    )
        self.assertEqual(502, raised.exception.status_code)
        self.assertIn("failed activation verification", str(raised.exception.detail))
        rollback.assert_called_once_with(backup)

    async def test_running_agent_loop_owns_session_between_turns(self):
        session_id = "loop-owner-" + uuid.uuid4().hex
        job_id = "job-" + uuid.uuid4().hex
        server.upsert_session(session_id, "loop", tempfile.gettempdir() + "/loop-project", "code")
        server._agent_loop_jobs[job_id] = server.AgentLoopJob(
            id=job_id,
            session_id=session_id,
            created_at=time.time(),
            updated_at=time.time(),
        )
        try:
            self.assertTrue(server._session_control_busy(session_id))
            with self.assertRaises(HTTPException) as raised:
                await server._chat_response(server.ChatRequest(message="race", session_id=session_id))
            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("Agent Loop", str(raised.exception.detail))
        finally:
            server._agent_loop_jobs.pop(job_id, None)
            self._cleanup_session(session_id)

    async def test_agent_loop_budget_ignores_existing_context_and_cache(self):
        usage = {
            "input_tokens": 190_000,
            "cache_read_input_tokens": 180_000,
            "cache_creation_input_tokens": 10_000,
            "output_tokens": 321,
        }
        # Two CJK characters plus four ASCII characters are estimated as three
        # newly submitted tokens; the 380k existing/cache input is not charged.
        self.assertEqual(324, server._agent_loop_usage_total(usage, "abcd中文"))

    async def test_validation_autodetect_falls_back_to_stdlib_unittest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = server.Path(temp_dir)
            (root / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text("import unittest\n", encoding="utf-8")
            with patch.object(server.shutil, "which", return_value=None):
                command, source = server._agent_loop_detect_test_command(temp_dir)
            self.assertIn("-m unittest discover -s tests", command)
            self.assertEqual("unittest project", source)

    async def test_native_rewind_applies_persisted_fork_offset(self):
        session_id = "native-offset-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-offset-project"
        server.upsert_session(session_id, "offset", cwd, "code")
        server.set_session_remote_state(session_id, "native-offset-source", True)
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.set_session_native_user_offset(session_id, 2)
        transcript = [
            {
                "type": "user",
                "uuid": f"000000000000000{index}",
                "message": {"content": [{"type": "text", "text": str(index)}]},
            }
            for index in range(4)
        ]
        try:
            with patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                    patch.object(server._claude_agent_bridge, "session_messages", AsyncMock(return_value=transcript)), \
                    patch.object(
                        server._claude_agent_bridge,
                        "rewind_files",
                        AsyncMock(return_value={"result": {"canRewind": True, "filesChanged": []}}),
                    ) as rewind:
                result = await server.rewind_agent_sdk_files(
                    session_id,
                    server.NativeRewindRequest(event_index=1, dry_run=True),
                )
            self.assertTrue(result["ok"])
            self.assertEqual("0000000000000003", rewind.await_args.args[1])
            self.assertEqual([], server.load_events(session_id))

            with patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                    patch.object(server._claude_agent_bridge, "session_messages", AsyncMock(return_value=transcript)), \
                    patch.object(
                        server._claude_agent_bridge,
                        "rewind_files",
                        AsyncMock(return_value={"result": {"canRewind": True, "filesChanged": ["tracked.txt"]}}),
                    ):
                applied = await server.rewind_agent_sdk_files(
                    session_id,
                    server.NativeRewindRequest(event_index=1, dry_run=False),
                )
            self.assertEqual("code_rewind", applied["rewind_event"]["type"])
            self.assertEqual("code_rewind", server.load_events(session_id)[0]["type"])
        finally:
            self._cleanup_session(session_id)

    async def test_manual_validation_is_sdk_code_only_and_owns_runtime(self):
        session_id = "validation-owner-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/validation-project"
        server.upsert_session(session_id, "validation", cwd, "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)

        async def fake_validation(command, actual_cwd, timeout, on_process=None):
            self.assertEqual("npm test", command)
            self.assertEqual(cwd, actual_cwd)
            self.assertTrue(server._session_control_busy(session_id))
            return {
                "command": command,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
                "timed_out": False,
                "duration_ms": 12,
            }

        try:
            with patch.object(server, "_run_validation_command", side_effect=fake_validation):
                result = await server.validate_code_session(
                    session_id,
                    server.CodeValidationRequest(command="npm test", timeout=30),
                )
            self.assertTrue(result["ok"])
            self.assertEqual("code_validation", result["event"]["type"])
            self.assertNotIn(session_id, server._code_validation_processes)
            self.assertEqual("code_validation", server.load_events(session_id)[0]["type"])
        finally:
            self._cleanup_session(session_id)

    async def test_manual_validation_rejects_chat_session(self):
        session_id = "validation-chat-" + uuid.uuid4().hex
        server.upsert_session(session_id, "chat", os.path.expanduser("~"), "chat")
        try:
            with self.assertRaises(HTTPException) as raised:
                await server.validate_code_session(
                    session_id,
                    server.CodeValidationRequest(command="true"),
                )
            self.assertEqual(409, raised.exception.status_code)
        finally:
            self._cleanup_session(session_id)

    async def test_validation_stop_request_before_process_spawn_is_honored(self):
        session_id = "validation-stop-race-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/validation-stop-race-project"
        server.upsert_session(session_id, "validation stop", cwd, "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)

        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False

            def terminate(self):
                self.terminated = True
                self.returncode = -15

        process = FakeProcess()

        async def fake_validation(command, actual_cwd, timeout, on_process=None):
            stopped = await server.stop_chat(session_id)
            self.assertEqual("code_validation", stopped["runtime"])
            self.assertIsNone(server._code_validation_processes[session_id])
            on_process(process)
            self.assertTrue(process.terminated)
            return {
                "command": command,
                "returncode": process.returncode,
                "stdout": "",
                "stderr": "stopped",
                "timed_out": False,
                "duration_ms": 1,
            }

        try:
            with patch.object(server, "_run_validation_command", side_effect=fake_validation):
                result = await server.validate_code_session(
                    session_id,
                    server.CodeValidationRequest(command="npm test"),
                )
            self.assertEqual(-15, result["event"]["returncode"])
            self.assertNotIn(session_id, server._code_validation_processes)
            self.assertNotIn(session_id, server._code_validation_stop_requests)
        finally:
            self._cleanup_session(session_id)

    async def test_native_fork_persists_transcript_offset(self):
        session_id = "native-fork-source-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-fork-project"
        server.upsert_session(session_id, "fork", cwd, "code")
        server.set_session_remote_state(session_id, "native-fork-source", True)
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.set_session_native_user_offset(session_id, 3)
        server.save_events(session_id, [
            {"type": "user_input", "text": "local zero", "ts": time.time()},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {"type": "user_input", "text": "local one", "ts": time.time()},
        ])
        transcript = [
            {
                "type": "user",
                "uuid": f"native-user-{index}",
                "message": {"content": [{"type": "text", "text": str(index)}]},
            }
            for index in range(5)
        ]
        request = Request({
            "type": "http", "method": "POST", "path": "/", "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        forked_session_id = ""
        try:
            with patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                    patch.object(server._claude_agent_bridge, "session_messages", AsyncMock(return_value=transcript)), \
                    patch.object(
                        server._claude_agent_bridge,
                        "fork_session",
                        AsyncMock(return_value={"sessionId": "native-fork-result"}),
                    ) as fork_session:
                result = await server.prepare_fork(
                    request,
                    session_id,
                    server.ForkRequest(event_index=1, new_text="branched"),
                )
            forked_session_id = result["session_id"]
            self.assertTrue(result["native_fork"])
            self.assertEqual("native-user-3", fork_session.await_args.kwargs["up_to_message_id"])
            with server.db_connect() as conn:
                row = conn.execute(
                    "SELECT remote_session_id, runtime_origin, native_user_offset FROM sessions WHERE id = ?",
                    (forked_session_id,),
                ).fetchone()
            self.assertEqual("native-fork-result", row["remote_session_id"])
            self.assertEqual(server._RUNTIME_ORIGIN_AGENT_SDK, row["runtime_origin"])
            self.assertEqual(4, row["native_user_offset"])
        finally:
            self._cleanup_session(session_id)
            if forked_session_id:
                self._cleanup_session(forked_session_id)

    async def test_closing_sse_detaches_and_drains_native_turn(self):
        session_id = "native-detach-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-detach-project"
        server.upsert_session(session_id, "detach", cwd, "code")
        queue = asyncio.Queue()
        turn = AgentSdkTurn("turn-detach", session_id, queue)
        server._agent_sdk_running_sessions.add(session_id)
        response = server._agent_sdk_streaming_response(
            turn=turn,
            session_id=session_id,
            remote_session_id="native-detach-requested",
            remote_ready=False,
            work_dir=cwd,
            display_text="continue",
            checkpoint=None,
            git_dirty_before={},
            workspace_mode="code",
        )
        iterator = response.body_iterator
        try:
            meta = await iterator.__anext__()
            self.assertIn("claude_agent_sdk", meta)
            await iterator.aclose()
            self.assertIn(session_id, server._agent_sdk_detached_turn_tasks)
            await queue.put({"type": "done", "sessionId": "native-detach-finished"})
            await asyncio.wait_for(server._agent_sdk_detached_turn_tasks[session_id], timeout=2)
            with server.db_connect() as conn:
                row = conn.execute(
                    "SELECT remote_session_id, remote_ready FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            self.assertEqual("native-detach-finished", row["remote_session_id"])
            self.assertTrue(row["remote_ready"])
            self.assertNotIn(session_id, server._agent_sdk_running_sessions)
        finally:
            task = server._agent_sdk_detached_turn_tasks.get(session_id)
            if task and not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self._cleanup_session(session_id)


if __name__ == "__main__":
    unittest.main()
