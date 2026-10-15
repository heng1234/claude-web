"""重复发送修复的前端契约测试 —— 工作模式"发完又重发并新建会话"。

现象：工作模式发一条消息，后端已正常跑完，但 SSE 的 meta 帧丢失时，前端
误判"后端没收到"→ 队列把同一条消息带 force_new=true 重发 → 又开一个新会话
重跑一遍（曾观测到同一句 7–17s 内连开 3 个会话）。

根因：sendCore 判断"是否到达后端"只认 serverAccepted（仅 meta 帧置位），
meta 一丢，成功轮次被当成失败触发重发。

修复：reachedBackend / catch 分支的 backendReached 纳入 lastResultEvent 与
sawServerEvent —— 只要从流里收到过任何事件，就认定后端已处理，不再重发。

这些断言锁定修复不被回退，并保持两份 index.html 一致。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGED_INDEX = ROOT / "claude_web" / "static" / "index.html"
ROOT_INDEX = ROOT / "static" / "index.html"


class CodeQueueResendGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PACKAGED_INDEX.read_text(encoding="utf-8")

    def test_saw_server_event_flag_declared_and_set(self):
        """必须有 sawServerEvent 标志，并在解析到 SSE 事件时置位。"""
        self.assertIn("let sawServerEvent = false;", self.html)
        # 解析成功后紧接着置位
        m = re.search(
            r"const obj = JSON\.parse\(line\.slice\(6\)\);\s*\n(?:\s*//.*\n)*\s*sawServerEvent = true;",
            self.html,
        )
        self.assertIsNotNone(m, "解析 SSE 事件后未置位 sawServerEvent")

    def test_reached_backend_includes_result_and_event(self):
        """reachedBackend 判定必须纳入 lastResultEvent 与 sawServerEvent。"""
        m = re.search(r"const reachedBackend = (.*?);", self.html, re.S)
        self.assertIsNotNone(m, "未找到 reachedBackend 判定")
        expr = m.group(1)
        self.assertIn("lastResultEvent", expr)
        self.assertIn("sawServerEvent", expr)

    def test_catch_branch_uses_backend_reached(self):
        """catch 分支返回的 accepted 不能再只认 serverAccepted。"""
        m = re.search(r"const backendReached = (.*?);", self.html)
        self.assertIsNotNone(m, "catch 分支未计算 backendReached")
        self.assertIn("sawServerEvent", m.group(1))
        self.assertIn(
            "accepted: e.name === 'AbortError' ? serverAccepted : backendReached",
            self.html,
            "catch 分支的 accepted 未改用 backendReached（AbortError 除外）",
        )

    def test_both_index_copies_identical(self):
        self.assertEqual(
            ROOT_INDEX.read_text(encoding="utf-8"),
            self.html,
            "static/index.html 与 claude_web/static/index.html 不一致",
        )


if __name__ == "__main__":
    unittest.main()
