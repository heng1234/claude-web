import asyncio
import json
import os
import tempfile
import unittest
import uuid
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from claude_web import server
from claude_web.agent_sdk_bridge import AgentSdkTurn


class RuntimeOwnershipTest(unittest.IsolatedAsyncioTestCase):
    def _cleanup_session(self, session_id):
        server._agent_sdk_running_sessions.discard(session_id)
        server._agent_sdk_detached_turn_tasks.pop(session_id, None)
        server._agent_sdk_context_usage_cache.pop(session_id, None)
        server._stopped_sessions.discard(session_id)
        server._plan_waiting_sessions.discard(session_id)
        server._code_validation_processes.pop(session_id, None)
        server._code_validation_stop_requests.discard(session_id)
        server.save_events(session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def test_context_usage_resolves_local_1m_default_model_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude_dir = Path(tmp) / ".claude"
            claude_dir.mkdir()
            (claude_dir / "settings.json").write_text(
                json.dumps({
                    "model": "claude-opus-5",
                    "env": {
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5[1M]",
                        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "claude-opus-5",
                    },
                }),
                encoding="utf-8",
            )
            with patch.object(server.Path, "home", return_value=Path(tmp)):
                usage = server._agent_sdk_context_usage_with_model(
                    {"totalTokens": 100_000, "maxTokens": 200_000},
                    None,
                )
        self.assertEqual("claude-opus-5[1M]", usage["model"])
        self.assertEqual(200_000, usage["maxTokens"])

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

    async def test_existing_session_rejects_cross_mode_takeover_before_appending_events(self):
        session_id = "runtime-mode-boundary-" + uuid.uuid4().hex
        server.upsert_session(session_id, "mode boundary", tempfile.gettempdir(), "code")
        try:
            with self.assertRaises(HTTPException) as raised:
                await server._chat_response(
                    server.ChatRequest(
                        message="ordinary chat must not take over this Code session",
                        session_id=session_id,
                        workspace_mode="chat",
                    )
                )
            self.assertEqual(409, raised.exception.status_code)
            self.assertIn("session mode mismatch", str(raised.exception.detail))
            self.assertEqual([], server.load_events(session_id))
        finally:
            self._cleanup_session(session_id)

    async def test_explicit_chat_mode_is_not_reclassified_by_project_cwd(self):
        session_id = "runtime-chat-project-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/ordinary-chat-project"
        server.upsert_session(session_id, "chat project", cwd, "chat")
        try:
            with server.db_connect() as conn:
                row = conn.execute(
                    "SELECT workspace_mode, cwd FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            self.assertEqual("chat", row["workspace_mode"])
            self.assertEqual(cwd, row["cwd"])
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

    async def test_reconnect_rebuilds_only_the_requested_sdk_session_without_replay(self):
        session_id = "runtime-reconnect-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-reconnect-project"
        server.upsert_session(session_id, "reconnect", cwd, "code")
        server.set_session_remote_state(session_id, "native-reconnect-session", True)
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        try:
            with patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                    patch.object(
                        server._claude_agent_bridge,
                        "reconnect_session",
                        AsyncMock(return_value={"ok": True, "reconnected": True}),
                    ) as reconnect:
                result = await server.reconnect_agent_sdk_session(
                    session_id,
                    server.NativeCompactRequest(
                        model="opus",
                        effort="high",
                        permission_mode="acceptEdits",
                        allowed_tools=["Read", "Edit"],
                    ),
                )
            self.assertTrue(result["ok"])
            self.assertEqual("native-reconnect-session", result["remote_session_id"])
            reconnect.assert_awaited_once()
            params = reconnect.await_args.args[1]
            self.assertEqual("native-reconnect-session", params["resumeSessionId"])
            self.assertNotIn("content", params)
            self.assertEqual([], server.load_events(session_id))
        finally:
            self._cleanup_session(session_id)

    async def test_context_usage_returns_cached_stale_value_during_active_turn(self):
        session_id = "runtime-context-busy-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-context-busy-project"
        server.upsert_session(session_id, "context", cwd, "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server._agent_sdk_running_sessions.add(session_id)
        server._agent_sdk_context_usage_cache[session_id] = {
            "totalTokens": 1200,
            "maxTokens": 200000,
        }
        try:
            with patch.object(server._claude_agent_bridge, "context_usage", AsyncMock()) as context_usage:
                result = await server.agent_sdk_context_usage(
                    session_id, None, None, None, None, None
                )
            self.assertTrue(result["stale"])
            self.assertTrue(result["available"])
            self.assertEqual(1200, result["totalTokens"])
            self.assertEqual(200000, result["maxTokens"])
            context_usage.assert_not_awaited()
        finally:
            self._cleanup_session(session_id)

    async def test_context_usage_runtime_race_falls_back_to_cached_stale_value(self):
        session_id = "runtime-context-race-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-context-race-project"
        server.upsert_session(session_id, "context", cwd, "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server._agent_sdk_context_usage_cache[session_id] = {
            "totalTokens": 2400,
            "maxTokens": 200000,
        }
        try:
            with patch.object(
                server._claude_agent_bridge,
                "context_usage",
                AsyncMock(side_effect=server.AgentSdkBridgeError("runtime is active")),
            ):
                result = await server.agent_sdk_context_usage(
                    session_id, None, None, None, None, None
                )
            self.assertTrue(result["stale"])
            self.assertTrue(result["available"])
            self.assertEqual(2400, result["totalTokens"])
            self.assertEqual(200000, result["maxTokens"])
        finally:
            self._cleanup_session(session_id)

    async def test_agent_sdk_stop_is_idempotent_and_records_one_terminal_event(self):
        session_id = "runtime-stop-idempotent-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-stop-idempotent-project"
        server.upsert_session(session_id, "stop", cwd, "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server._agent_sdk_running_sessions.add(session_id)
        try:
            with patch.object(
                server._claude_agent_bridge,
                "interrupt",
                AsyncMock(return_value={"ok": True}),
            ) as interrupt:
                first = await server.stop_chat(session_id)
                second = await server.stop_chat(session_id)
            self.assertTrue(first["ok"])
            self.assertTrue(second["already_stopping"])
            interrupt.assert_awaited_once_with(session_id)
            stops = [
                event for event in server.load_events(session_id)
                if event.get("type") == "system" and event.get("subtype") == "stopped"
            ]
            self.assertEqual(1, len(stops))
        finally:
            self._cleanup_session(session_id)

    async def test_agent_sdk_stop_failure_keeps_runtime_ownership(self):
        session_id = "runtime-stop-failed-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-stop-failed-project"
        server.upsert_session(session_id, "stop failed", cwd, "code")
        server._agent_sdk_running_sessions.add(session_id)
        try:
            with patch.object(
                server._claude_agent_bridge,
                "interrupt",
                AsyncMock(side_effect=server.AgentSdkBridgeError("interrupt failed")),
            ), patch.object(
                server._claude_agent_bridge,
                "close_session",
                AsyncMock(side_effect=server.AgentSdkBridgeError("close failed")),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await server.stop_chat(session_id)
            self.assertEqual(503, raised.exception.status_code)
            self.assertIn(session_id, server._agent_sdk_running_sessions)
            self.assertNotIn(session_id, server._stopped_sessions)
            self.assertFalse(any(
                event.get("subtype") == "stopped"
                for event in server.load_events(session_id)
            ))
        finally:
            self._cleanup_session(session_id)

    async def test_plan_ready_stop_records_waiting_state_without_user_error(self):
        session_id = "runtime-plan-ready-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/sdk-plan-ready-project"
        turn_id = "turn-" + uuid.uuid4().hex
        server.upsert_session(session_id, "plan", cwd, "code")
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.append_event(
            session_id,
            {"type": "user_input", "text": "make a plan", "turn_id": turn_id},
        )
        server._agent_sdk_running_sessions.add(session_id)
        try:
            with patch.object(
                server._claude_agent_bridge,
                "interrupt",
                AsyncMock(return_value={"ok": True}),
            ) as interrupt:
                result = await server.stop_chat(session_id, reason="plan_ready")
            self.assertEqual("plan_ready", result["reason"])
            interrupt.assert_awaited_once_with(session_id)
            events = server.load_events(session_id)
            self.assertFalse(any(event.get("message") == "用户中止" for event in events))
            plan_events = [
                event for event in events
                if event.get("type") == "system" and event.get("subtype") == "plan_ready"
            ]
            self.assertEqual(1, len(plan_events))
            self.assertEqual(turn_id, plan_events[0]["turn_id"])

            server._agent_sdk_running_sessions.discard(session_id)
            server._stopped_sessions.discard(session_id)
            self.assertEqual("waiting_plan", server._agent_sdk_turn_state(session_id)["state"])
        finally:
            self._cleanup_session(session_id)

    def test_agent_sdk_contradictory_success_error_uses_reported_api_error(self):
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "errors": [],
        }
        normalized = server._normalize_agent_sdk_result(
            result,
            "API Error: Stream idle timeout - no chunks received",
        )
        self.assertEqual("error_during_execution", normalized["subtype"])
        self.assertEqual("success", normalized["reported_subtype"])
        self.assertEqual(
            "API Error: Stream idle timeout - no chunks received",
            normalized["error"],
        )

    def test_transport_error_text_is_not_rendered_as_duplicate_assistant_message(self):
        message = "API Error: Connection closed mid-response. The response above may be incomplete."
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": message}]},
        }
        self.assertEqual(message, server._agent_sdk_reported_api_error(event))
        self.assertIsNone(server._strip_agent_sdk_api_error_text(event, message))

        mixed = {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "已经完成的部分"},
                {"type": "text", "text": message},
            ]},
        }
        cleaned = server._strip_agent_sdk_api_error_text(mixed, message)
        self.assertEqual("已经完成的部分", server._agent_sdk_assistant_text(cleaned))

    def test_abort_diagnostic_and_missing_final_text_are_normalized(self):
        abort_result = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "terminal_reason": "aborted_streaming",
            "errors": ["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
        }
        self.assertTrue(server._agent_sdk_abort_diagnostic(abort_result))

        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "现在根因很清楚了。让我给你最终总结：\n\n完整结论",
        }
        recovered = server._agent_sdk_final_text_event(
            result,
            "现在根因很清楚了。让我给你最终总结：",
            "local-session",
            "turn-1",
        )
        self.assertTrue(recovered["recovered_final"])
        self.assertEqual("完整结论", recovered["message"]["content"][0]["text"])

    async def test_transport_failure_auto_reconnects_without_replaying_content(self):
        session_id = "runtime-auto-reconnect-" + uuid.uuid4().hex
        remote_id = "native-auto-reconnect-" + uuid.uuid4().hex
        statuses = []

        async def on_status(attempt, maximum, restored, error):
            statuses.append((attempt, maximum, restored, error))

        with patch.object(
            server._claude_agent_bridge,
            "reconnect_session",
            AsyncMock(side_effect=[
                asyncio.TimeoutError(),
                {"ok": True, "reconnected": True},
            ]),
        ) as reconnect, patch.object(server.asyncio, "sleep", AsyncMock()) as sleep:
            restored, error = await server._auto_reconnect_agent_sdk_session(
                session_id,
                remote_id,
                {
                    "content": [{"type": "text", "text": "do not replay"}],
                    "model": "opus",
                    "permissionMode": "acceptEdits",
                },
                on_status=on_status,
            )

        self.assertTrue(restored)
        self.assertEqual("", error)
        self.assertEqual(2, reconnect.await_count)
        sent_params = reconnect.await_args.args[1]
        self.assertNotIn("content", sent_params)
        self.assertEqual(remote_id, sent_params["resumeSessionId"])
        self.assertEqual(remote_id, sent_params["runtimeEpoch"])
        sleep.assert_awaited_once_with(1.0)
        self.assertEqual((2, 5, True, ""), statuses[-1])

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
                    patch.object(server, "confirm_pending_activation"), \
                    patch.object(server, "mark_activation_pending") as mark_pending, \
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
        mark_pending.assert_called_once_with(None, selected)

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
                    patch.object(server, "confirm_pending_activation"), \
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

    async def test_successful_code_turn_confirms_pending_sdk_activation(self):
        with patch.object(server, "pending_activation", return_value={"version": "0.3.0"}), \
                patch.object(server, "confirm_pending_activation") as confirm:
            state = await server._settle_pending_agent_sdk_activation(
                success=True,
                session_id="session-a",
            )
        self.assertEqual("validated", state)
        confirm.assert_called_once_with()

    async def test_sdk_compatibility_failure_restores_pending_activation(self):
        server._agent_sdk_running_sessions.clear()
        with patch.object(server, "pending_activation", return_value={"version": "0.3.0"}), \
                patch.object(server._claude_agent_bridge, "shutdown", AsyncMock()) as shutdown, \
                patch.object(server, "rollback_pending_activation", return_value={"version": "0.3.0"}) as rollback, \
                patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)):
            state = await server._settle_pending_agent_sdk_activation(
                success=False,
                error="TypeError: query is not a function",
                session_id="session-a",
            )
        self.assertEqual("rolled_back", state)
        shutdown.assert_awaited_once_with()
        rollback.assert_called_once_with()

    async def test_ordinary_sdk_failure_keeps_pending_backup(self):
        with patch.object(server, "pending_activation", return_value={"version": "0.3.0"}), \
                patch.object(server, "rollback_pending_activation") as rollback:
            state = await server._settle_pending_agent_sdk_activation(
                success=False,
                error="authentication expired",
                session_id="session-a",
            )
        self.assertEqual("pending", state)
        rollback.assert_not_called()

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

    async def test_prepare_fork_preserves_original_images_docs_and_full_prompt(self):
        session_id = "attachment-fork-source-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/attachment-chat-project"
        server.upsert_session(session_id, "attachment fork", cwd, "chat")
        server.save_events(session_id, [{
            "type": "user_input",
            "text": "分析附件",
            "full_text": "【文档: spec.pdf】\n---\n关键规格\n---\n\n分析附件",
            "images": ["/tmp/screenshot.png"],
            "docs": [{"name": "spec.pdf", "path": "/tmp/spec.pdf", "size": 42}],
            "ts": time.time(),
        }])
        request = Request({
            "type": "http", "method": "POST", "path": "/", "headers": [],
            "client": ("127.0.0.1", 12345),
        })
        forked_session_id = ""
        try:
            result = await server.prepare_fork(
                request,
                session_id,
                server.ForkRequest(event_index=0),
            )
            forked_session_id = result["session_id"]
            self.assertEqual(["/tmp/screenshot.png"], result["images"])
            self.assertEqual("spec.pdf", result["docs"][0]["name"])
            self.assertIn("关键规格", result["sent_message"])
            with server.db_connect() as conn:
                row = conn.execute(
                    "SELECT workspace_mode FROM sessions WHERE id = ?",
                    (forked_session_id,),
                ).fetchone()
            self.assertEqual("chat", row["workspace_mode"])
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
            await queue.put({
                "type": "event",
                "event": {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "native-detach-finished",
                    "usage": {},
                },
            })
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

    async def test_detached_final_recovery_keeps_streamed_prefix_once(self):
        session_id = "native-detach-prefix-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-detach-prefix-project"
        turn_id = "turn-" + uuid.uuid4().hex
        server.upsert_session(session_id, "detach prefix", cwd, "code")
        server.save_events(session_id, [{"type": "user_input", "text": "finish", "turn_id": turn_id}])
        queue = asyncio.Queue()
        turn = AgentSdkTurn("turn-detach-prefix", session_id, queue)
        server._agent_sdk_running_sessions.add(session_id)
        response = server._agent_sdk_streaming_response(
            turn=turn,
            session_id=session_id,
            remote_session_id="native-detach-prefix",
            remote_ready=True,
            work_dir=cwd,
            display_text="finish",
            checkpoint=None,
            git_dirty_before={},
            workspace_mode="code",
            turn_id=turn_id,
        )
        iterator = response.body_iterator
        try:
            await iterator.__anext__()
            await queue.put({
                "type": "event",
                "event": {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "最终总结："}]},
                },
            })
            streamed = await iterator.__anext__()
            self.assertIn("最终总结", streamed)
            await iterator.aclose()
            await queue.put({
                "type": "event",
                "event": {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "最终总结：\n\n完整内容",
                    "usage": {},
                },
            })
            await queue.put({"type": "done", "sessionId": "native-detach-prefix"})
            await asyncio.wait_for(server._agent_sdk_detached_turn_tasks[session_id], timeout=2)
            assistant_text = [
                server._agent_sdk_assistant_text(event)
                for event in server.load_events(session_id)
                if event.get("type") == "assistant"
            ]
            self.assertEqual(["最终总结：", "完整内容"], assistant_text)
        finally:
            task = server._agent_sdk_detached_turn_tasks.get(session_id)
            if task and not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self._cleanup_session(session_id)

    async def test_native_turn_state_keeps_runtime_busy_until_persisted_terminal_event(self):
        session_id = "native-state-" + uuid.uuid4().hex
        server.upsert_session(session_id, "state", tempfile.gettempdir(), "code")
        try:
            server.save_events(session_id, [{"type": "user_input", "text": "work"}])
            self.assertEqual("incomplete", server._agent_sdk_turn_state(session_id)["state"])

            server._agent_sdk_running_sessions.add(session_id)
            self.assertEqual("running", server._agent_sdk_turn_state(session_id)["state"])
            server._agent_sdk_running_sessions.discard(session_id)

            server.append_event(session_id, {"type": "result", "subtype": "success", "usage": {}})
            state = server._agent_sdk_turn_state(session_id)
            self.assertEqual("completed", state["state"])
            self.assertEqual("result", state["terminal_event_type"])
        finally:
            self._cleanup_session(session_id)

    async def test_plan_wait_suppresses_interrupt_result_and_finishes_as_waiting(self):
        session_id = "native-plan-wait-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-plan-wait-project"
        turn_id = "turn-" + uuid.uuid4().hex
        server.upsert_session(session_id, "plan wait", cwd, "code")
        server.save_events(
            session_id,
            [
                {"type": "user_input", "text": "plan", "turn_id": turn_id},
                {
                    "type": "system",
                    "subtype": "plan_ready",
                    "message": "计划已就绪，等待审批",
                    "turn_id": turn_id,
                },
            ],
        )
        queue = asyncio.Queue()
        turn = AgentSdkTurn("turn-plan-wait", session_id, queue)
        await queue.put({
            "type": "event",
            "event": {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": "User interrupted",
                "usage": {},
            },
        })
        await queue.put({"type": "done", "success": False, "sessionId": "native-plan-wait"})
        server._agent_sdk_running_sessions.add(session_id)
        server._plan_waiting_sessions.add(session_id)
        response = server._agent_sdk_streaming_response(
            turn=turn,
            session_id=session_id,
            remote_session_id="native-plan-wait",
            remote_ready=True,
            work_dir=cwd,
            display_text="plan",
            checkpoint=None,
            git_dirty_before={},
            workspace_mode="code",
            turn_id=turn_id,
        )
        try:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            payload = "".join(chunks)
            self.assertNotIn("User interrupted", payload)
            self.assertIn('"turn_state": "waiting_plan"', payload)
            events = server.load_events(session_id)
            self.assertFalse(any(event.get("type") == "result" for event in events))
            self.assertEqual("waiting_plan", server._agent_sdk_turn_state(session_id)["state"])
        finally:
            self._cleanup_session(session_id)

    async def test_native_stream_synthesizes_terminal_error_if_daemon_done_has_no_result(self):
        session_id = "native-missing-result-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-missing-result-project"
        server.upsert_session(session_id, "missing", cwd, "code")
        server.save_events(session_id, [{"type": "user_input", "text": "work"}])
        queue = asyncio.Queue()
        turn = AgentSdkTurn("turn-missing", session_id, queue)
        await queue.put({"type": "done", "success": True, "sessionId": "native-missing"})
        server._agent_sdk_running_sessions.add(session_id)
        response = server._agent_sdk_streaming_response(
            turn=turn,
            session_id=session_id,
            remote_session_id="native-missing",
            remote_ready=True,
            work_dir=cwd,
            display_text="work",
            checkpoint=None,
            git_dirty_before={},
            workspace_mode="code",
        )
        try:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            payload = "".join(chunks)
            self.assertIn("runtime ended before returning a final result", payload)
            self.assertIn('"turn_state": "failed"', payload)
            self.assertEqual("failed", server._agent_sdk_turn_state(session_id)["state"])
        finally:
            self._cleanup_session(session_id)

    async def test_native_stream_recovers_final_result_text_missing_from_partial_messages(self):
        session_id = "native-final-recovery-" + uuid.uuid4().hex
        cwd = tempfile.gettempdir() + "/native-final-recovery-project"
        turn_id = "turn-" + uuid.uuid4().hex
        server.upsert_session(session_id, "final recovery", cwd, "code")
        server.save_events(
            session_id,
            [{"type": "user_input", "text": "summarize", "turn_id": turn_id}],
        )
        queue = asyncio.Queue()
        turn = AgentSdkTurn("turn-final-recovery", session_id, queue)
        await queue.put({
            "type": "event",
            "event": {
                "type": "assistant",
                "message": {
                    "id": "assistant-prefix",
                    "content": [{"type": "text", "text": "最终总结："}],
                },
            },
        })
        await queue.put({
            "type": "event",
            "event": {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "最终总结：\n\n完整内容",
                "usage": {},
            },
        })
        await queue.put({"type": "done", "success": True, "sessionId": "native-final-recovery"})
        server._agent_sdk_running_sessions.add(session_id)
        response = server._agent_sdk_streaming_response(
            turn=turn,
            session_id=session_id,
            remote_session_id="native-final-recovery",
            remote_ready=True,
            work_dir=cwd,
            display_text="summarize",
            checkpoint=None,
            git_dirty_before={},
            workspace_mode="code",
            turn_id=turn_id,
        )
        try:
            payload = "".join([chunk async for chunk in response.body_iterator])
            self.assertIn('"recovered_final": true', payload)
            self.assertIn("完整内容", payload)
            recovered = [
                event for event in server.load_events(session_id)
                if event.get("recovered_final") is True
            ]
            self.assertEqual(1, len(recovered))
            self.assertEqual("完整内容", recovered[0]["message"]["content"][0]["text"])
        finally:
            self._cleanup_session(session_id)


if __name__ == "__main__":
    unittest.main()
