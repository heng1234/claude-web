"""Unit tests for multi-role roundtable discussion.

The roundtable feature lets several agent-template "roles" weigh in on one
topic within a single Code-mode turn. We reuse agent_templates as the role
library and the SDK's native Task/subagent frames for visualization, so the
whole discussion is one ordinary Code turn driven by a composed prompt.

These tests pin:
  1. _build_roundtable_prompt — pure prompt synthesis from roles + topic.
  2. _roundtable_compose — resolves role ids against agent_templates and
     returns the message/display_message the frontend sends via /api/chat.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web import server


class RoundtablePromptTest(unittest.TestCase):
    def _role(self, name, prompt, icon="🧩"):
        return {"name": name, "icon": icon, "system_prompt": prompt}

    def test_empty_roles_raises(self):
        with self.assertRaises(ValueError):
            server._build_roundtable_prompt([], "议题")

    def test_empty_topic_raises(self):
        with self.assertRaises(ValueError):
            server._build_roundtable_prompt([self._role("A", "pa")], "   ")

    def test_prompt_includes_topic_and_all_roles(self):
        roles = [
            self._role("架构师", "你从系统设计角度分析。"),
            self._role("安全专家", "你从安全角度分析。"),
        ]
        prompt = server._build_roundtable_prompt(roles, "是否引入消息队列")
        self.assertIn("是否引入消息队列", prompt)
        self.assertIn("架构师", prompt)
        self.assertIn("安全专家", prompt)
        # Each role's system prompt is carried into the briefing.
        self.assertIn("你从系统设计角度分析。", prompt)
        self.assertIn("你从安全角度分析。", prompt)

    def test_prompt_instructs_task_dispatch(self):
        prompt = server._build_roundtable_prompt([self._role("A", "pa")], "topic")
        # Must instruct the model to use the Task tool to dispatch each role,
        # which is what drives the native subagent visualization.
        self.assertIn("Task", prompt)

    def test_prompt_requests_final_synthesis(self):
        prompt = server._build_roundtable_prompt(
            [self._role("A", "pa"), self._role("B", "pb")], "topic"
        )
        # A roundtable should end with a synthesis, not just parallel opinions.
        self.assertTrue(any(kw in prompt for kw in ("综合", "总结", "结论")))


class RoundtableComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "rt.db"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.init_patch = patch.object(server, "_DB_INITIALIZED", False)
        self.init_patch.start()
        server.init_db()

    def tearDown(self) -> None:
        self.init_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_compose_resolves_roles(self):
        a = server._agent_template_create({"name": "架构师", "system_prompt": "sa", "mode": "code"})
        b = server._agent_template_create({"name": "安全专家", "system_prompt": "sb", "mode": "code"})
        out = server._roundtable_compose([a, b], "选型讨论")
        self.assertIn("架构师", out["message"])
        self.assertIn("安全专家", out["message"])
        self.assertIn("选型讨论", out["message"])
        self.assertTrue(out["display_message"])

    def test_compose_missing_role_raises_keyerror(self):
        with self.assertRaises(KeyError):
            server._roundtable_compose(["does-not-exist"], "topic")

    def test_compose_empty_roles_raises_valueerror(self):
        with self.assertRaises(ValueError):
            server._roundtable_compose([], "topic")


if __name__ == "__main__":
    unittest.main()
