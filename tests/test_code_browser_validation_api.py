import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from claude_web import server
from claude_web.code_browser_validation import CodeBrowserValidationError


class _FakeBrowserValidationRegistry:
    def __init__(self):
        self.recipe = {
            "id": "recipe-1", "name": "Code UI", "url": "http://127.0.0.1:8765",
            "viewport": {"width": 1440, "height": 900, "device_scale_factor": 1},
            "steps": [{"id": "open", "action": "navigate", "value": "/"}],
            "assertions": [], "server_command_suggestion": "npm run dev",
        }
        self.run = {
            "id": "run-1", "recipe_id": "recipe-1", "status": "queued",
            "reason_code": "", "reason": "", "evidence": None,
        }

    def list_recipes(self, session_id):
        if session_id == "chat":
            raise CodeBrowserValidationError("code_session_required", "浏览器验收仅支持 Code 会话")
        return [self.recipe]

    def create_recipe(self, session_id, **payload):
        return {**self.recipe, **payload}

    def get_recipe(self, session_id, recipe_id):
        if recipe_id == "foreign":
            raise CodeBrowserValidationError("validation_forbidden", "验收记录不属于当前 Code 会话")
        return self.recipe

    def list_runs(self, session_id, *, recipe_id=""):
        return [self.run] if not recipe_id or recipe_id == self.run["recipe_id"] else []

    def create_run(self, session_id, recipe_id, **payload):
        return {**self.run, "recipe_id": recipe_id, **payload}

    def get_run(self, session_id, run_id):
        if run_id == "missing":
            raise CodeBrowserValidationError("run_not_found", "浏览器验收运行不存在")
        return self.run

    def transition_run(self, session_id, run_id, status, **payload):
        return {**self.run, "status": status, **payload}

    def record_evidence(self, session_id, run_id, **payload):
        evidence = {
            "step_results": payload["step_results"],
            "assertion_results": payload["assertion_results"],
            "screenshots": payload["screenshots"],
            "console_summary": payload["console_summary"],
            "network_summary": payload["network_summary"],
        }
        return {**self.run, "status": "running", "evidence": evidence}


class CodeBrowserValidationApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = _FakeBrowserValidationRegistry()
        self.getter = patch.object(
            server, "_get_code_browser_validation_registry", return_value=self.registry,
        )
        self.getter.start()

    async def asyncTearDown(self):
        self.getter.stop()

    async def test_recipe_run_and_evidence_endpoints_are_session_scoped_records(self):
        listed = await server.list_code_browser_validation_recipes("owner")
        self.assertEqual("recipe-1", listed["recipes"][0]["id"])
        created = await server.create_code_browser_validation_recipe(
            "owner",
            server.CodeBrowserValidationRecipeRequest(
                name="Code UI", url="http://127.0.0.1:8765",
                viewport={"width": 1440, "height": 900},
                steps=[{"id": "open", "action": "navigate", "value": "/"}],
                assertions=[], server_command_suggestion="npm run dev",
            ),
        )
        self.assertEqual("npm run dev", created["recipe"]["server_command_suggestion"])

        run = await server.create_code_browser_validation_run(
            "owner", server.CodeBrowserValidationRunRequest(recipe_id="recipe-1"),
        )
        self.assertEqual("queued", run["run"]["status"])
        self.assertEqual("external", run["execution"])
        self.assertIn("外部浏览器执行器", run["message"])

        running = await server.update_code_browser_validation_run(
            "owner", "run-1",
            server.CodeBrowserValidationRunStatusRequest(status="running"),
        )
        self.assertEqual("running", running["run"]["status"])
        evidence = await server.record_code_browser_validation_evidence(
            "owner", "run-1",
            server.CodeBrowserValidationEvidenceRequest(
                step_results=[{"step_id": "open", "status": "passed"}],
                assertion_results=[],
            ),
        )
        self.assertEqual("open", evidence["evidence"]["step_results"][0]["step_id"])

    async def test_unavailable_is_explicitly_skipped_and_errors_are_stable(self):
        skipped = await server.update_code_browser_validation_run(
            "owner", "run-1",
            server.CodeBrowserValidationRunStatusRequest(
                status="skipped", reason_code="browser_unavailable", reason="not connected",
            ),
        )
        self.assertEqual("skipped", skipped["run"]["status"])
        self.assertEqual("browser_unavailable", skipped["run"]["reason_code"])

        with self.assertRaises(HTTPException) as chat:
            await server.list_code_browser_validation_recipes("chat")
        self.assertEqual(409, chat.exception.status_code)
        self.assertEqual("code_session_required", chat.exception.detail["code"])
        with self.assertRaises(HTTPException) as forbidden:
            await server.get_code_browser_validation_recipe("owner", "foreign")
        self.assertEqual(403, forbidden.exception.status_code)
        with self.assertRaises(HTTPException) as missing:
            await server.get_code_browser_validation_run("owner", "missing")
        self.assertEqual(404, missing.exception.status_code)
        self.assertEqual("run_not_found", missing.exception.detail["code"])

    def test_registry_initializes_once_with_separate_evidence_root(self):
        self.getter.stop()
        original = server._code_browser_validation_registry
        fake = MagicMock()
        try:
            server._code_browser_validation_registry = None
            with patch.object(server, "CodeBrowserValidationRegistry", return_value=fake) as constructor:
                result = server._get_code_browser_validation_registry()
                self.assertIs(fake, result)
                self.assertEqual(server.DB_PATH, constructor.call_args.args[0])
                self.assertEqual(
                    (server._DATA_DIR / "browser-validation-evidence").resolve(),
                    constructor.call_args.kwargs["evidence_root"],
                )
                fake.initialize.assert_called_once_with()
        finally:
            server._code_browser_validation_registry = original
            self.getter.start()


class CodeBrowserValidationFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_html = Path("claude_web/static/index.html").read_text(encoding="utf-8")
        cls.root_html = Path("static/index.html").read_text(encoding="utf-8")

    def test_ui_is_inside_code_review_validation_and_static_files_match(self):
        self.assertEqual(self.package_html, self.root_html)
        review_start = self.package_html.index('id="codeReviewModal"')
        review_end = self.package_html.index('id="rewindPreviewModal"', review_start)
        review = self.package_html[review_start:review_end]
        self.assertIn('id="cwBrowserValidation" class="cw-browser-validation cw-code-only"', review)
        self.assertIn('待外部浏览器执行器回传证据', review)
        self.assertIn('不会自动执行浏览器', review)

    def test_frontend_creates_records_without_browser_or_server_command_execution(self):
        start = self.package_html.index("let browserValidationRefreshSerial")
        end = self.package_html.index("async function toggleCodeInspectorExternalMenu", start)
        body = self.package_html[start:end]
        self.assertIn("/browser-validation/recipes", body)
        self.assertIn("/browser-validation/runs", body)
        self.assertIn("status:'skipped'", body)
        self.assertIn("JSON.stringify(evidence, null, 2)", body)
        self.assertIn("server_command_suggestion", body)
        self.assertIn("服务器命令建议（不执行）", body)
        self.assertNotIn("runCodeValidation(", body)
        self.assertNotIn("window.open(", body)

    def test_server_routes_delegate_blocking_registry_calls_to_threads(self):
        source = Path("claude_web/server.py").read_text(encoding="utf-8")
        start = source.index('@app.get("/api/sessions/{session_id}/browser-validation/recipes")')
        end = source.index('@app.post("/api/agent-loop/start")', start)
        routes = source[start:end]
        self.assertGreaterEqual(routes.count("await asyncio.to_thread"), 16)
        self.assertNotIn("subprocess", routes)


if __name__ == "__main__":
    unittest.main()
