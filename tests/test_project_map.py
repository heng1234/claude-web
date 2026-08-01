import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import HTTPException

from claude_web.project_map.scanner import ProjectScanner, ScanLimits, ScanResult
from claude_web.project_map.service import ProjectMapService
from claude_web.project_map.storage import (
    ProjectMapPartialRefreshRejected,
    ProjectMapPublishCancelled,
    ProjectMapStorage,
)


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


def _insert_session(db_path: Path, session_id: str, cwd: Path, mode: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id, cwd, workspace_mode) VALUES (?, ?, ?)",
            (session_id, str(cwd), mode),
        )


def _empty_dataset(storage_key: str, cwd: Path) -> dict:
    return {
        "manifest": {
            "storage_key": storage_key,
            "workspace_path": str(cwd),
        },
        "profile": {},
        "files": [],
        "evidence": [],
        "nodes": [],
        "relations": [],
    }


class ProjectScannerTest(unittest.TestCase):
    def test_scanner_extracts_supported_evidence_and_excludes_secrets_and_escape_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "api.py").write_text(
                "import os\n"
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {'ok': True}\n"
                "class A:\n"
                "    def save(self): return os.getcwd()\n"
                "class B:\n"
                "    def save(self): return os.getcwd()\n",
                encoding="utf-8",
            )
            (root / "client.js").write_text(
                "export const loadHealth = () => fetch('/health');\n",
                encoding="utf-8",
            )
            (root / ".env").write_text("API_TOKEN=do-not-read\n", encoding="utf-8")
            (root / "credentials.json").write_text('{"token":"do-not-read"}', encoding="utf-8")
            (root / ".mcp.json").write_text(
                '{"mcpServers":{"x":{"env":{"API_KEY":"do-not-read"}}}}',
                encoding="utf-8",
            )
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text(
                '{"env":{"API_KEY":"do-not-read"}}',
                encoding="utf-8",
            )
            (root / "secrets").mkdir()
            (root / "secrets" / "config.yaml").write_text("fixture: excluded-secret-file\n", encoding="utf-8")
            (root / "binary.txt").write_bytes(b"hello\0secret")
            outside = root.parent / f"{root.name}-outside.py"
            outside.write_text("def escaped(): pass\n", encoding="utf-8")
            try:
                (root / "escape.py").symlink_to(outside)
                result = ProjectScanner(ScanLimits(max_seconds=2)).scan(root)
            finally:
                outside.unlink(missing_ok=True)

            paths = {item["path"] for item in result.files}
            self.assertIn("api.py", paths)
            self.assertIn("client.js", paths)
            self.assertNotIn(".env", paths)
            self.assertNotIn("credentials.json", paths)
            self.assertNotIn(".mcp.json", paths)
            self.assertNotIn(".claude/settings.json", paths)
            self.assertNotIn("secrets/config.yaml", paths)
            self.assertNotIn("binary.txt", paths)
            self.assertNotIn("escape.py", paths)
            labels = {item.label for item in result.evidence}
            self.assertIn("GET /health", labels)
            self.assertIn("FETCH /health", labels)
            self.assertIn("A.save", labels)
            self.assertIn("B.save", labels)
            method_evidence = [
                item.id for item in result.evidence
                if item.label in {"A.save", "B.save"}
            ]
            self.assertEqual(2, len(set(method_evidence)))

    def test_scanner_reports_budget_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(4):
                (root / f"file_{index}.py").write_text(f"def item_{index}(): pass\n", encoding="utf-8")
            result = ProjectScanner(ScanLimits(max_files=2)).scan(root)
            self.assertTrue(result.partial)
            self.assertEqual("file_limit", result.partial_reason)
            self.assertEqual(2, len(result.files))

    def test_non_git_walk_prunes_directories_at_depth_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root
            for index in range(6):
                current = current / f"level-{index}"
                current.mkdir()
            (current / "too-deep.py").write_text("VALUE = 1\n", encoding="utf-8")
            result = ProjectScanner(ScanLimits(max_depth=2, max_seconds=2)).scan(root)
            paths = {item["path"] for item in result.files}
            self.assertNotIn("level-0/level-1/level-2/level-3/level-4/level-5/too-deep.py", paths)


class ProjectMapStorageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.root = self.root.resolve()
        self.db_path = Path(self.temporary.name) / "project-map.db"
        _create_sessions_table(self.db_path)
        self.storage = ProjectMapStorage(self.db_path)
        self.storage.initialize()
        _insert_session(self.db_path, "session-a", self.root, "code")

    def tearDown(self):
        self.temporary.cleanup()

    def test_snapshot_publish_uses_compare_and_swap_and_keeps_previous_revision(self):
        key = "workspace-key"
        for run_id in ("run-a", "run-b"):
            self.storage.create_run(
                run_id=run_id,
                owner_session_id="session-a",
                storage_key=key,
                canonical_cwd=str(self.root),
                base_revision=0,
                model="",
                effort="",
                preferred_language="zh",
            )
        revision = self.storage.publish_snapshot(
            run_id="run-a",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            dataset=_empty_dataset(key, self.root),
            files=[],
            source_root_hash="hash-a",
            scanner_version="scanner-v1",
            prompt_version="prompt-v1",
        )
        superseded = self.storage.publish_snapshot(
            run_id="run-b",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            dataset=_empty_dataset(key, self.root),
            files=[],
            source_root_hash="hash-b",
            scanner_version="scanner-v1",
            prompt_version="prompt-v1",
        )
        self.assertEqual(1, revision)
        self.assertIsNone(superseded)
        snapshot = self.storage.latest_snapshot(key)
        self.assertEqual(1, snapshot["revision"])
        self.assertEqual("hash-a", snapshot["source_root_hash"])

    def test_active_run_creation_is_atomic_per_workspace(self):
        key = "workspace-key"

        def create(index: int):
            return self.storage.create_run_if_idle(
                run_id=f"run-{index}",
                owner_session_id=f"session-{index}",
                storage_key=key,
                canonical_cwd=str(self.root),
                base_revision=0,
                model="",
                effort="",
                preferred_language="zh",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(create, range(4)))

        rows = []
        with self.storage.connect() as conn:
            rows = conn.execute(
                "SELECT run_id FROM project_map_runs WHERE storage_key = ?",
                (key,),
            ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(1, sum(result is None for result in results))
        self.assertEqual(3, sum(result is not None for result in results))

    def test_initialize_marks_unfinished_runs_interrupted_without_replay(self):
        self.storage.create_run(
            run_id="run-restart",
            owner_session_id="session-a",
            storage_key="workspace-key",
            canonical_cwd=str(self.root),
            base_revision=0,
            model="",
            effort="",
            preferred_language="zh",
        )
        ProjectMapStorage(self.db_path).initialize()
        run = self.storage.run("run-restart")
        self.assertEqual("interrupted", run["status"])
        self.assertEqual("service_restarted", run["error_category"])
        self.assertEqual("interrupted", self.storage.events_after("run-restart", 0)[-1]["status"])

    def test_cancel_and_cwd_change_are_rechecked_inside_publish_transaction(self):
        key = "workspace-key"
        self.storage.create_run(
            run_id="cancelled-run",
            owner_session_id="session-a",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            model="",
            effort="",
            preferred_language="zh",
        )
        self.storage.request_cancel("cancelled-run")
        with self.assertRaises(ProjectMapPublishCancelled):
            self.storage.publish_snapshot(
                run_id="cancelled-run",
                storage_key=key,
                canonical_cwd=str(self.root),
                base_revision=0,
                dataset=_empty_dataset(key, self.root),
                files=[],
                source_root_hash="hash",
                scanner_version="scanner-v1",
                prompt_version="prompt-v1",
            )

        self.storage.create_run(
            run_id="moved-run",
            owner_session_id="session-a",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            model="",
            effort="",
            preferred_language="zh",
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET cwd = ? WHERE id = 'session-a'",
                (str(self.root.parent),),
            )
        revision = self.storage.publish_snapshot(
            run_id="moved-run",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            dataset=_empty_dataset(key, self.root),
            files=[],
            source_root_hash="hash",
            scanner_version="scanner-v1",
            prompt_version="prompt-v1",
        )
        self.assertIsNone(revision)
        self.assertIsNone(self.storage.latest_snapshot(key))

    def test_partial_refresh_does_not_replace_complete_snapshot(self):
        key = "workspace-key"
        self.storage.create_run(
            run_id="complete-run", owner_session_id="session-a", storage_key=key,
            canonical_cwd=str(self.root), base_revision=0, model="", effort="",
            preferred_language="zh",
        )
        complete = _empty_dataset(key, self.root)
        complete["manifest"]["partial"] = False
        self.assertEqual(1, self.storage.publish_snapshot(
            run_id="complete-run", storage_key=key, canonical_cwd=str(self.root),
            base_revision=0, dataset=complete, files=[], source_root_hash="complete",
            scanner_version="scanner-v1", prompt_version="prompt-v1",
        ))
        self.storage.create_run(
            run_id="partial-run", owner_session_id="session-a", storage_key=key,
            canonical_cwd=str(self.root), base_revision=1, model="", effort="",
            preferred_language="zh",
        )
        partial = _empty_dataset(key, self.root)
        partial["manifest"].update({"partial": True, "partial_reason": "time_limit"})
        with self.assertRaises(ProjectMapPartialRefreshRejected):
            self.storage.publish_snapshot(
                run_id="partial-run", storage_key=key, canonical_cwd=str(self.root),
                base_revision=1, dataset=partial, files=[], source_root_hash="partial",
                scanner_version="scanner-v1", prompt_version="prompt-v1",
            )
        snapshot = self.storage.latest_snapshot(key)
        self.assertEqual(1, snapshot["revision"])
        self.assertEqual("complete", snapshot["completeness"])

    def test_snapshot_metadata_history_and_context_pack_storage_are_additive(self):
        key = "workspace-key"
        self.storage.create_run(
            run_id="history-run", owner_session_id="session-a", storage_key=key,
            canonical_cwd=str(self.root), base_revision=0, model="", effort="",
            preferred_language="zh",
        )
        self.storage.publish_snapshot(
            run_id="history-run", storage_key=key, canonical_cwd=str(self.root),
            base_revision=0, dataset=_empty_dataset(key, self.root), files=[],
            source_root_hash="root-hash", scanner_version="scanner-v2",
            prompt_version="prompt-v3",
        )
        listed = self.storage.list_snapshots(key, limit=10, offset=0)
        self.assertEqual(1, listed["total"])
        self.assertEqual("scanner-v2", listed["items"][0]["scanner_version"])
        self.assertEqual("prompt-v3", self.storage.snapshot(key, 1)["prompt_version"])
        self.storage.create_context_pack(
            pack_id="opaque-pack", owner_session_id="session-a", storage_key=key,
            canonical_cwd=str(self.root), revision=1, payload={"nodes": []},
            file_hashes={}, expires_at=10**12,
        )
        pack = self.storage.context_pack("opaque-pack")
        self.assertEqual("session-a", pack["owner_session_id"])
        self.assertEqual({"nodes": []}, pack["payload"])


class ProjectMapServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "project"
        self.root.mkdir()
        self.root = self.root.resolve()
        self.db_path = base / "service.db"
        _create_sessions_table(self.db_path)
        self.service = ProjectMapService(self.db_path)
        await self.service.startup()

    async def asyncTearDown(self):
        await self.service.shutdown()
        self.temporary.cleanup()

    async def test_only_code_sessions_with_specific_project_roots_are_allowed(self):
        _insert_session(self.db_path, "chat-session", self.root, "chat")
        _insert_session(self.db_path, "code-session", self.root, "code")
        _insert_session(self.db_path, "home-session", Path.home(), "code")
        _insert_session(self.db_path, "root-session", Path("/"), "code")

        with self.assertRaises(HTTPException) as chat_error:
            await self.service.get_map("chat-session")
        self.assertEqual(409, chat_error.exception.status_code)
        self.assertIn("Code", str(chat_error.exception.detail))

        result = await self.service.get_map("code-session")
        self.assertTrue(result["ok"])
        self.assertFalse(result["exists"])
        self.assertEqual(self.root.name, result["project_name"])

        with self.assertRaises(HTTPException) as home_error:
            await self.service.get_map("home-session")
        self.assertEqual(409, home_error.exception.status_code)
        with self.assertRaises(HTTPException) as root_error:
            await self.service.get_map("root-session")
        self.assertEqual(409, root_error.exception.status_code)

    async def test_same_canonical_project_shares_storage_key_but_other_project_isolated(self):
        other = self.root.parent / "other"
        other.mkdir()
        _insert_session(self.db_path, "code-a", self.root, "code")
        _insert_session(self.db_path, "code-b", self.root / ".", "code")
        _insert_session(self.db_path, "code-c", other, "code")

        a = await self.service.get_map("code-a")
        b = await self.service.get_map("code-b")
        c = await self.service.get_map("code-c")
        self.assertEqual(a["storage_key"], b["storage_key"])
        self.assertNotEqual(a["storage_key"], c["storage_key"])

    async def test_generation_is_blocked_during_agent_sdk_maintenance(self):
        _insert_session(self.db_path, "maintenance-session", self.root, "code")
        self.service._generation_blocked = lambda: True
        with self.assertRaises(HTTPException) as raised:
            await self.service.start_run("maintenance-session")
        self.assertEqual(409, raised.exception.status_code)

    async def test_run_registration_holds_shared_sdk_maintenance_lock(self):
        lock = asyncio.Lock()
        service = ProjectMapService(self.db_path, maintenance_lock=lock)
        _insert_session(self.db_path, "locked-registration", self.root, "code")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def register(*args, **kwargs):
            self.assertTrue(lock.locked())
            entered.set()
            await release.wait()
            return {"ok": True}

        service._register_run = register
        task = asyncio.create_task(service.start_run("locked-registration"))
        await entered.wait()
        self.assertTrue(lock.locked())
        release.set()
        self.assertEqual({"ok": True}, await task)
        self.assertFalse(lock.locked())

    async def test_run_access_is_project_scoped(self):
        other = self.root.parent / "other"
        other.mkdir()
        _insert_session(self.db_path, "owner", self.root, "code")
        _insert_session(self.db_path, "same-project", self.root, "code")
        _insert_session(self.db_path, "other-project", other, "code")
        _, _, key = self.service.resolve_code_project("owner")
        self.service.storage.create_run(
            run_id="owned-run",
            owner_session_id="owner",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            model="",
            effort="",
            preferred_language="zh",
        )

        self.service.validate_run_access("same-project", "owned-run")
        with self.assertRaises(HTTPException) as raised:
            self.service.validate_run_access("other-project", "owned-run")
        self.assertEqual(403, raised.exception.status_code)

    async def test_generation_uses_dedicated_analysis_profile(self):
        captured = {}

        class Turn:
            async def events(self):
                yield {
                    "type": "event",
                    "event": {
                        "type": "result",
                        "structured_output": {"nodes": [], "relations": []},
                    },
                }

        async def open_turn(session_key, params, timeout):
            captured.update(params)
            return Turn()

        self.service.analysis_bridge.open_turn = open_turn
        scan = ProjectScanner().scan(self.root)
        result = await self.service._generate_semantic(
            {
                "run_id": "profile-run",
                "storage_key": "profile-key",
                "canonical_cwd": str(self.root),
                "preferred_language": "zh",
                "model": "",
                "effort": "",
            },
            scan,
            asyncio.Event(),
        )
        self.service._internal_sessions.pop("profile-run", None)
        self.assertEqual({"nodes": [], "relations": []}, result)
        self.assertEqual("project-map", captured["runtimeProfile"])
        self.assertFalse(captured["browserEnabled"])
        self.assertEqual("json_schema", captured["outputFormat"]["type"])

    async def test_generation_pipeline_publishes_valid_snapshot(self):
        (self.root / "api.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/ready')\n"
            "def ready(): return {'ready': True}\n",
            encoding="utf-8",
        )
        _insert_session(self.db_path, "pipeline-session", self.root, "code")

        class Turn:
            async def events(self):
                yield {
                    "type": "event",
                    "event": {
                        "type": "result",
                        "structured_output": {"nodes": [], "relations": []},
                    },
                }

        class FakeBridge:
            async def open_turn(self, session_key, params, timeout):
                return Turn()

            async def interrupt(self, session_key):
                return None

            async def close_session(self, session_key):
                return None

            async def shutdown(self):
                return None

        self.service.analysis_bridge = FakeBridge()
        started = await self.service.start_run("pipeline-session")
        run_id = started["run"]["run_id"]
        await self.service._tasks[run_id]

        run = self.service.storage.run(run_id)
        snapshot = self.service.storage.latest_snapshot(started["storage_key"])
        self.assertEqual("completed", run["status"])
        self.assertEqual(1, snapshot["revision"])
        self.assertTrue(snapshot["dataset"]["nodes"])
        self.assertTrue(
            any(node["kind"] == "route" for node in snapshot["dataset"]["nodes"])
        )
        self.assertTrue(
            any(
                node["kind"] == "file"
                and any(source.get("path") == "api.py" for source in node["sources"])
                for node in snapshot["dataset"]["nodes"]
            )
        )

    async def test_semantic_output_with_forged_evidence_is_rejected(self):
        dataset = {
            "manifest": {},
            "profile": {},
            "files": [],
            "evidence": [{
                "id": "ev-known",
                "path": "known.py",
                "file_hash": "hash",
                "start_line": 1,
                "end_line": 1,
                "symbol_key": "function:known",
                "kind": "function",
                "label": "known",
                "excerpt": "def known(): pass",
                "snippet_hash": "snippet",
            }],
            "nodes": [],
            "relations": [],
        }
        semantic = {
            "nodes": [{
                "title": "伪造节点",
                "summary": "不应被接受",
                "roles": ["service"],
                "evidence_ids": ["ev-known", "ev-forged"],
            }],
            "relations": [],
        }
        with self.assertRaises(ValueError):
            self.service._merge_semantic(dataset, semantic, None)

        negative_relation = {
            "nodes": [
                {
                    "title": "A",
                    "summary": "A summary",
                    "roles": ["service"],
                    "evidence_ids": ["ev-known"],
                },
                {
                    "title": "B",
                    "summary": "B summary",
                    "roles": ["service"],
                    "evidence_ids": ["ev-known"],
                },
            ],
            "relations": [{
                "source_index": -1,
                "target_index": 0,
                "type": "CALLS",
                "label": "invalid",
                "evidence_ids": ["ev-known"],
            }],
        }
        with self.assertRaises(ValueError):
            self.service._merge_semantic(dataset, negative_relation, None)

    async def test_duplicate_semantic_anchors_reuse_old_ids_one_to_one(self):
        evidence = {
            "id": "ev-known",
            "path": "known.py",
            "file_hash": "hash",
            "start_line": 1,
            "end_line": 1,
            "symbol_key": "function:known",
            "kind": "function",
            "label": "known",
            "excerpt": "def known(): pass",
            "snippet_hash": "snippet",
        }
        dataset = {
            "manifest": {},
            "profile": {},
            "files": [],
            "evidence": [evidence],
            "nodes": [],
            "relations": [],
        }
        old_snapshot = {"dataset": {"nodes": [
            {
                "id": "semantic:old-z",
                "layer": "semantic",
                "title": "Alpha",
                "roles": ["service"],
                "evidence_ids": ["ev-known"],
            },
            {
                "id": "semantic:old-a",
                "layer": "semantic",
                "title": "Beta",
                "roles": ["service"],
                "evidence_ids": ["ev-known"],
            },
        ]}}
        semantic = {
            "nodes": [
                {
                    "title": "Alpha",
                    "summary": "Alpha summary",
                    "roles": ["service"],
                    "evidence_ids": ["ev-known"],
                },
                {
                    "title": "Beta",
                    "summary": "Beta summary",
                    "roles": ["service"],
                    "evidence_ids": ["ev-known"],
                },
            ],
            "relations": [],
        }
        self.service._merge_semantic(dataset, semantic, old_snapshot)
        ids_by_title = {node["title"]: node["id"] for node in dataset["nodes"]}
        self.assertEqual("semantic:old-z", ids_by_title["Alpha"])
        self.assertEqual("semantic:old-a", ids_by_title["Beta"])

    async def test_impact_reports_stale_snapshot_and_complete_reverse_path(self):
        target = self.root / "target.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        _insert_session(self.db_path, "impact-session", self.root, "code")
        _, _, key = self.service.resolve_code_project("impact-session")
        scan = ProjectScanner().scan(self.root)
        dataset = _empty_dataset(key, self.root)
        dataset["nodes"] = [
            {"id": "a", "kind": "service", "title": "A", "sources": []},
            {
                "id": "b", "kind": "test", "title": "B", "roles": ["test"],
                "sources": [{"path": "tests/test_target.py"}],
            },
            {
                "id": "c",
                "kind": "file",
                "title": "C",
                "sources": [{"path": "target.py"}],
            },
        ]
        dataset["relations"] = [
            {
                "id": "ab", "source_id": "a", "target_id": "b", "type": "IMPORTS",
                "provenance": "parser", "confidence": "high", "evidence_ids": [],
            },
            {
                "id": "bc", "source_id": "b", "target_id": "c", "type": "CALLS",
                "provenance": "parser", "confidence": "high", "evidence_ids": [],
            },
        ]
        self.service.storage.create_run(
            run_id="impact-run",
            owner_session_id="impact-session",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            model="",
            effort="",
            preferred_language="zh",
        )
        self.service.storage.publish_snapshot(
            run_id="impact-run",
            storage_key=key,
            canonical_cwd=str(self.root),
            base_revision=0,
            dataset=dataset,
            files=scan.files,
            source_root_hash=scan.source_root_hash,
            scanner_version="scanner-v1",
            prompt_version="prompt-v1",
        )
        target.write_text("VALUE = 2\n", encoding="utf-8")

        result = await self.service.impact("impact-session", ["target.py"])
        by_id = {item["node_id"]: item for item in result["impacts"]}
        self.assertTrue(result["stale"])
        self.assertEqual(2, by_id["a"]["distance"])
        self.assertEqual(2, len(by_id["a"]["path"]))
        self.assertEqual("ab", by_id["a"]["path"][0]["relation_id"])
        self.assertEqual("parser", by_id["a"]["path"][0]["provenance"])
        self.assertEqual("b", result["test_suggestions"][0]["node_id"])
        shallow = await self.service.impact(
            "impact-session", ["target.py"], max_depth=1
        )
        self.assertNotIn("a", {item["node_id"] for item in shallow["impacts"]})
        limited = await self.service.impact(
            "impact-session", ["target.py"], max_results=1
        )
        self.assertEqual(1, len(limited["impacts"]))
        self.assertTrue(limited["truncated"])
        with self.assertRaises(HTTPException) as mismatch:
            await self.service.impact(
                "impact-session", ["target.py"], expected_revision=2
            )
        self.assertEqual(409, mismatch.exception.status_code)

    async def test_freshness_partial_scan_does_not_report_deleted_files(self):
        target = self.root / "kept.py"
        target.write_text("VALUE = 1\n", encoding="utf-8")
        _insert_session(self.db_path, "fresh-session", self.root, "code")
        _, _, key = self.service.resolve_code_project("fresh-session")
        scan = ProjectScanner().scan(self.root)
        self.service.storage.create_run(
            run_id="fresh-run", owner_session_id="fresh-session", storage_key=key,
            canonical_cwd=str(self.root), base_revision=0, model="", effort="",
            preferred_language="zh",
        )
        self.service.storage.publish_snapshot(
            run_id="fresh-run", storage_key=key, canonical_cwd=str(self.root),
            base_revision=0, dataset=_empty_dataset(key, self.root), files=scan.files,
            source_root_hash=scan.source_root_hash, scanner_version="scanner-v1",
            prompt_version="prompt-v1",
        )
        self.service.scanner.scan = lambda root: ScanResult(
            root=root, files=[], partial=True, partial_reason="time_limit",
            source_root_hash="partial-hash",
        )
        freshness = await self.service.freshness("fresh-session")
        self.assertEqual([], freshness["changes"]["deleted"])
        self.assertEqual("kept.py", freshness["changes"]["unknown_missing"][0]["path"])
        self.assertEqual("partial", freshness["scan_completeness"])

    async def test_file_change_rename_is_only_inferred_for_unique_hashes(self):
        unique = self.service._file_changes(
            [{"path": "old.py", "hash": "same"}],
            [{"path": "new.py", "hash": "same"}],
            partial=False,
        )
        self.assertEqual(
            [{"from": "old.py", "to": "new.py", "hash": "same"}],
            unique["renamed"],
        )
        ambiguous = self.service._file_changes(
            [
                {"path": "old-a.py", "hash": "duplicate"},
                {"path": "old-b.py", "hash": "duplicate"},
            ],
            [
                {"path": "new-a.py", "hash": "duplicate"},
                {"path": "new-b.py", "hash": "duplicate"},
            ],
            partial=False,
        )
        self.assertEqual([], ambiguous["renamed"])
        self.assertEqual(2, ambiguous["counts"]["added"])
        self.assertEqual(2, ambiguous["counts"]["deleted"])

    async def test_revision_list_get_and_compare_are_project_scoped(self):
        _insert_session(self.db_path, "revision-session", self.root, "code")
        _, _, key = self.service.resolve_code_project("revision-session")
        first = _empty_dataset(key, self.root)
        first["nodes"] = [{"id": "file:a.py", "kind": "file", "title": "a.py"}]
        second = _empty_dataset(key, self.root)
        second["nodes"] = [
            {"id": "file:a.py", "kind": "file", "title": "A.py"},
            {"id": "file:b.py", "kind": "file", "title": "b.py"},
        ]
        for revision, dataset in ((0, first), (1, second)):
            run_id = f"revision-run-{revision}"
            self.service.storage.create_run(
                run_id=run_id, owner_session_id="revision-session", storage_key=key,
                canonical_cwd=str(self.root), base_revision=revision, model="", effort="",
                preferred_language="zh",
            )
            self.service.storage.publish_snapshot(
                run_id=run_id, storage_key=key, canonical_cwd=str(self.root),
                base_revision=revision, dataset=dataset, files=[],
                source_root_hash=f"hash-{revision}", scanner_version="scanner-v1",
                prompt_version="prompt-v1",
            )
        revisions = await self.service.list_revisions("revision-session")
        self.assertEqual([2, 1], [item["revision"] for item in revisions["items"]])
        loaded = await self.service.get_revision("revision-session", 1)
        self.assertEqual("a.py", loaded["dataset"]["nodes"][0]["title"])
        compared = await self.service.compare_revisions("revision-session", 1, 2)
        self.assertEqual(1, compared["nodes"]["counts"]["added"])
        self.assertEqual(1, compared["nodes"]["counts"]["modified"])

    async def test_context_pack_is_opaque_session_bound_and_revalidated(self):
        target = self.root / "context.py"
        target.write_text("def answer():\n    return 42\n", encoding="utf-8")
        _insert_session(self.db_path, "pack-owner", self.root, "code")
        _insert_session(self.db_path, "same-project-other-session", self.root, "code")
        _, _, key = self.service.resolve_code_project("pack-owner")
        scan = ProjectScanner().scan(self.root)
        file_item = next(item for item in scan.files if item["path"] == "context.py")
        evidence = next(item for item in scan.evidence if item.path == "context.py")
        dataset = _empty_dataset(key, self.root)
        dataset["files"] = scan.files
        dataset["evidence"] = [evidence.model_dump()]
        dataset["nodes"] = [{
            "id": "file:context.py", "kind": "file", "title": "context.py",
            "summary": "source", "roles": ["source"], "confidence": "high",
            "evidence_ids": [evidence.id],
            "sources": [{
                "path": "context.py", "line_start": 1, "line_end": 2,
                "symbol_key": "function:answer", "file_hash": file_item["hash"],
            }],
        }]
        self.service.storage.create_run(
            run_id="pack-run", owner_session_id="pack-owner", storage_key=key,
            canonical_cwd=str(self.root), base_revision=0, model="", effort="",
            preferred_language="zh",
        )
        self.service.storage.publish_snapshot(
            run_id="pack-run", storage_key=key, canonical_cwd=str(self.root),
            base_revision=0, dataset=dataset, files=scan.files,
            source_root_hash=scan.source_root_hash, scanner_version="scanner-v1",
            prompt_version="prompt-v1",
        )
        created = await self.service.create_context_pack(
            "pack-owner", ["file:context.py"], expected_revision=1
        )
        self.assertNotIn("context.py", created["pack_id"])
        self.assertTrue(created["context"]["snippets"])
        resolved = await self.service.resolve_context_pack("pack-owner", created["pack_id"])
        self.assertEqual(1, resolved["revision"])
        with self.assertRaises(HTTPException) as cross_session:
            await self.service.resolve_context_pack(
                "same-project-other-session", created["pack_id"]
            )
        self.assertEqual(403, cross_session.exception.status_code)
        target.write_text("def answer():\n    return 43\n", encoding="utf-8")
        with self.assertRaises(HTTPException) as stale:
            await self.service.resolve_context_pack("pack-owner", created["pack_id"])
        self.assertEqual(409, stale.exception.status_code)


class ProjectMapFrontendBoundaryTest(unittest.TestCase):
    def test_project_map_runtime_keeps_user_auth_but_excludes_project_settings(self):
        source = (
            Path(__file__).parents[1]
            / "claude_web"
            / "agent_bridge"
            / "daemon.mjs"
        ).read_text(encoding="utf-8")
        start = source.index("if (params.runtimeProfile === 'project-map')")
        end = source.index("\n  const options = {", start)
        profile = source[start:end]
        self.assertIn("settingSources: ['user']", profile)
        self.assertIn("includePartialMessages: true", profile)
        self.assertNotIn("'project'", profile)
        self.assertNotIn("'local'", profile)
        self.assertIn("tools: []", profile)
        self.assertIn("strictMcpConfig: true", profile)
        self.assertIn("mcpServers: {}", profile)

    def test_project_map_assets_are_lazy_and_code_only(self):
        index = (
            Path(__file__).parents[1] / "claude_web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="cwProjectMapBtn" class="sb-icon-btn cw-code-only"', index)
        self.assertNotIn('<script src="/assets/project-map.js"', index)
        self.assertNotIn('<link rel="stylesheet" href="/assets/project-map.css"', index)
        self.assertIn("if (!codeMode) return;", index)
        self.assertIn("window.CWProjectMap?.close({ silent:true });", index)


if __name__ == "__main__":
    unittest.main()
