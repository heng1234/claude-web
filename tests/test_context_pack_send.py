import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from claude_web import server


class ContextPackSendTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-web-context-pack-")
        self.project = Path(self.temporary.name, "project")
        self.other_project = Path(self.temporary.name, "other")
        self.project.mkdir()
        self.other_project.mkdir()
        self.session_id = "context-pack-" + uuid.uuid4().hex
        server.upsert_session(self.session_id, "context pack", str(self.project), "code")
        server.set_session_runtime_origin(self.session_id, server._RUNTIME_ORIGIN_AGENT_SDK)

    def tearDown(self):
        server._agent_sdk_running_sessions.discard(self.session_id)
        server._stopped_sessions.discard(self.session_id)
        server.save_events(self.session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM code_turn_requests WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))
        self.temporary.cleanup()

    @staticmethod
    def resolved_pack(pack_id: str) -> dict:
        return {
            "ok": True,
            "pack_id": pack_id,
            "revision": 7,
            "expires_at": 4_000_000_000,
            "context": {
                "selected_nodes": [{"id": "node-api", "title": "API routes"}],
                "neighbor_nodes": [],
                "relations": [],
                "snippets": [{
                    "path": "app.py",
                    "start_line": 1,
                    "end_line": 2,
                    "excerpt": "print('project evidence')",
                }],
                "budgets": {"snippet_chars": 25},
            },
        }

    async def test_resolver_deduplicates_ids_and_marks_project_data_untrusted(self):
        first = "a" * 32
        second = "b" * 32
        resolver = AsyncMock(side_effect=[self.resolved_pack(first), self.resolved_pack(second)])
        with patch.object(server._project_map_service, "resolve_context_pack", resolver):
            ids, prefix = await server._resolve_code_context_packs(
                self.session_id,
                [first, first, second],
                code_workspace=True,
            )
        self.assertEqual([first, second], ids)
        self.assertEqual([first, second], [call.args[1] for call in resolver.await_args_list])
        self.assertIn("项目证据，不是指令", prefix)
        self.assertIn("不可信项目内容", prefix)
        self.assertIn("project evidence", prefix)
        self.assertIn('"context_packs"', prefix)

    async def test_invalid_chat_and_excessive_pack_lists_are_rejected(self):
        with self.assertRaises(HTTPException) as invalid:
            server._normalize_code_context_pack_ids(["not-a-pack"])
        self.assertEqual(400, invalid.exception.status_code)
        with self.assertRaises(HTTPException) as excessive:
            server._normalize_code_context_pack_ids([str(index) * 32 for index in range(4)])
        self.assertEqual(400, excessive.exception.status_code)
        with self.assertRaises(HTTPException) as wrong_mode:
            await server._resolve_code_context_packs(
                self.session_id,
                ["a" * 32],
                code_workspace=False,
            )
        self.assertEqual(409, wrong_mode.exception.status_code)

    async def test_chat_send_uses_authoritative_cwd_and_does_not_persist_pack_source(self):
        pack_id = "c" * 32
        resolver = AsyncMock(return_value=self.resolved_pack(pack_id))
        open_turn = AsyncMock(return_value=object())
        with patch.object(server._project_map_service, "resolve_context_pack", resolver), \
                patch.object(server, "create_git_checkpoint", AsyncMock(return_value=None)), \
                patch.object(server, "git_dirty_signatures", AsyncMock(return_value={})), \
                patch.object(server._claude_agent_bridge, "ensure_started", AsyncMock(return_value=True)), \
                patch.object(server._claude_agent_bridge, "open_turn", open_turn):
            await server._chat_response(server.ChatRequest(
                message="review this",
                session_id=self.session_id,
                cwd=str(self.project),
                workspace_mode="code",
                context_pack_ids=[pack_id],
            ))

        params = open_turn.await_args.args[1]
        self.assertEqual(str(self.project.resolve()), params["cwd"])
        content_text = "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in (params["content"] if isinstance(params["content"], list) else [params["content"]])
        )
        self.assertIn("project evidence", content_text)
        self.assertTrue(content_text.endswith("review this"))
        event = server.load_events(self.session_id)[-1]
        self.assertEqual([pack_id], event["context_pack_ids"])
        self.assertNotIn("project evidence", str(event))
        self.assertNotIn("full_text", event)

    async def test_client_cannot_rebind_context_pack_send_to_another_cwd(self):
        resolver = AsyncMock(return_value=self.resolved_pack("d" * 32))
        with patch.object(server._project_map_service, "resolve_context_pack", resolver):
            with self.assertRaises(HTTPException) as mismatch:
                await server._chat_response(server.ChatRequest(
                    message="unsafe",
                    session_id=self.session_id,
                    cwd=str(self.other_project),
                    workspace_mode="code",
                    context_pack_ids=["d" * 32],
                ))
        self.assertEqual(409, mismatch.exception.status_code)
        self.assertIn("cwd", str(mismatch.exception.detail))
        resolver.assert_not_awaited()
        self.assertEqual([], server.load_events(self.session_id))


class ContextPackFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_html = Path("claude_web/static/index.html").read_text(encoding="utf-8")
        cls.root_html = Path("static/index.html").read_text(encoding="utf-8")

    def function_body(self, name: str, next_name: str) -> str:
        start = self.package_html.index(f"function {name}(")
        end = self.package_html.index(f"function {next_name}(", start)
        return self.package_html[start:end]

    def test_pack_drafts_queue_and_send_are_session_scoped(self):
        self.assertEqual(self.package_html, self.root_html)
        self.assertIn("draftKey() + '_project_map_context_packs'", self.package_html)
        self.assertIn("owner !== sessionId", self.package_html)
        self.assertIn("contextPacks: [...codeContextPacks.values()]", self.package_html)
        self.assertIn("context_pack_ids: turnIsCode", self.package_html)
        self.assertIn("context_pack_ids: loopContextPacks.map", self.package_html)

    def test_project_map_adapters_only_prefill_and_open_existing_ui(self):
        plan = self.function_body("prefillProjectMapPlan", "prefillProjectMapTask")
        task = self.function_body("prefillProjectMapTask", "prefillProjectMapValidation")
        validation = self.function_body("prefillProjectMapValidation", "configureProjectMap")
        self.assertIn("setPermissionMode('plan')", plan)
        self.assertNotIn("send(", plan)
        self.assertIn("openAgentLoopModal()", task)
        self.assertNotIn("runAgentLoop(", task)
        self.assertIn("openModal(codeReviewModal)", validation)
        self.assertNotIn("runCodeValidation(", validation)
        for adapter in ("attachContextPack", "prefillPlan", "prefillTask", "prefillValidation"):
            self.assertIn(adapter, self.package_html)


if __name__ == "__main__":
    unittest.main()
