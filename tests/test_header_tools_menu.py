"""Header「更多工具」下拉菜单的前端契约测试。

顶栏图标过多时，低频/工作区专属入口收进 headerToolsMenu 下拉菜单。
这些断言锁定：菜单结构存在、被收纳按钮的 id 未丢失（否则 JS handler 绑不上）、
两份 index.html 一致、菜单文案 i18n 双语对等。
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGED_INDEX = ROOT / "claude_web" / "static" / "index.html"
ROOT_INDEX = ROOT / "static" / "index.html"
ZH = ROOT / "claude_web" / "static" / "i18n" / "zh.json"
EN = ROOT / "claude_web" / "static" / "i18n" / "en.json"

# 收进「更多工具」菜单的按钮 —— id 必须保留，前端 handler 靠 id 绑定
MENU_ITEM_IDS = [
    "cwProjectMapBtn",
    "cwOpenCodeFileBtn",
    "roundtableBtn",
    "scheduledTasksBtn",
    "configCenterBtn",
    "headerMoreBtn",
]
# 仍外露在顶栏的高频按钮
EXPOSED_IDS = ["connectorBtn", "agentTemplateBtn", "helpBtn", "exportBtn"]


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        nk = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, nk))
        else:
            out[nk] = v
    return out


def _menu_block(html):
    """精确截取 headerToolsMenu 的内容区（到匹配的闭合标签为止）。

    用括号深度匹配 <div> 而非固定窗口，避免把菜单后面外露的按钮框进来。
    """
    open_tag = 'id="headerToolsMenu"'
    start = html.index(open_tag)
    # 回退到该 div 的 '<'
    div_start = html.rindex("<", 0, start)
    depth = 0
    i = div_start
    while i < len(html):
        if html.startswith("<div", i):
            depth += 1
            i += 4
        elif html.startswith("</div>", i):
            depth -= 1
            i += 6
            if depth == 0:
                return html[div_start:i]
        else:
            i += 1
    raise AssertionError("headerToolsMenu 的闭合 </div> 未找到")


class HeaderToolsMenuTest(unittest.TestCase):
    def setUp(self):
        self.html = PACKAGED_INDEX.read_text(encoding="utf-8")

    def test_menu_trigger_and_container_exist(self):
        self.assertEqual(self.html.count('id="headerToolsBtn"'), 1)
        self.assertEqual(self.html.count('id="headerToolsMenu"'), 1)
        # 菜单默认收起，且是无障碍菜单
        self.assertIn('id="headerToolsMenu" class="cw-header-tools-menu hidden" role="menu"', self.html)
        self.assertIn('aria-haspopup="menu"', self.html)

    def test_menu_items_keep_original_ids(self):
        # 每个被收纳按钮的 id 全局唯一（handler 绑定不会歧义/丢失）
        for item_id in MENU_ITEM_IDS:
            self.assertEqual(
                self.html.count(f'id="{item_id}"'), 1, f"{item_id} 应恰好出现一次"
            )

    def test_menu_items_live_inside_menu(self):
        menu_block = _menu_block(self.html)
        for item_id in MENU_ITEM_IDS:
            self.assertIn(f'id="{item_id}"', menu_block, f"{item_id} 应在菜单容器内")

    def test_exposed_buttons_stay_out_of_menu(self):
        menu_block = _menu_block(self.html)
        for exposed_id in EXPOSED_IDS:
            self.assertNotIn(
                f'id="{exposed_id}"', menu_block, f"{exposed_id} 应外露，不进菜单"
            )

    def test_workspace_group_is_code_only(self):
        # 工作区分组带 cw-code-only，Chat 模式整组隐藏
        self.assertIn('<div class="cw-more-tool-group cw-code-only">', self.html)

    def test_two_index_html_identical(self):
        self.assertEqual(
            PACKAGED_INDEX.read_text(encoding="utf-8"),
            ROOT_INDEX.read_text(encoding="utf-8"),
            "packaged 与 root 的 index.html 必须字节一致",
        )

    def test_menu_i18n_keys_resolve_both_langs(self):
        zh = _flatten(json.loads(ZH.read_text(encoding="utf-8")))
        en = _flatten(json.loads(EN.read_text(encoding="utf-8")))
        menu_block = _menu_block(self.html)
        keys = set(re.findall(r'data-i18n="([^"]+)"', menu_block))
        self.assertIn("html.tools_group_workspace", keys)
        for key in keys:
            self.assertIn(key, zh, f"{key} 缺 zh 翻译")
            self.assertIn(key, en, f"{key} 缺 en 翻译")


if __name__ == "__main__":
    unittest.main()
