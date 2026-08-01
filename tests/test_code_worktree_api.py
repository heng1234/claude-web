import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from fastapi import HTTPException

from claude_web import server
from claude_web.code_worktrees import CodeWorktreeError


class _FakeWorktreeManager:
    def __init__(self):
        self.created = []
        self.removed = []
        self.items = [{
            "id": "wt-1",
            "source_session_id": "source",
            "worktree_session_id": "derived",
            "slug": "task-one",
            "branch": "claude-web/task-one",
            "base_ref": "HEAD",
            "path": "/tmp/repo.claude-web-worktrees/task-one",
            "status": "active",
            "exists": True,
            "dirty": False,
            "runtime_active": False,
        }]

    def list(self, session_id):
        if session_id == "chat":
            raise CodeWorktreeError("code_session_required", "Worktree 仅支持 Code 会话")
        return self.items

    def create(self, session_id, *, slug, branch, base_ref):
        self.created.append((session_id, slug, branch, base_ref))
        return {**self.items[0], "slug": slug, "branch": branch or f"claude-web/{slug}", "base_ref": base_ref}

    def get(self, session_id, worktree_id):
        if worktree_id == "foreign":
            raise CodeWorktreeError("worktree_forbidden", "Worktree 不属于当前 Code 会话")
        return self.items[0]

    def remove(self, session_id, worktree_id, *, confirm):
        if not confirm:
            raise CodeWorktreeError("confirmation_required", "删除 Worktree 需要显式确认")
        if worktree_id == "dirty":
            raise CodeWorktreeError("worktree_dirty", "Worktree 存在未提交修改，拒绝删除")
        self.removed.append((session_id, worktree_id, confirm))
        return {**self.items[0], "status": "removed", "exists": False}


class CodeWorktreeApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = _FakeWorktreeManager()
        self.getter = patch.object(server, "_get_code_worktree_manager", return_value=self.manager)
        self.getter.start()

    async def asyncTearDown(self):
        self.getter.stop()

    async def test_list_create_get_and_confirmed_remove_are_session_scoped(self):
        listed = await server.list_code_worktrees("source")
        self.assertEqual("wt-1", listed["worktrees"][0]["id"])

        created = await server.create_code_worktree(
            "source",
            server.CodeWorktreeCreateRequest(slug="api-fix", branch="", base_ref="main"),
        )
        self.assertEqual("derived", created["worktree"]["worktree_session_id"])
        self.assertEqual([("source", "api-fix", "", "main")], self.manager.created)

        loaded = await server.get_code_worktree("source", "wt-1")
        self.assertEqual("wt-1", loaded["worktree"]["id"])
        removed = await server.remove_code_worktree(
            "source", "wt-1", server.CodeWorktreeRemoveRequest(confirm=True),
        )
        self.assertEqual("removed", removed["worktree"]["status"])
        self.assertEqual([("source", "wt-1", True)], self.manager.removed)

    async def test_errors_have_stable_4xx_status_and_machine_code(self):
        with self.assertRaises(HTTPException) as chat:
            await server.list_code_worktrees("chat")
        self.assertEqual(409, chat.exception.status_code)
        self.assertEqual("code_session_required", chat.exception.detail["code"])

        with self.assertRaises(HTTPException) as foreign:
            await server.get_code_worktree("source", "foreign")
        self.assertEqual(403, foreign.exception.status_code)
        self.assertEqual("worktree_forbidden", foreign.exception.detail["code"])

        with self.assertRaises(HTTPException) as confirmation:
            await server.remove_code_worktree(
                "source", "wt-1", server.CodeWorktreeRemoveRequest(confirm=False),
            )
        self.assertEqual(400, confirmation.exception.status_code)
        self.assertEqual("confirmation_required", confirmation.exception.detail["code"])

        with self.assertRaises(HTTPException) as dirty:
            await server.remove_code_worktree(
                "source", "dirty", server.CodeWorktreeRemoveRequest(confirm=True),
            )
        self.assertEqual(409, dirty.exception.status_code)
        self.assertEqual("worktree_dirty", dirty.exception.detail["code"])

    def test_manager_initializes_with_live_session_activity_checker(self):
        self.getter.stop()
        original = server._code_worktree_manager
        fake = MagicMock()
        try:
            server._code_worktree_manager = None
            with patch.object(server, "CodeWorktreeManager", return_value=fake) as constructor, \
                    patch.object(server, "_session_control_busy", return_value=True) as busy:
                result = server._get_code_worktree_manager()
                self.assertIs(fake, result)
                constructor.assert_called_once_with(server.DB_PATH, activity_checker=ANY)
                checker = constructor.call_args.kwargs["activity_checker"]
                self.assertTrue(checker("derived"))
                busy.assert_called_once_with("derived")
                fake.initialize.assert_called_once_with()
        finally:
            server._code_worktree_manager = original
            self.getter.start()


class CodeWorktreeFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_html = Path("claude_web/static/index.html").read_text(encoding="utf-8")
        cls.root_html = Path("static/index.html").read_text(encoding="utf-8")

    def test_worktree_ui_is_code_only_and_duplicate_static_files_match(self):
        self.assertEqual(self.package_html, self.root_html)
        self.assertIn('id="cwWorktreesModal" class="modal-backdrop hidden cw-code-only"', self.package_html)
        self.assertIn('id="cwWorktreesBtn"', self.package_html)
        self.assertIn("if (!codeMode || !sessionId)", self.package_html)

    def test_create_navigates_to_derived_session_without_rebinding_current_cwd(self):
        start = self.package_html.index("cwWorktreeCreate?.addEventListener")
        end = self.package_html.index("function setPermissionModeMenuOpen", start)
        body = self.package_html[start:end]
        self.assertIn("await refreshSessions()", body)
        self.assertIn("loadSession(item.worktree_session_id", body)
        self.assertNotIn("cwdInput.value", body)
        self.assertNotIn("startNewSession", body)

    def test_delete_is_confirmed_and_disabled_for_dirty_or_active_without_force(self):
        start = self.package_html.index("function codeWorktreeDeleteReason")
        end = self.package_html.index("function setPermissionModeMenuOpen", start)
        body = self.package_html[start:end]
        self.assertIn("item.runtime_active", body)
        self.assertIn("item.dirty", body)
        self.assertIn("confirm(`确认删除 Worktree", body)
        self.assertIn("JSON.stringify({ confirm:true })", body)
        self.assertNotIn("force:", body)
        self.assertNotIn("delete_branch", body)


if __name__ == "__main__":
    unittest.main()
