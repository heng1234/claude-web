from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_web.code_worktrees import CodeWorktreeError, CodeWorktreeManager


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class CodeWorktreeManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.repo = base / "sample-repo"
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "tests@example.com")
        _git(self.repo, "config", "user.name", "Tests")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-m", "initial")
        self.db_path = base / "worktrees.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    cwd TEXT NOT NULL DEFAULT '',
                    workspace_mode TEXT NOT NULL DEFAULT 'chat',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE code_turn_requests (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE code_message_queue (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    state TEXT NOT NULL
                )
                """
            )
            conn.executemany(
                "INSERT INTO sessions (id, cwd, workspace_mode) VALUES (?, ?, ?)",
                [
                    ("source", str(self.repo), "code"),
                    ("other", str(self.repo), "code"),
                    ("chat", str(self.repo), "chat"),
                    ("not-git", str(base), "code"),
                ],
            )
        self.manager = CodeWorktreeManager(self.db_path)
        self.manager.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code: str, callback) -> CodeWorktreeError:
        with self.assertRaises(CodeWorktreeError) as raised:
            callback()
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_create_isolates_files_and_creates_a_distinct_code_session(self) -> None:
        created = self.manager.create("source", slug="task-one")
        path = Path(str(created["path"]))
        self.assertTrue(created["exists"])
        self.assertEqual("active", created["status"])
        self.assertEqual("claude-web/task-one", created["branch"])
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), created["head"])
        self.assertEqual(
            self.repo.resolve().parent / "sample-repo.claude-web-worktrees" / "task-one",
            path,
        )
        with sqlite3.connect(self.db_path) as conn:
            source_cwd = conn.execute(
                "SELECT cwd FROM sessions WHERE id = 'source'"
            ).fetchone()[0]
            derived = conn.execute(
                "SELECT cwd, workspace_mode FROM sessions WHERE id = ?",
                (created["worktree_session_id"],),
            ).fetchone()
        self.assertEqual(str(self.repo), source_cwd)
        self.assertEqual((str(path), "code"), derived)

        (path / "tracked.txt").write_text("isolated\n", encoding="utf-8")
        self.assertEqual("base\n", (self.repo / "tracked.txt").read_text(encoding="utf-8"))
        self.assertTrue(self.manager.get("source", str(created["id"]))["dirty"])
        self.assert_error(
            "worktree_forbidden",
            lambda: self.manager.get("other", str(created["id"])),
        )

    def test_slug_traversal_duplicates_and_non_code_or_non_git_sources_are_rejected(self) -> None:
        self.assert_error(
            "invalid_slug", lambda: self.manager.create("source", slug="../escape")
        )
        self.assert_error(
            "code_session_required", lambda: self.manager.create("chat", slug="chat-task")
        )
        self.assert_error(
            "git_repo_required", lambda: self.manager.create("not-git", slug="plain-task")
        )
        self.manager.create("source", slug="duplicate")
        self.assert_error(
            "duplicate_worktree", lambda: self.manager.create("source", slug="duplicate")
        )

    def test_remove_requires_confirmation_clean_tree_and_inactive_session(self) -> None:
        created = self.manager.create("source", slug="removal")
        worktree_id = str(created["id"])
        path = Path(str(created["path"]))
        self.assert_error(
            "confirmation_required",
            lambda: self.manager.remove("source", worktree_id, confirm=False),
        )
        (path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assert_error(
            "worktree_dirty",
            lambda: self.manager.remove("source", worktree_id, confirm=True),
        )
        _git(path, "checkout", "--", "tracked.txt")
        self.manager.set_runtime_active("source", worktree_id, True)
        self.assert_error(
            "worktree_active",
            lambda: self.manager.remove("source", worktree_id, confirm=True),
        )
        self.manager.set_runtime_active("source", worktree_id, False)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO code_turn_requests
                    (id, session_id, state, created_at, updated_at)
                VALUES ('turn', ?, 'starting', 0, 0)
                """,
                (created["worktree_session_id"],),
            )
        self.assert_error(
            "worktree_active",
            lambda: self.manager.remove("source", worktree_id, confirm=True),
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM code_turn_requests WHERE id = 'turn'")
        removed = self.manager.remove("source", worktree_id, confirm=True)
        self.assertEqual("removed", removed["status"])
        self.assertFalse(removed["exists"])
        self.assertFalse(path.exists())
        self.assertTrue(self.repo.exists())
        with sqlite3.connect(self.db_path) as conn:
            archived = conn.execute(
                "SELECT archived FROM sessions WHERE id = ?",
                (created["worktree_session_id"],),
            ).fetchone()[0]
        self.assertEqual(1, archived)

        # Idempotent cleanup also repairs older registry rows whose derived
        # session was not archived by a previous release.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET archived = 0 WHERE id = ?",
                (created["worktree_session_id"],),
            )
        again = self.manager.remove("source", worktree_id, confirm=True)
        self.assertEqual("removed", again["status"])
        with sqlite3.connect(self.db_path) as conn:
            repaired = conn.execute(
                "SELECT archived FROM sessions WHERE id = ?",
                (created["worktree_session_id"],),
            ).fetchone()[0]
        self.assertEqual(1, repaired)

    def test_restart_reconciles_registry_and_git_state(self) -> None:
        created = self.manager.create("source", slug="restart")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE code_worktrees SET status = 'creating' WHERE id = ?",
                (created["id"],),
            )
            conn.execute(
                "DELETE FROM sessions WHERE id = ?",
                (created["worktree_session_id"],),
            )
        restarted = CodeWorktreeManager(self.db_path)
        restarted.initialize()
        recovered = restarted.get("source", str(created["id"]))
        self.assertEqual("active", recovered["status"])
        self.assertTrue(recovered["exists"])
        with sqlite3.connect(self.db_path) as conn:
            session = conn.execute(
                "SELECT cwd, workspace_mode FROM sessions WHERE id = ?",
                (created["worktree_session_id"],),
            ).fetchone()
        self.assertEqual((created["path"], "code"), session)


if __name__ == "__main__":
    unittest.main()
