from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claude_web.code_browser_validation import (
    CodeBrowserValidationError,
    CodeBrowserValidationRegistry,
)


class CodeBrowserValidationRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.project = base / "project"
        self.project.mkdir()
        self.evidence_root = base / "app-evidence"
        self.evidence_root.mkdir()
        self.db_path = base / "validation.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL DEFAULT '',
                    workspace_mode TEXT NOT NULL DEFAULT 'chat'
                )
                """
            )
            conn.executemany(
                "INSERT INTO sessions (id, cwd, workspace_mode) VALUES (?, ?, ?)",
                [
                    ("owner", str(self.project), "code"),
                    ("other", str(self.project), "code"),
                    ("chat", str(self.project), "chat"),
                ],
            )
        self.registry = CodeBrowserValidationRegistry(
            self.db_path, evidence_root=self.evidence_root
        )
        self.registry.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> CodeBrowserValidationError:
        with self.assertRaises(CodeBrowserValidationError) as raised:
            callback()
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def create_recipe(self):
        return self.registry.create_recipe(
            "owner",
            name="Code 页面基础验收",
            url="http://127.0.0.1:8765/code",
            viewport={"width": 1440, "height": 900, "device_scale_factor": 1},
            steps=[
                {"id": "open", "action": "navigate", "value": "/code"},
                {"id": "click-map", "action": "click", "target": "#project-map"},
            ],
            assertions=[
                {"id": "map-visible", "type": "visible", "target": "#project-map-panel"},
            ],
            server_command_suggestion="touch should-not-exist",
            change_set_id="changes-1",
            revision=7,
        )

    def test_recipe_and_run_are_persistent_and_server_command_is_record_only(self) -> None:
        recipe = self.create_recipe()
        self.assertEqual("http://127.0.0.1:8765/code", recipe["url"])
        self.assertEqual(1440, recipe["viewport"]["width"])
        self.assertEqual("changes-1", recipe["change_set_id"])
        self.assertEqual(7, recipe["revision"])
        self.assertFalse((self.project / "should-not-exist").exists())

        run = self.registry.create_run("owner", str(recipe["id"]))
        self.assertEqual("queued", run["status"])
        self.assertEqual("changes-1", run["change_set_id"])
        restarted = CodeBrowserValidationRegistry(
            self.db_path, evidence_root=self.evidence_root
        )
        restarted.initialize()
        self.assertEqual(recipe["id"], restarted.list_recipes("owner")[0]["id"])
        self.assertEqual(run["id"], restarted.list_runs("owner")[0]["id"])

    def test_evidence_paths_and_pass_state_are_strictly_validated(self) -> None:
        recipe = self.create_recipe()
        run = self.registry.create_run("owner", str(recipe["id"]))
        self.registry.transition_run("owner", str(run["id"]), "running")
        project_shot = self.project / "artifacts" / "page.png"
        project_shot.parent.mkdir()
        project_shot.write_bytes(b"png")
        app_shot = self.evidence_root / "console.webp"
        app_shot.write_bytes(b"webp")
        recorded = self.registry.record_evidence(
            "owner",
            str(run["id"]),
            step_results=[
                {"step_id": "open", "status": "passed", "duration_ms": 15},
                {"step_id": "click-map", "status": "passed", "duration_ms": 20},
            ],
            assertion_results=[
                {
                    "assertion_id": "map-visible", "status": "passed",
                    "actual": True, "expected": True,
                },
            ],
            screenshots=[str(project_shot), str(app_shot)],
            console_summary=[{"level": "info", "message": "ready"}],
            network_summary=[
                {"method": "GET", "url": "http://127.0.0.1:8765/api", "status": 200}
            ],
        )
        self.assertEqual(2, len(recorded["evidence"]["screenshots"]))
        passed = self.registry.transition_run("owner", str(run["id"]), "passed")
        self.assertEqual("passed", passed["status"])

        outside = Path(self.temporary.name).parent / "outside-validation.png"
        outside.write_bytes(b"png")
        try:
            second = self.registry.create_run("owner", str(recipe["id"]))
            self.registry.transition_run("owner", str(second["id"]), "running")
            self.assert_error(
                "invalid_screenshot",
                lambda: self.registry.record_evidence(
                    "owner", str(second["id"]), step_results=[], assertion_results=[],
                    screenshots=[str(outside)],
                ),
            )
        finally:
            outside.unlink(missing_ok=True)

    def test_failed_evidence_cannot_pass_and_unavailable_is_skipped(self) -> None:
        recipe = self.create_recipe()
        run = self.registry.create_run("owner", str(recipe["id"]))
        self.registry.transition_run("owner", str(run["id"]), "running")
        self.registry.record_evidence(
            "owner",
            str(run["id"]),
            step_results=[{"step_id": "open", "status": "failed", "detail": "timeout"}],
            assertion_results=[],
        )
        self.assert_error(
            "failed_evidence",
            lambda: self.registry.transition_run("owner", str(run["id"]), "passed"),
        )
        failed = self.registry.transition_run(
            "owner", str(run["id"]), "failed", reason_code="assertion_failed"
        )
        self.assertEqual("failed", failed["status"])

        unavailable = self.registry.create_run("owner", str(recipe["id"]))
        skipped = self.registry.mark_unavailable(
            "owner", str(unavailable["id"]), "浏览器扩展未连接"
        )
        self.assertEqual("skipped", skipped["status"])
        self.assertEqual("browser_unavailable", skipped["reason_code"])

        invalid = self.registry.create_run("owner", str(recipe["id"]))
        self.registry.transition_run("owner", str(invalid["id"]), "running")
        self.assert_error(
            "unavailable_must_skip",
            lambda: self.registry.transition_run(
                "owner", str(invalid["id"]), "failed",
                reason_code="browser_unavailable",
            ),
        )

    def test_exact_session_workspace_and_payload_limits_are_enforced(self) -> None:
        recipe = self.create_recipe()
        self.assertEqual([], self.registry.list_recipes("other"))
        self.assert_error(
            "validation_forbidden",
            lambda: self.registry.get_recipe("other", str(recipe["id"])),
        )
        self.assert_error(
            "code_session_required",
            lambda: self.registry.list_recipes("chat"),
        )
        self.assert_error(
            "invalid_url",
            lambda: self.registry.create_recipe(
                "owner", name="bad", url="file:///tmp/page.html",
                viewport={"width": 800, "height": 600},
                steps=[{"action": "navigate"}], assertions=[],
            ),
        )
        self.assert_error(
            "invalid_steps",
            lambda: self.registry.create_recipe(
                "owner", name="too-many", url="https://localhost:8765",
                viewport={"width": 800, "height": 600},
                steps=[{"id": f"step-{index}", "action": "click"} for index in range(51)],
                assertions=[],
            ),
        )
        run = self.registry.create_run("owner", str(recipe["id"]))
        self.registry.transition_run("owner", str(run["id"]), "running")
        self.assert_error(
            "invalid_evidence",
            lambda: self.registry.record_evidence(
                "owner", str(run["id"]),
                step_results=[{"step_id": "unknown", "status": "passed"}],
                assertion_results=[],
            ),
        )
        self.assert_error(
            "invalid_evidence",
            lambda: self.registry.record_evidence(
                "owner", str(run["id"]), step_results=[], assertion_results=[],
                console_summary=[{"level": "info", "message": "x"} for _ in range(101)],
            ),
        )
        self.registry.record_evidence(
            "owner", str(run["id"]), step_results=[], assertion_results=[]
        )
        self.assert_error(
            "assertions_incomplete",
            lambda: self.registry.transition_run("owner", str(run["id"]), "passed"),
        )
        moved = self.project.parent / "moved"
        moved.mkdir()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET cwd = ? WHERE id = 'owner'", (str(moved),))
        self.assertEqual([], self.registry.list_recipes("owner"))
        self.assert_error(
            "workspace_changed",
            lambda: self.registry.get_recipe("owner", str(recipe["id"])),
        )


if __name__ == "__main__":
    unittest.main()
