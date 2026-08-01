import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "claude_web" / "static" / "project-map.js"
STYLE = ROOT / "claude_web" / "static" / "project-map.css"


class ProjectMapExplorerFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = SCRIPT.read_text(encoding="utf-8")
        cls.style = STYLE.read_text(encoding="utf-8")

    def test_search_prioritizes_exact_and_prefix_matches(self):
        self.assertIn("if (title === needle) score = 500", self.script)
        self.assertIn("else if (title.startsWith(needle)) score = 400", self.script)
        self.assertNotIn("syncSelectionWithVisibleNodes();\n      renderContent();\n      const input", self.script)

    def test_canvas_uses_honest_render_counts_and_local_neighborhood(self):
        self.assertIn("graph.nodes.length}/${graph.totalNodes}", self.script)
        self.assertIn("graph.relations.length}/${graph.totalRelations}", self.script)
        self.assertIn("state.viewMode === 'overview'", self.script)
        self.assertIn("relation.source_id === selected || relation.target_id === selected", self.script)

    def test_graph_has_roving_focus_pan_zoom_and_relation_inspector(self):
        self.assertIn("tabindex=\"${active ? '0' : '-1'}\"", self.script)
        self.assertIn("moveGraphFocus", self.script)
        self.assertIn("bindCanvasNavigation", self.script)
        self.assertIn("data-pm-edge", self.script)
        self.assertIn("关系证据", self.script)
        self.assertIn(".pm-edge-hit", self.style)

    def test_all_kinds_and_relationship_types_are_filterable(self):
        self.assertNotIn(".slice(0, 6)", self.script)
        self.assertIn("relationTypes()", self.script)
        self.assertIn("data-pm-relation", self.script)

    def test_history_freshness_and_context_actions_are_revision_bound(self):
        self.assertIn("/revisions?limit=50", self.script)
        self.assertIn("/revisions/compare?from_revision=", self.script)
        self.assertIn("/context-packs", self.script)
        self.assertIn("expected_revision: state.revision", self.script)
        self.assertIn("state.adapter?.prefillPlan", self.script)
        self.assertIn("state.adapter?.prefillTask", self.script)
        self.assertNotIn("state.adapter?.send", self.script)

    def test_accessible_list_remains_available_when_graph_is_hidden_on_mobile(self):
        self.assertIn('aria-label="项目节点列表"', self.script)
        self.assertIn("@media (max-width: 760px)", self.style)
        self.assertIn(".pm-canvas-panel", self.style)
        self.assertIn("display: none", self.style)


if __name__ == "__main__":
    unittest.main()
