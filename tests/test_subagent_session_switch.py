"""子代理（Task）状态按会话隔离的前端契约测试 —— Issue #13。

Bug：工作模式下切换会话再切回来，当前对话正在调用的子代理不显示。

原因：`subagentCards` 是全局 Map，`loadSession` 每次都用
`resetToolUseRuntimeState()` 清空它；而代码模式切回已缓存会话时走 DOM cache
快路径（不重放事件），卡片因此永远不会被重建。

修复：把子代理状态挂到 `SessionState`（与 `dom` / `pendingEvents` 同一套
按会话保存/恢复机制），离开会话前保存、走 DOM cache 快路径时恢复。

这些断言锁定修复不被回退，并保持两份 index.html 一致。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGED_INDEX = ROOT / "claude_web" / "static" / "index.html"
ROOT_INDEX = ROOT / "static" / "index.html"


class SubagentSessionSwitchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PACKAGED_INDEX.read_text(encoding="utf-8")

    def test_session_state_owns_subagent_fields(self):
        """SessionState 必须持有按会话的子代理状态。"""
        ctor = re.search(r"class SessionState \{.*?\n\}", self.html, re.S)
        self.assertIsNotNone(ctor, "未找到 SessionState 类")
        body = ctor.group(0)
        self.assertIn("this.subagents = new Map()", body)
        self.assertIn("this.subagentDismissed = false", body)

    def test_subagent_bindings_are_rebindable(self):
        """subagentCards 必须可重新绑定（const 会让按会话切换抛错）。"""
        self.assertIn("let subagentCards = new Map()", self.html)
        self.assertNotIn("const subagentCards", self.html)

    def test_reset_rebinds_instead_of_clearing(self):
        """reset 不能 clear()：那个 Map 可能已归属别的会话，清空会污染它。"""
        reset = re.search(
            r"function resetToolUseRuntimeState\(\) \{.*?\n\}", self.html, re.S
        )
        self.assertIsNotNone(reset, "未找到 resetToolUseRuntimeState")
        body = reset.group(0)
        self.assertIn("subagentCards = new Map()", body)
        self.assertNotIn("subagentCards.clear()", body)

    def test_save_and_restore_helpers_exist(self):
        """保存 / 恢复两个 helper 都要在，且真正读写 SessionState。"""
        save = re.search(
            r"function saveSubagentStateTo\(sid\) \{.*?\n\}", self.html, re.S
        )
        self.assertIsNotNone(save, "未找到 saveSubagentStateTo")
        self.assertIn("st.subagents = subagentCards", save.group(0))

        restore = re.search(
            r"function restoreSubagentStateFrom\(sid\) \{.*?\n\}", self.html, re.S
        )
        self.assertIsNotNone(restore, "未找到 restoreSubagentStateFrom")
        self.assertIn("subagentCards =", restore.group(0))

    def test_load_session_saves_before_reset(self):
        """必须在 reset 清空绑定之前保存，否则离开的会话状态直接丢失。"""
        save_at = self.html.find("saveSubagentStateTo(prevSessionIdSnap)")
        self.assertNotEqual(save_at, -1, "loadSession 未保存上一个会话的子代理状态")
        reset_at = self.html.find("resetToolUseRuntimeState();", save_at)
        self.assertNotEqual(reset_at, -1)
        self.assertLess(save_at, reset_at, "保存必须发生在 reset 之前")

    def test_dom_cache_path_restores_subagents(self):
        """DOM cache 快路径不重放事件，必须在该分支内显式恢复子代理卡片。"""
        branch = re.search(
            r"const hasDomCache = .*?\n    if \(hasDomCache\) \{(.*?)\n    \} else \{",
            self.html,
            re.S,
        )
        self.assertIsNotNone(branch, "未找到 hasDomCache 快路径分支")
        self.assertIn(
            "restoreSubagentStateFrom(data.id)",
            branch.group(1),
            "DOM cache 快路径没有恢复子代理状态 —— Issue #13 会复现",
        )

    def test_both_index_copies_identical(self):
        self.assertEqual(
            ROOT_INDEX.read_text(encoding="utf-8"),
            self.html,
            "static/index.html 与 claude_web/static/index.html 不一致",
        )


if __name__ == "__main__":
    unittest.main()
