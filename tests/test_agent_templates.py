"""Unit tests for agent_templates CRUD helpers.

Agent templates are reusable session presets (icon + system prompt + model +
permission mode + bound connectors + default task + cwd + mode). They mirror
Doubao's "伙伴" and opcode/jetbrains agent definitions. These tests pin the
helper contract used by the /api/agent-templates endpoints, against an isolated
temp DB so no real user data is touched.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web import server


class AgentTemplateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "templates.db"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
        # Fresh WAL init flag so db_connect re-runs PRAGMA on the new file.
        self.init_patch = patch.object(server, "_DB_INITIALIZED", False)
        self.init_patch.start()
        server.init_db()

    def tearDown(self) -> None:
        self.init_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    # ---- create / get / list ------------------------------------------------
    def test_create_returns_id_and_get_roundtrips_fields(self):
        tid = server._agent_template_create({
            "name": "代码审查员",
            "icon": "🔍",
            "system_prompt": "你是严格的代码审查员。",
            "model": "claude-opus-4-8",
            "permission_mode": "plan",
            "default_task": "审查当前 diff",
            "cwd": "/tmp/proj",
            "connector_ids": ["github", "feishu"],
            "mode": "code",
        })
        self.assertRegex(tid, r"^[0-9a-f]{32}$")
        row = server._agent_template_get(tid)
        self.assertEqual(row["name"], "代码审查员")
        self.assertEqual(row["mode"], "code")
        self.assertEqual(row["permission_mode"], "plan")
        # connector_ids surfaces as a parsed list, not raw JSON text.
        self.assertEqual(row["connector_ids"], ["github", "feishu"])
        self.assertFalse(row["builtin"])

    def test_blank_name_is_rejected(self):
        with self.assertRaises(ValueError):
            server._agent_template_create({"name": "   "})

    def test_invalid_permission_mode_falls_back_to_default(self):
        tid = server._agent_template_create({"name": "x", "permission_mode": "nonsense"})
        self.assertEqual(server._agent_template_get(tid)["permission_mode"], "default")

    def test_invalid_mode_falls_back_to_both(self):
        tid = server._agent_template_create({"name": "x", "mode": "sideways"})
        self.assertEqual(server._agent_template_get(tid)["mode"], "both")

    def test_connector_ids_must_be_a_list_of_strings(self):
        tid = server._agent_template_create({"name": "x", "connector_ids": "github"})
        # A bare string is coerced to empty, not split into characters.
        self.assertEqual(server._agent_template_get(tid)["connector_ids"], [])

    # ---- mode filtering -----------------------------------------------------
    def test_list_filters_by_mode(self):
        server._agent_template_create({"name": "chat-only", "mode": "chat"})
        server._agent_template_create({"name": "code-only", "mode": "code"})
        server._agent_template_create({"name": "either", "mode": "both"})
        code_names = {t["name"] for t in server._agent_template_list(mode="code")}
        chat_names = {t["name"] for t in server._agent_template_list(mode="chat")}
        self.assertEqual(code_names, {"code-only", "either"})
        self.assertEqual(chat_names, {"chat-only", "either"})
        # No filter → all.
        self.assertEqual(len(server._agent_template_list()), 3)

    # ---- update / delete ----------------------------------------------------
    def test_update_changes_fields_and_bumps_updated_at(self):
        tid = server._agent_template_create({"name": "old"})
        before = server._agent_template_get(tid)["updated_at"]
        server._agent_template_update(tid, {"name": "new", "connector_ids": ["email"]})
        after = server._agent_template_get(tid)
        self.assertEqual(after["name"], "new")
        self.assertEqual(after["connector_ids"], ["email"])
        self.assertGreaterEqual(after["updated_at"], before)

    def test_delete_removes_row(self):
        tid = server._agent_template_create({"name": "temp"})
        server._agent_template_delete(tid)
        self.assertIsNone(server._agent_template_get(tid))

    def test_builtin_templates_are_read_only(self):
        tid = server._agent_template_create({"name": "seed", "builtin": True})
        with self.assertRaises(PermissionError):
            server._agent_template_update(tid, {"name": "hacked"})
        with self.assertRaises(PermissionError):
            server._agent_template_delete(tid)

    # ---- clone --------------------------------------------------------------
    def test_clone_produces_editable_copy(self):
        src = server._agent_template_create({
            "name": "内置模板", "builtin": True, "connector_ids": ["github"],
        })
        clone_id = server._agent_template_clone(src)
        self.assertNotEqual(clone_id, src)
        clone = server._agent_template_get(clone_id)
        self.assertFalse(clone["builtin"])
        self.assertEqual(clone["connector_ids"], ["github"])
        self.assertIn("副本", clone["name"])
        # Clone is editable even though the source was builtin.
        server._agent_template_update(clone_id, {"name": "我的模板"})
        self.assertEqual(server._agent_template_get(clone_id)["name"], "我的模板")

    # ---- export / import ----------------------------------------------------
    def test_export_import_roundtrips_without_id_collision(self):
        src = server._agent_template_create({
            "name": "分享模板", "system_prompt": "hi", "connector_ids": ["notion"],
        })
        blob = server._agent_template_export(src)
        self.assertNotIn("id", blob)  # exported blob carries no id
        self.assertEqual(blob["name"], "分享模板")
        new_id = server._agent_template_import(blob)
        self.assertNotEqual(new_id, src)
        imported = server._agent_template_get(new_id)
        self.assertEqual(imported["connector_ids"], ["notion"])
        self.assertFalse(imported["builtin"])  # imported is never builtin


if __name__ == "__main__":
    unittest.main()
