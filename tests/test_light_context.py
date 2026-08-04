import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("CLAUDE_WEB_DATA_DIR", tempfile.mkdtemp(prefix="claude-web-light-context-test-"))

from claude_web import server  # noqa: E402


class LightContextTest(unittest.TestCase):
    def sample_events(self):
        return [
            {
                "type": "user_input",
                "text": "实现设置页，并保留最近几轮用户要求。",
                "docs": [{"path": "/tmp/spec.md"}],
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "PRIVATE_CHAIN_OF_THOUGHT"},
                        {"type": "text", "text": "决定沿用现有设置卡片。"},
                        {
                            "type": "tool_use",
                            "id": "read-1",
                            "name": "Read",
                            "input": {"file_path": "/repo/app.py", "offset": 19, "limit": 20},
                        },
                        {
                            "type": "tool_use",
                            "id": "edit-1",
                            "name": "Edit",
                            "input": {"file_path": "/repo/app.py"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-1",
                            "content": "关键配置位于第 20 行。\n" + ("R" * 5000),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "edit-1",
                            "content": "FULL_DIFF_SHOULD_NOT_SURVIVE\n" + ("D" * 5000),
                        },
                    ]
                },
            },
            {
                "type": "result",
                "changed_files": [{"path": "app.py", "status": "M"}],
            },
        ]

    def test_light_snippet_keeps_summaries_but_drops_raw_thinking_and_diff(self):
        snippet = server.format_light_context_snippet(self.sample_events(), max_chars=5000)

        self.assertIn("用户要求：实现设置页", snippet)
        self.assertIn("本轮附件：/tmp/spec.md", snippet)
        self.assertIn("读取 /repo/app.py L20-L39", snippet)
        self.assertIn("关键配置位于第 20 行", snippet)
        self.assertIn("修改摘要：M app.py", snippet)
        self.assertNotIn("PRIVATE_CHAIN_OF_THOUGHT", snippet)
        self.assertNotIn("FULL_DIFF_SHOULD_NOT_SURVIVE", snippet)
        self.assertNotIn("R" * 600, snippet)

    def test_resume_context_uses_compacted_summary_and_recent_requirements(self):
        events = [
            {
                "type": "user_input",
                "text": "【会话已压缩】\n- 目标：完成轻上下文模式",
                "compacted": True,
                "remote_detached": True,
            },
            {"type": "user_input", "text": "最近要求：开关默认开启。"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "DO_NOT_FORWARD"},
                        {"type": "text", "text": "已确认沿用 localStorage。"},
                    ]
                },
            },
        ]

        context = server.build_compacted_resume_context(events)

        self.assertIn("Code 轻上下文恢复", context)
        self.assertIn("目标：完成轻上下文模式", context)
        self.assertIn("最近要求：开关默认开启", context)
        self.assertIn("已确认沿用 localStorage", context)
        self.assertNotIn("DO_NOT_FORWARD", context)

    def test_detached_compaction_forces_a_fresh_remote_session(self):
        row = {"remote_session_id": "fresh-remote-id", "remote_ready": 0}
        events = [
            {
                "type": "user_input",
                "text": "summary",
                "compacted": True,
                "remote_detached": True,
            },
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}},
        ]

        remote_id, ready = server.resolve_remote_session_state("local-id", row, events)

        self.assertEqual("fresh-remote-id", remote_id)
        self.assertFalse(ready)


class LegacyChatCompactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_compaction_detaches_remote_and_keeps_recent_turn(self):
        session_id = "test-light-context-" + uuid.uuid4().hex
        events = [
            {"type": "user_input", "text": "第一轮目标"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "第一轮结论"}]},
            },
            {"type": "user_input", "text": "第二轮要求"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "第二轮结论"}]},
            },
        ]
        server.upsert_session(session_id, "聊天压缩测试", os.path.expanduser("~"), "chat")
        server.save_events(session_id, events)
        server.set_session_remote_state(session_id, "old-remote-id", True)
        request = SimpleNamespace(
            client=SimpleNamespace(host="127.0.0.1"),
            headers={},
            url=SimpleNamespace(path="/api/compact"),
        )
        fake_process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"- goal: keep working\n- changed: app.py", b"")),
            kill=lambda: None,
            wait=AsyncMock(return_value=0),
        )

        try:
            with patch.object(
                server.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=fake_process),
            ):
                result = await server.compact_session(request, session_id, keep_last=1)

            compacted = server.load_events(session_id)
            with server.db_connect() as conn:
                row = conn.execute(
                    "SELECT remote_session_id, remote_ready FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()

            self.assertTrue(result["ok"])
            self.assertFalse(result.get("skipped", False))
            self.assertTrue(compacted[0]["compacted"])
            self.assertTrue(compacted[0]["remote_detached"])
            self.assertEqual("light-v1", compacted[0]["context_strategy"])
            self.assertEqual("第二轮要求", compacted[1]["text"])
            self.assertNotEqual("old-remote-id", row["remote_session_id"])
            self.assertEqual(0, row["remote_ready"])

            remote_id, ready = server.resolve_remote_session_state(session_id, row, compacted)
            self.assertEqual(row["remote_session_id"], remote_id)
            self.assertFalse(ready)
            self.assertIn("goal: keep working", server.build_compacted_resume_context(compacted))
        finally:
            server.save_events(session_id, [])
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            for backup in server.iter_session_compact_backups(session_id):
                backup.unlink(missing_ok=True)


class _FakeNativeCompactTurn:
    async def events(self):
        yield {
            "type": "event",
            "event": {
                "type": "system",
                "subtype": "compact_boundary",
                "session_id": "native-compact-session",
                "compact_metadata": {"pre_tokens": 850_000, "post_tokens": 120_000},
            },
        }
        yield {
            "type": "event",
            "event": {
                "type": "result",
                "subtype": "success",
                "session_id": "native-compact-session",
                "usage": {"input_tokens": 120_000, "output_tokens": 10},
            },
        }
        yield {"type": "done", "sessionId": "native-compact-session"}


class _FakeNativeCompactFailureTurn:
    def __init__(self, result=None):
        self.result = result

    async def events(self):
        if self.result is not None:
            yield {"type": "event", "event": self.result}
        yield {"type": "done", "sessionId": "native-compact-session"}


class NativeCodeCompactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_code_compact_uses_native_control_endpoint_and_keeps_1m_limit(self):
        session_id = "test-native-compact-" + uuid.uuid4().hex
        server.upsert_session(session_id, "原生压缩测试", "/tmp/native-project", "code")
        server.set_session_remote_state(session_id, "native-compact-session", True)
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        server.save_events(session_id, [{"type": "user_input", "text": "保留这条用户要求"}])

        try:
            with patch.object(server._claude_agent_bridge, "ensure_started", new=AsyncMock(return_value=True)), patch.object(
                server._claude_agent_bridge,
                "open_turn",
                new=AsyncMock(return_value=_FakeNativeCompactTurn()),
            ) as open_turn, patch.object(
                server._claude_agent_bridge,
                "context_usage",
                new=AsyncMock(return_value={"usage": {"totalTokens": 120_000, "maxTokens": 1_000_000}}),
            ):
                result = await server.compact_agent_sdk_session(
                    session_id,
                    server.NativeCompactRequest(model="sonnet"),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(1_000_000, result["context_usage"]["maxTokens"])
            self.assertEqual(850_000, result["compact"]["pre_tokens"])
            params = open_turn.await_args.args[1]
            self.assertEqual("/compact", params["content"][0]["text"])
            stored = server.load_events(session_id)
            self.assertEqual("保留这条用户要求", stored[0]["text"])
            self.assertFalse(any(event.get("type") == "user_input" and event.get("text") == "/compact" for event in stored))
        finally:
            server.save_events(session_id, [])
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def test_native_compact_rejects_error_or_missing_result(self):
        for label, result_event in (
            ("missing", None),
            ("error", {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error": "compact failed",
                "usage": {},
            }),
        ):
            with self.subTest(label=label):
                session_id = "test-native-compact-failure-" + uuid.uuid4().hex
                server.upsert_session(session_id, "压缩失败测试", "/tmp/native-project", "code")
                try:
                    with patch.object(
                        server._claude_agent_bridge,
                        "ensure_started",
                        new=AsyncMock(return_value=True),
                    ), patch.object(
                        server._claude_agent_bridge,
                        "open_turn",
                        new=AsyncMock(return_value=_FakeNativeCompactFailureTurn(result_event)),
                    ):
                        with self.assertRaises(server.HTTPException) as raised:
                            await server.compact_agent_sdk_session(
                                session_id,
                                server.NativeCompactRequest(model="sonnet"),
                            )
                    self.assertEqual(502, raised.exception.status_code)
                finally:
                    server.save_events(session_id, [])
                    with server.db_connect() as conn:
                        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


class CorruptedToolHistoryErrorDetectorTest(unittest.TestCase):
    def test_anthropic_variant(self):
        msg = "API Error: 400 messages.2.content.0: unexpected `tool_use_id` found in `tool_result` blocks: toolu_abc123"
        self.assertTrue(server._corrupted_tool_history_error(msg))

    def test_bedrock_variant(self):
        msg = "Bedrock error message: The number of toolResult blocks at messages.2.content exceeds the number of toolUse blocks of previous turn."
        self.assertTrue(server._corrupted_tool_history_error(msg))

    def test_transport_error_not_matched(self):
        self.assertFalse(server._corrupted_tool_history_error("stream idle timeout"))
        self.assertFalse(server._corrupted_tool_history_error("connection closed"))

    def test_empty_not_matched(self):
        self.assertFalse(server._corrupted_tool_history_error(""))
        self.assertFalse(server._corrupted_tool_history_error(None))


class AutoRecoverCorruptedSdkSessionTest(unittest.IsolatedAsyncioTestCase):
    async def _make_session(self, events):
        session_id = str(uuid.uuid4())
        server.upsert_session(session_id, "恢复测试", os.path.expanduser("~"), "code")
        server.save_events(session_id, events)
        server.set_session_remote_state(session_id, "old-remote-id", True)
        server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        return session_id

    async def _cleanup(self, session_id):
        server.save_events(session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        for backup in server.iter_session_compact_backups(session_id):
            backup.unlink(missing_ok=True)

    async def test_recovery_writes_sdk_recovered_marker(self):
        events = [
            {"type": "user_input", "text": "第一轮"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "结论一"}]}},
            {"type": "user_input", "text": "第二轮"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "结论二"}]}},
        ]
        session_id = await self._make_session(events)
        fake_process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"- goal: continue work\n- changed: app.py", b"")),
            kill=lambda: None,
            wait=AsyncMock(return_value=0),
        )
        try:
            with patch.object(server.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=fake_process)):
                result = await server._auto_recover_corrupted_sdk_session(session_id, timeout=30.0)

            self.assertTrue(result["ok"])
            self.assertFalse(result.get("fallback"))

            saved = server.load_events(session_id)
            self.assertTrue(saved[0]["compacted"])
            self.assertTrue(saved[0]["sdk_recovered"])
            self.assertNotIn("remote_detached", saved[0])
            self.assertEqual("sdk-recovery-v1", saved[0]["context_strategy"])
            self.assertIn("goal: continue work", saved[0]["text"])

            with server.db_connect() as conn:
                row = conn.execute(
                    "SELECT remote_session_id, remote_ready, runtime_origin FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            self.assertNotEqual("old-remote-id", row["remote_session_id"])
            self.assertEqual(0, row["remote_ready"])
            self.assertEqual(server._RUNTIME_ORIGIN_AGENT_SDK, row["runtime_origin"])
        finally:
            await self._cleanup(session_id)

    async def test_recovery_fallback_on_haiku_timeout(self):
        events = [
            {"type": "user_input", "text": "任务一"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "user_input", "text": "任务二"},
        ]
        session_id = await self._make_session(events)

        async def slow_communicate():
            await asyncio.sleep(999)
            return b"", b""

        fake_process = SimpleNamespace(
            communicate=slow_communicate,
            kill=lambda: None,
            wait=AsyncMock(return_value=0),
        )
        try:
            with patch.object(server.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=fake_process)):
                result = await server._auto_recover_corrupted_sdk_session(session_id, timeout=0.05)

            self.assertTrue(result["ok"])
            self.assertTrue(result.get("fallback"))

            saved = server.load_events(session_id)
            self.assertTrue(saved[0]["compacted"])
            self.assertTrue(saved[0]["sdk_recovered"])
        finally:
            await self._cleanup(session_id)

    async def test_sdk_recovery_pending_injects_resume_context(self):
        """sdk_recovery_pending guard ensures resume context is built for SDK sessions."""
        events = [
            {
                "type": "user_input",
                "text": "【会话已自动恢复 · 以下为之前对话的摘要】\n\n- goal: keep going",
                "compacted": True,
                "sdk_recovered": True,
                "context_strategy": "sdk-recovery-v1",
            },
            {"type": "user_input", "text": "继续"},
        ]
        resume = server.build_compacted_resume_context(events)
        self.assertIn("goal: keep going", resume)


import asyncio as _asyncio_mod  # noqa: E402 — needed for timeout mock in fallback test


if __name__ == "__main__":
    unittest.main()
