import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException

from claude_web import server


class CodeWorkspaceLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session_id = "code-loop-" + uuid.uuid4().hex
        self.cwd = tempfile.mkdtemp(prefix="claude-web-code-loop-")
        server.upsert_session(self.session_id, "code loop", self.cwd, "code")
        server.set_session_runtime_origin(self.session_id, server._RUNTIME_ORIGIN_AGENT_SDK)

    def tearDown(self):
        server.save_events(self.session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM code_message_queue WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM code_turn_requests WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM code_project_settings WHERE cwd = ?", (str(Path(self.cwd).resolve()),))
            conn.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))

    async def test_persistent_queue_reorders_and_turn_idempotency_removes_accepted_item(self):
        first = "queue-" + uuid.uuid4().hex
        second = "queue-" + uuid.uuid4().hex
        await server.add_code_message_queue(
            self.session_id,
            server.CodeQueueRequest(id=first, payload={"message": "first"}),
        )
        await server.add_code_message_queue(
            self.session_id,
            server.CodeQueueRequest(id=second, payload={"message": "second"}),
        )
        await server.reorder_code_message_queue(
            self.session_id,
            server.CodeQueueOrderRequest(ids=[second, first]),
        )
        queued = await server.list_code_message_queue(self.session_id)
        self.assertEqual([second, first], [item["id"] for item in queued["items"]])

        reserved = server._reserve_code_turn(self.session_id, second)
        server._mark_code_turn_accepted(self.session_id, reserved)
        queued = await server.list_code_message_queue(self.session_id)
        self.assertEqual([first], [item["id"] for item in queued["items"]])
        with self.assertRaises(HTTPException) as raised:
            server._reserve_code_turn(self.session_id, second)
        self.assertEqual(409, raised.exception.status_code)

    async def test_auto_approval_and_project_validation_settings_are_session_and_project_scoped(self):
        enabled = await server.set_code_session_auto_approve(
            self.session_id,
            server.CodeAutoApproveRequest(enabled=True),
        )
        self.assertTrue(enabled["enabled"])
        with server.db_connect() as conn:
            row = conn.execute("SELECT auto_approve FROM sessions WHERE id = ?", (self.session_id,)).fetchone()
        self.assertEqual(1, row["auto_approve"])

        saved = await server.set_code_project_settings(
            self.session_id,
            server.CodeProjectSettingsRequest(
                validation_command="python -m unittest",
                validation_mode="auto",
                validation_timeout=45,
            ),
        )
        loaded = await server.get_code_project_settings(self.session_id)
        self.assertEqual("auto", saved["validation_mode"])
        self.assertEqual("python -m unittest", loaded["validation_command"])
        self.assertEqual(45, loaded["validation_timeout"])

    def test_history_page_starts_on_user_turn_and_preserves_absolute_offsets(self):
        events = []
        for index in range(5):
            events.extend([
                {"type": "user_input", "text": f"turn {index}"},
                {"type": "assistant", "message": {"content": []}},
                {"type": "result", "duration_ms": index},
            ])
        page = server.session_event_page(events, 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(9, page["event_offset"])
        self.assertEqual(3, page["user_offset"])
        self.assertEqual("turn 3", page["events"][0]["text"])
        self.assertEqual(len(events), page["total_event_count"])

    def test_diff_hunks_are_independently_addressable(self):
        diff = """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1 +1 @@
-old one
+new one
@@ -5 +5 @@
-old two
+new two
"""
        hunks = server._diff_hunk_patches(diff)
        self.assertEqual([0, 1], [hunk["index"] for hunk in hunks])
        self.assertTrue(all(hunk["revertible"] for hunk in hunks))
        self.assertIn("@@ -1 +1 @@", hunks[0]["patch"])
        self.assertNotIn("@@ -5 +5 @@", hunks[0]["patch"])


class CodeWorkspaceStaticContractTest(unittest.TestCase):
    def test_light_context_stays_default_on_and_permission_copy_is_current(self):
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("lightContextMode: LS.get('lightContextMode', true)", source)
        self.assertIn("LIGHT_CONTEXT_DEFAULT_POLICY = 'native-compact-default-on-v1'", source)
        self.assertIn("LS.set('lightContextMode', true)", source)
        self.assertNotIn("Web 版不能在运行中批准权限", source)
        self.assertIn("替我审批", source)


if __name__ == "__main__":
    unittest.main()
