from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claude_web.code_context_ledger import CodeContextLedger, CodeContextLedgerError


def _create_sessions_table(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                cwd TEXT NOT NULL DEFAULT '',
                workspace_mode TEXT NOT NULL DEFAULT 'chat'
            )
            """
        )


def _insert_session(db_path: Path, session_id: str, cwd: Path, mode: str = "code") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id, cwd, workspace_mode) VALUES (?, ?, ?)",
            (session_id, str(cwd), mode),
        )


class CodeContextLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root_a = (base / "project-a").resolve()
        self.root_b = (base / "project-b").resolve()
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.db_path = base / "ledger.db"
        _create_sessions_table(self.db_path)
        _insert_session(self.db_path, "code-a", self.root_a)
        _insert_session(self.db_path, "code-b", self.root_b)
        _insert_session(self.db_path, "chat-a", self.root_a, "chat")
        self.ledger = CodeContextLedger(self.db_path)
        self.ledger.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_only_for_exact_code_session_and_server_owned_cwd(self) -> None:
        item = self.ledger.record_project_map_pack(
            "code-a",
            pack_id="pack-1",
            revision=7,
            descriptor={"node_ids": ["node-a"], "relationship_count": 2},
            token_estimate=120,
            expected_cwd=str(self.root_a),
        )
        self.assertEqual(str(self.root_a), item["canonical_cwd"])
        self.assertEqual("7", item["revision"])
        self.assertEqual("pack-1", item["descriptor"]["pack_id"])

        with self.assertRaisesRegex(CodeContextLedgerError, "仅支持 Code 会话") as error:
            self.ledger.record_user_pinned("chat-a", descriptor={"path": "README.md"})
        self.assertEqual("code_session_required", error.exception.code)
        with self.assertRaises(CodeContextLedgerError) as error:
            self.ledger.record_user_pinned(
                "code-a",
                descriptor={"path": "README.md"},
                expected_cwd=str(self.root_b),
            )
        self.assertEqual("workspace_mismatch", error.exception.code)

    def test_restart_persistence_and_cross_session_isolation(self) -> None:
        item_a = self.ledger.record_user_pinned(
            "code-a",
            descriptor={"path": "docs/plan.md", "reason": "user-selected"},
            token_estimate=50,
        )
        self.ledger.record_auto_retrieval(
            "code-b",
            descriptor={"symbol": "ProjectMapService", "path": "service.py"},
            token_estimate=80,
        )

        restarted = CodeContextLedger(self.db_path)
        restarted.initialize()
        listed_a = restarted.list("code-a")
        self.assertEqual(1, listed_a["total"])
        self.assertEqual(item_a["id"], listed_a["items"][0]["id"])
        self.assertEqual(1, restarted.list("code-b")["total"])
        with self.assertRaises(CodeContextLedgerError) as error:
            restarted.get("code-b", item_a["id"])
        self.assertEqual("ledger_forbidden", error.exception.code)

    def test_descriptor_and_token_budgets_reject_source_bodies(self) -> None:
        limited = CodeContextLedger(
            self.db_path,
            max_descriptor_bytes=300,
            max_token_estimate=200,
        )
        limited.initialize()
        with self.assertRaises(CodeContextLedgerError) as error:
            limited.record_auto_retrieval("code-a", descriptor={"source_code": "print('x')"})
        self.assertEqual("raw_content_forbidden", error.exception.code)
        for forbidden_key in ("excerpt", "body", "system_prompt", "user_prompt"):
            with self.subTest(forbidden_key=forbidden_key):
                with self.assertRaises(CodeContextLedgerError) as error:
                    limited.record_auto_retrieval(
                        "code-a", descriptor={forbidden_key: "sensitive project text"}
                    )
                self.assertEqual("raw_content_forbidden", error.exception.code)
        with self.assertRaises(CodeContextLedgerError) as error:
            limited.record_auto_retrieval(
                "code-a",
                descriptor={"paths": ["x" * 100, "y" * 100, "z" * 100]},
            )
        self.assertEqual("descriptor_too_large", error.exception.code)
        with self.assertRaises(CodeContextLedgerError) as error:
            limited.record_sdk_context_usage(
                "code-a",
                descriptor={"window_tokens": 1_000_000},
                token_estimate=201,
            )
        self.assertEqual("token_budget_exceeded", error.exception.code)

    def test_count_size_and_retention_limits_prune_old_rows(self) -> None:
        now = [100.0]
        ledger = CodeContextLedger(
            self.db_path,
            max_entries_per_session=2,
            max_descriptor_bytes=300,
            max_total_descriptor_bytes=300,
            retention_seconds=10,
            clock=lambda: now[0],
        )
        ledger.initialize()
        old = ledger.record_descriptor(
            "code-a",
            entry_type="auto_retrieval",
            source="retrieval",
            descriptor={"path": "old.py"},
            created_at=80,
        )
        first = ledger.record_auto_retrieval("code-a", descriptor={"path": "first.py", "refs": ["a" * 80]})
        second = ledger.record_auto_retrieval("code-a", descriptor={"path": "second.py", "refs": ["b" * 80]})
        third = ledger.record_auto_retrieval("code-a", descriptor={"path": "third.py", "refs": ["c" * 80]})

        listed = ledger.list("code-a")
        ids = {item["id"] for item in listed["items"]}
        self.assertNotIn(old["id"], ids)
        self.assertNotIn(first["id"], ids)
        self.assertLessEqual(listed["total"], 2)
        self.assertIn(third["id"], ids)
        self.assertIn(second["id"], ids)
        self.assertLessEqual(ledger.summary("code-a")["descriptor_bytes"], 300)

    def test_stale_and_native_compact_lifecycle(self) -> None:
        pack = self.ledger.record_project_map_pack(
            "code-a",
            pack_id="pack-before-compact",
            revision=3,
            descriptor={"node_ids": ["n1", "n2"]},
            token_estimate=100,
        )
        usage = self.ledger.record_sdk_context_usage(
            "code-a",
            descriptor={"total_tokens": 900, "window_tokens": 1000},
            token_estimate=900,
        )
        retrieval = self.ledger.record_auto_retrieval(
            "code-a",
            descriptor={"paths": ["service.py"]},
            token_estimate=70,
        )
        pinned = self.ledger.record_user_pinned(
            "code-a",
            descriptor={"path": "requirements.md", "reason": "keep"},
            token_estimate=30,
        )
        self.assertEqual(2, self.ledger.mark_stale("code-a", [pack["id"], retrieval["id"]]))

        compact = self.ledger.record_native_compact(
            "code-a",
            descriptor={"pre_tokens": 1070, "post_tokens": 180},
            token_estimate=180,
        )
        self.assertEqual("compacted", self.ledger.get("code-a", pack["id"])["lifecycle_state"])
        self.assertEqual("compacted", self.ledger.get("code-a", usage["id"])["lifecycle_state"])
        dropped = self.ledger.get("code-a", retrieval["id"])
        self.assertEqual("dropped", dropped["lifecycle_state"])
        self.assertEqual("auto_retrieval", dropped["compact_category"])
        self.assertEqual(compact["id"], dropped["compact_event_id"])
        self.assertEqual("active", self.ledger.get("code-a", pinned["id"])["lifecycle_state"])

        summary = self.ledger.summary("code-a")
        self.assertEqual(2, summary["stale_count"])
        self.assertEqual(2, summary["by_lifecycle"]["compacted"])
        self.assertEqual(1, summary["by_lifecycle"]["dropped"])
        self.assertEqual(2, summary["by_lifecycle"]["active"])
        self.assertEqual(compact["id"], summary["latest_native_compact"]["id"])

    def test_workspace_move_hides_old_ledger_and_rejects_direct_access(self) -> None:
        item = self.ledger.record_user_pinned("code-a", descriptor={"path": "before.py"})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET cwd = ? WHERE id = 'code-a'", (str(self.root_b),))
        self.assertEqual(0, self.ledger.list("code-a")["total"])
        with self.assertRaises(CodeContextLedgerError) as error:
            self.ledger.get("code-a", item["id"])
        self.assertEqual("workspace_mismatch", error.exception.code)


if __name__ == "__main__":
    unittest.main()
