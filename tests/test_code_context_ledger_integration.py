from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from claude_web import server
from claude_web.code_context_ledger import CodeContextLedger


class _NativeCompactTurn:
    async def events(self):
        yield {
            "type": "event",
            "event": {
                "type": "system",
                "subtype": "compact_boundary",
                "session_id": "ledger-native-session",
                "compact_metadata": {"pre_tokens": 900_000, "post_tokens": 140_000},
            },
        }
        yield {
            "type": "event",
            "event": {
                "type": "result",
                "subtype": "success",
                "session_id": "ledger-native-session",
                "usage": {"input_tokens": 140_000, "output_tokens": 20},
            },
        }
        yield {"type": "done", "sessionId": "ledger-native-session"}


class CodeContextLedgerIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-web-ledger-integration-")
        self.project = Path(self.temporary.name, "project").resolve()
        self.other = Path(self.temporary.name, "other").resolve()
        self.project.mkdir()
        self.other.mkdir()
        self.session_id = "ledger-code-" + uuid.uuid4().hex
        self.foreign_id = "ledger-foreign-" + uuid.uuid4().hex
        self.chat_id = "ledger-chat-" + uuid.uuid4().hex
        server.upsert_session(self.session_id, "ledger", str(self.project), "code")
        server.upsert_session(self.foreign_id, "foreign", str(self.other), "code")
        server.upsert_session(self.chat_id, "chat", str(self.project), "chat")
        server.set_session_runtime_origin(self.session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
        self.ledger = CodeContextLedger(server.DB_PATH)
        self.ledger.initialize()
        self.getter = patch.object(server, "_get_code_context_ledger", return_value=self.ledger)
        self.getter.start()

    async def asyncTearDown(self):
        self.getter.stop()
        server._agent_sdk_running_sessions.discard(self.session_id)
        server._compacting_sessions.discard(self.session_id)
        server._agent_sdk_context_usage_cache.pop(self.session_id, None)
        server.save_events(self.session_id, [])
        with server.db_connect() as conn:
            conn.execute(
                "DELETE FROM code_context_ledger WHERE session_id IN (?, ?, ?)",
                (self.session_id, self.foreign_id, self.chat_id),
            )
            conn.execute(
                "DELETE FROM sessions WHERE id IN (?, ?, ?)",
                (self.session_id, self.foreign_id, self.chat_id),
            )
        self.temporary.cleanup()

    @staticmethod
    def resolved_pack(pack_id: str, *, padding: str = "") -> dict:
        return {
            "pack_id": pack_id,
            "revision": 12,
            "expires_at": 4_000_000_000,
            "context": {
                "selected_nodes": [{"id": "node-api", "title": "API"}],
                "neighbor_nodes": [{"id": "node-test"}],
                "relations": [{"source": "node-test", "target": "node-api"}],
                "snippets": [{
                    "path": "app.py",
                    "excerpt": "SECRET_SOURCE_BODY" + padding,
                    "prompt": "DO_NOT_PERSIST_PROMPT",
                }],
            },
        }

    async def test_context_pack_is_recorded_only_after_validated_attach_without_body(self):
        pack_id = "a" * 32
        resolver = AsyncMock(return_value=self.resolved_pack(pack_id))
        with patch.object(server._project_map_service, "resolve_context_pack", resolver):
            _, prefix = await server._resolve_code_context_packs(
                self.session_id, [pack_id], code_workspace=True,
            )
        self.assertIn("SECRET_SOURCE_BODY", prefix)
        item = self.ledger.list(self.session_id, entry_type="project_map_pack")["items"][0]
        self.assertEqual(12, int(item["revision"]))
        self.assertEqual(2, item["descriptor"]["node_count"])
        self.assertEqual(1, item["descriptor"]["relation_count"])
        self.assertEqual(1, item["descriptor"]["snippet_count"])
        serialized = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("SECRET_SOURCE_BODY", serialized)
        self.assertNotIn("DO_NOT_PERSIST_PROMPT", serialized)
        self.assertNotIn("excerpt", serialized)
        self.assertNotIn("prompt", serialized)

        before = self.ledger.summary(self.session_id)["count"]
        with patch.object(server._project_map_service, "resolve_context_pack", AsyncMock(
            return_value=self.resolved_pack("b" * 32, padding="x" * 1000),
        )), patch.object(server, "_CODE_CONTEXT_PACK_PREFIX_LIMIT", 10):
            with self.assertRaises(HTTPException) as oversized:
                await server._resolve_code_context_packs(
                    self.session_id, ["b" * 32], code_workspace=True,
                )
        self.assertEqual(413, oversized.exception.status_code)
        self.assertEqual(before, self.ledger.summary(self.session_id)["count"])

    async def test_fresh_sdk_usage_records_scalars_and_write_failure_does_not_block(self):
        usage = {
            "totalTokens": 120_000,
            "maxTokens": 1_000_000,
            "model": "sonnet",
            "content": "SDK_BODY_MUST_NOT_ENTER_LEDGER",
            "prompt": "SDK_PROMPT_MUST_NOT_ENTER_LEDGER",
        }
        with patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                patch.object(server._claude_agent_bridge, "context_usage", AsyncMock(return_value={"usage": usage})):
            result = await server.agent_sdk_context_usage(
                self.session_id, "sonnet", None, None, None, None,
            )
        self.assertFalse(result["stale"])
        item = self.ledger.list(self.session_id, entry_type="sdk_context_usage")["items"][0]
        self.assertEqual(120_000, item["token_estimate"])
        self.assertEqual(1_000_000, item["descriptor"]["max_tokens"])
        serialized = json.dumps(item, ensure_ascii=False)
        self.assertNotIn("SDK_BODY", serialized)
        self.assertNotIn("SDK_PROMPT", serialized)

        with patch.object(server, "_get_code_context_ledger", side_effect=RuntimeError("ledger unavailable")), \
                patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                patch.object(server._claude_agent_bridge, "context_usage", AsyncMock(return_value={"usage": usage})):
            result = await server.agent_sdk_context_usage(
                self.session_id, "sonnet", None, None, None, None,
            )
        self.assertTrue(result["ok"])

    async def test_native_compact_updates_ledger_lifecycle_and_normalizes_usage(self):
        pack = self.ledger.record_project_map_pack(
            self.session_id, pack_id="before-pack", revision=1, descriptor={"node_count": 1},
        )
        retrieval = self.ledger.record_auto_retrieval(
            self.session_id, descriptor={"path": "service.py"}, token_estimate=30,
        )
        pinned = self.ledger.record_user_pinned(
            self.session_id, descriptor={"path": "requirements.md"}, token_estimate=10,
        )
        with patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                patch.object(server._claude_agent_bridge, "open_turn", AsyncMock(return_value=_NativeCompactTurn())), \
                patch.object(server._claude_agent_bridge, "context_usage", AsyncMock(return_value={"usage": []})):
            result = await server.compact_agent_sdk_session(
                self.session_id, server.NativeCompactRequest(model="sonnet"),
            )
        self.assertEqual({}, result["context_usage"])
        self.assertEqual("compacted", self.ledger.get(self.session_id, pack["id"])["lifecycle_state"])
        self.assertEqual("dropped", self.ledger.get(self.session_id, retrieval["id"])["lifecycle_state"])
        self.assertEqual("active", self.ledger.get(self.session_id, pinned["id"])["lifecycle_state"])
        compact = self.ledger.list(self.session_id, entry_type="native_compact")["items"][0]
        self.assertEqual(900_000, compact["descriptor"]["pre_tokens"])
        self.assertEqual(140_000, compact["descriptor"]["post_tokens"])

    async def test_read_apis_reject_chat_cross_session_and_changed_cwd(self):
        item = self.ledger.record_user_pinned(self.session_id, descriptor={"path": "plan.md"})
        listed = await server.list_code_context_ledger(self.session_id, 100, 0, None, None)
        summarized = await server.summarize_code_context_ledger(self.session_id)
        loaded = await server.get_code_context_ledger_entry(self.session_id, item["id"])
        self.assertEqual(1, listed["total"])
        self.assertEqual(1, summarized["summary"]["count"])
        self.assertEqual(item["id"], loaded["entry"]["id"])

        with self.assertRaises(HTTPException) as chat:
            await server.list_code_context_ledger(self.chat_id, 100, 0, None, None)
        self.assertEqual(409, chat.exception.status_code)
        self.assertEqual("code_session_required", chat.exception.detail["code"])
        with self.assertRaises(HTTPException) as foreign:
            await server.get_code_context_ledger_entry(self.foreign_id, item["id"])
        self.assertEqual(403, foreign.exception.status_code)
        self.assertEqual("ledger_forbidden", foreign.exception.detail["code"])

        with server.db_connect() as conn:
            conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(self.other), self.session_id))
        self.assertEqual(0, (await server.list_code_context_ledger(
            self.session_id, 100, 0, None, None,
        ))["total"])
        with self.assertRaises(HTTPException) as moved:
            await server.get_code_context_ledger_entry(self.session_id, item["id"])
        self.assertEqual(409, moved.exception.status_code)
        self.assertEqual("workspace_mismatch", moved.exception.detail["code"])


class CodeContextLedgerFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_html = Path("claude_web/static/index.html").read_text(encoding="utf-8")
        cls.root_html = Path("static/index.html").read_text(encoding="utf-8")

    def test_ledger_is_code_only_and_static_copies_match(self):
        self.assertEqual(self.package_html, self.root_html)
        self.assertIn('id="cwContextUsageModal" class="modal-backdrop hidden cw-code-only"', self.package_html)
        self.assertIn('id="cwContextLedger" class="cw-context-ledger"', self.package_html)
        self.assertIn("if (!codeMode || !sessionId) return null;", self.package_html)
        self.assertIn("fetch(`${base}/summary`)", self.package_html)
        self.assertIn(".cw-browser-validation { grid-column:1/-1;", self.package_html)
        self.assertIn(".cw-browser-validation > summary,.cw-browser-validation-form input,.cw-browser-validation-form textarea { min-height:44px; }", self.package_html)

    def test_renderer_shows_budget_and_lifecycle_but_never_descriptor_body(self):
        start = self.package_html.index("function renderCodeContextLedger(")
        end = self.package_html.index("async function refreshCodeContextLedger", start)
        body = self.package_html[start:end]
        self.assertIn("active_token_estimate", body)
        self.assertIn("item.lifecycle_state", body)
        self.assertIn("item.token_estimate", body)
        self.assertIn("item.revision", body)
        self.assertIn("item.stale", body)
        self.assertNotIn("item.descriptor", body)
        self.assertNotIn("excerpt", body)
        self.assertNotIn("prompt", body)


if __name__ == "__main__":
    unittest.main()
