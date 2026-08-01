import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException

from claude_web import server


class CodeGitIndexTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-web-index-")
        self.repo = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "initial")
        self.session_id = "index-" + uuid.uuid4().hex
        server.upsert_session(self.session_id, "index", str(self.repo), "code")
        server.set_session_runtime_origin(self.session_id, server._RUNTIME_ORIGIN_AGENT_SDK)

    def tearDown(self):
        server._agent_sdk_running_sessions.discard(self.session_id)
        server.save_events(self.session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    async def status(self, *paths: str) -> dict:
        result = await server.code_change_index_status(
            self.session_id,
            server.CodeIndexStatusRequest(paths=list(paths)),
        )
        return {item["requested_path"]: item for item in result["items"]}

    async def apply(self, path: str, action: str, etag: str) -> dict:
        return await server.update_code_change_index(
            self.session_id,
            server.CodeIndexActionRequest(path=path, action=action, expected_etag=etag),
        )

    async def test_stage_and_unstage_keep_review_state_orthogonal(self):
        change_set_id = uuid.uuid4().hex
        server.save_events(self.session_id, [{
            "type": "result",
            "change_set_id": change_set_id,
            "changed_files": [{"path": "tracked.txt", "review_state": "pending"}],
        }])
        (self.repo / "tracked.txt").write_text("first edit\n", encoding="utf-8")

        before = (await self.status("tracked.txt"))["tracked.txt"]
        self.assertEqual("unstaged", before["index_state"])
        staged = await self.apply("tracked.txt", "stage", before["etag"])
        self.assertEqual("staged", staged["item"]["index_state"])
        self.assertTrue(staged["review_state_unchanged"])
        self.assertEqual(
            "pending",
            server.load_events(self.session_id)[0]["changed_files"][0]["review_state"],
        )

        (self.repo / "tracked.txt").write_text("second edit\n", encoding="utf-8")
        partial = (await self.status("tracked.txt"))["tracked.txt"]
        self.assertEqual("partial", partial["index_state"])
        unstaged = await self.apply("tracked.txt", "unstage", partial["etag"])
        self.assertEqual("unstaged", unstaged["item"]["index_state"])
        self.assertEqual(
            "pending",
            server.load_events(self.session_id)[0]["changed_files"][0]["review_state"],
        )

    async def test_untracked_delete_and_staged_rename_have_useful_states(self):
        draft = self.repo / "draft.txt"
        draft.write_text("draft\n", encoding="utf-8")
        untracked = (await self.status("draft.txt"))["draft.txt"]
        self.assertEqual("untracked", untracked["index_state"])
        staged_draft = await self.apply("draft.txt", "stage", untracked["etag"])
        self.assertEqual("staged", staged_draft["item"]["index_state"])
        unstaged_draft = await self.apply("draft.txt", "unstage", staged_draft["item"]["etag"])
        self.assertEqual("untracked", unstaged_draft["item"]["index_state"])

        (self.repo / "tracked.txt").unlink()
        deleted = (await self.status("tracked.txt"))["tracked.txt"]
        self.assertEqual("unstaged", deleted["index_state"])
        self.assertTrue(deleted["deleted"])
        staged_delete = await self.apply("tracked.txt", "stage", deleted["etag"])
        self.assertEqual("staged", staged_delete["item"]["index_state"])
        self.assertTrue(staged_delete["item"]["deleted"])
        await self.apply("tracked.txt", "unstage", staged_delete["item"]["etag"])

        self.git("restore", "tracked.txt")
        self.git("mv", "tracked.txt", "renamed.txt")
        renamed = (await self.status("renamed.txt"))["renamed.txt"]
        self.assertEqual("staged", renamed["index_state"])
        self.assertTrue(renamed["renamed"])
        self.assertEqual("tracked.txt", renamed["old_path"])
        rename_unstaged = await self.apply("renamed.txt", "unstage", renamed["etag"])
        self.assertIn(rename_unstaged["item"]["index_state"], {"unstaged", "untracked"})

    async def test_etag_and_busy_checks_reject_races(self):
        target = self.repo / "tracked.txt"
        target.write_text("first\n", encoding="utf-8")
        stale = (await self.status("tracked.txt"))["tracked.txt"]
        target.write_text("second\n", encoding="utf-8")
        with self.assertRaises(HTTPException) as raised:
            await self.apply("tracked.txt", "stage", stale["etag"])
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("", self.git("diff", "--cached", "--", "tracked.txt"))

        fresh = (await self.status("tracked.txt"))["tracked.txt"]
        server._agent_sdk_running_sessions.add(self.session_id)
        with self.assertRaises(HTTPException) as busy:
            await self.apply("tracked.txt", "stage", fresh["etag"])
        self.assertEqual(409, busy.exception.status_code)
        server._agent_sdk_running_sessions.discard(self.session_id)

    async def test_chat_and_invalid_paths_are_rejected(self):
        chat_id = "index-chat-" + uuid.uuid4().hex
        server.upsert_session(chat_id, "chat", str(self.repo), "chat")
        try:
            with self.assertRaises(HTTPException) as wrong_mode:
                await server.code_change_index_status(
                    chat_id,
                    server.CodeIndexStatusRequest(paths=["tracked.txt"]),
                )
            self.assertEqual(409, wrong_mode.exception.status_code)
            with self.assertRaises(HTTPException) as outside:
                await server.code_change_index_status(
                    self.session_id,
                    server.CodeIndexStatusRequest(paths=["../outside.txt"]),
                )
            self.assertEqual(400, outside.exception.status_code)
        finally:
            with server.db_connect() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (chat_id,))

    def test_porcelain_parser_marks_conflicts_and_renames(self):
        entries = server._parse_git_porcelain_entries("UU conflict.txt\0R  renamed.txt\0old.txt\0")
        by_path = {item["path"]: item for item in entries}
        self.assertEqual("conflicted", by_path["conflict.txt"]["index_state"])
        self.assertTrue(by_path["conflict.txt"]["conflicted"])
        self.assertEqual("staged", by_path["renamed.txt"]["index_state"])
        self.assertTrue(by_path["renamed.txt"]["renamed"])
        self.assertEqual("old.txt", by_path["renamed.txt"]["old_path"])


class UnbornCodeGitIndexTest(unittest.IsolatedAsyncioTestCase):
    async def test_unstage_in_repository_without_head_keeps_file_untracked(self):
        with tempfile.TemporaryDirectory(prefix="claude-web-unborn-index-") as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            (repo / "first.txt").write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "first.txt"], check=True)
            session_id = "index-unborn-" + uuid.uuid4().hex
            server.upsert_session(session_id, "unborn", str(repo), "code")
            server.set_session_runtime_origin(session_id, server._RUNTIME_ORIGIN_AGENT_SDK)
            try:
                status = await server.code_change_index_status(
                    session_id,
                    server.CodeIndexStatusRequest(paths=["first.txt"]),
                )
                self.assertFalse(status["head_available"])
                item = status["items"][0]
                self.assertEqual("staged", item["index_state"])
                result = await server.update_code_change_index(
                    session_id,
                    server.CodeIndexActionRequest(
                        path="first.txt",
                        action="unstage",
                        expected_etag=item["etag"],
                    ),
                )
                self.assertEqual("untracked", result["item"]["index_state"])
                self.assertTrue((repo / "first.txt").exists())
            finally:
                with server.db_connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
