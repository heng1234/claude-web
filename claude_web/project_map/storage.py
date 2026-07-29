from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


TERMINAL_STATUSES = {
    "completed", "failed", "cancelled", "interrupted", "superseded",
}
ACTIVE_STATUSES = {
    "queued", "scanning", "extracting", "generating", "validating", "persisting",
}


class ProjectMapPublishCancelled(Exception):
    pass


class ProjectMapStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_map_workspaces (
                    storage_key TEXT PRIMARY KEY,
                    canonical_cwd TEXT NOT NULL,
                    active_revision INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_map_snapshots (
                    storage_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    dataset_json TEXT NOT NULL,
                    source_root_hash TEXT NOT NULL DEFAULT '',
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    scanner_version TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (storage_key, revision)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_map_files (
                    storage_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    language TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    parse_quality TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (storage_key, revision, path)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_map_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_session_id TEXT NOT NULL,
                    storage_key TEXT NOT NULL,
                    canonical_cwd TEXT NOT NULL,
                    base_revision INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL DEFAULT '',
                    effort TEXT NOT NULL DEFAULT '',
                    preferred_language TEXT NOT NULL DEFAULT 'zh',
                    error_category TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_map_run_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_map_runs_workspace "
                "ON project_map_runs (storage_key, status, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_map_events_run "
                "ON project_map_run_events (run_id, seq)"
            )
            now = time.time()
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            interrupted = conn.execute(
                f"""
                SELECT run_id FROM project_map_runs
                WHERE status IN ({placeholders})
                """,
                tuple(sorted(ACTIVE_STATUSES)),
            ).fetchall()
            conn.execute(
                f"""
                UPDATE project_map_runs
                SET status = 'interrupted', phase = 'interrupted',
                    error_category = 'service_restarted',
                    error_message = '服务重启，生成任务未自动重放',
                    updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (now, *sorted(ACTIVE_STATUSES)),
            )
            for row in interrupted:
                self._append_event_in_transaction(conn, row["run_id"], {
                    "type": "status",
                    "status": "interrupted",
                    "phase": "interrupted",
                    "progress": 100,
                    "message": "服务重启，项目地图生成已中断",
                    "error_category": "service_restarted",
                    "error_message": "服务重启，生成任务未自动重放",
                }, now)

    @staticmethod
    def _append_event_in_transaction(
        conn: sqlite3.Connection,
        run_id: str,
        event: Dict[str, Any],
        now: Optional[float] = None,
    ) -> int:
        created_at = time.time() if now is None else now
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS value FROM project_map_run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["value"]) + 1
        payload = {**event, "seq": seq, "ts": created_at}
        conn.execute(
            "INSERT INTO project_map_run_events (run_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, seq, json.dumps(payload, ensure_ascii=False), created_at),
        )
        return seq

    def session_row(self, session_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id, cwd, workspace_mode FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

    def active_revision(self, storage_key: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT active_revision FROM project_map_workspaces WHERE storage_key = ?",
                (storage_key,),
            ).fetchone()
        return int(row["active_revision"]) if row else 0

    def latest_snapshot(self, storage_key: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.revision, s.run_id, s.dataset_json, s.source_root_hash, s.created_at
                FROM project_map_workspaces w
                JOIN project_map_snapshots s
                  ON s.storage_key = w.storage_key AND s.revision = w.active_revision
                WHERE w.storage_key = ?
                """,
                (storage_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "revision": int(row["revision"]),
            "run_id": row["run_id"],
            "dataset": json.loads(row["dataset_json"]),
            "source_root_hash": row["source_root_hash"],
            "created_at": float(row["created_at"]),
        }

    def create_run(
        self,
        *,
        run_id: str,
        owner_session_id: str,
        storage_key: str,
        canonical_cwd: str,
        base_revision: int,
        model: str,
        effort: str,
        preferred_language: str,
    ) -> None:
        now = time.time()
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO project_map_runs (
                    run_id, owner_session_id, storage_key, canonical_cwd,
                    base_revision, status, phase, progress, model, effort,
                    preferred_language, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, owner_session_id, storage_key, canonical_cwd,
                    base_revision, model, effort, preferred_language, now, now,
                ),
            )
            self._append_event_in_transaction(conn, run_id, {
                "type": "status",
                "status": "queued",
                "phase": "queued",
                "progress": 0,
                "message": "已加入项目地图生成队列",
            }, now)

    def create_run_if_idle(
        self,
        *,
        run_id: str,
        owner_session_id: str,
        storage_key: str,
        canonical_cwd: str,
        base_revision: int,
        model: str,
        effort: str,
        preferred_language: str,
    ) -> Optional[Dict[str, Any]]:
        """Atomically create a run, or return the workspace's active run."""
        now = time.time()
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                f"""
                SELECT * FROM project_map_runs
                WHERE storage_key = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (storage_key, *sorted(ACTIVE_STATUSES)),
            ).fetchone()
            if row is not None:
                return dict(row)
            conn.execute(
                """
                INSERT INTO project_map_runs (
                    run_id, owner_session_id, storage_key, canonical_cwd,
                    base_revision, status, phase, progress, model, effort,
                    preferred_language, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, owner_session_id, storage_key, canonical_cwd,
                    base_revision, model, effort, preferred_language, now, now,
                ),
            )
            self._append_event_in_transaction(conn, run_id, {
                "type": "status",
                "status": "queued",
                "phase": "queued",
                "progress": 0,
                "message": "已加入项目地图生成队列",
            }, now)
        return None

    def active_run(self, storage_key: str) -> Optional[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM project_map_runs
                WHERE storage_key = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (storage_key, *sorted(ACTIVE_STATUSES)),
            ).fetchone()
        return dict(row) if row else None

    def has_active_runs(self) -> bool:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        try:
            with self.connect() as conn:
                row = conn.execute(
                    f"SELECT 1 FROM project_map_runs WHERE status IN ({placeholders}) LIMIT 1",
                    tuple(sorted(ACTIVE_STATUSES)),
                ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_map_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        progress: int,
        message: str,
        error_category: str = "",
        error_message: str = "",
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE project_map_runs
                SET status = ?, phase = ?, progress = ?,
                    error_category = ?, error_message = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status, status, max(0, min(100, int(progress))),
                    error_category, error_message, now, run_id,
                ),
            )
            self._append_event_in_transaction(conn, run_id, {
                "type": "status",
                "status": status,
                "phase": status,
                "progress": max(0, min(100, int(progress))),
                "message": message,
                **({"error_category": error_category} if error_category else {}),
                **({"error_message": error_message} if error_message else {}),
            }, now)

    def request_cancel(self, run_id: str) -> bool:
        now = time.time()
        with self.connect(immediate=True) as conn:
            row = conn.execute(
                "SELECT status, cancel_requested FROM project_map_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] in TERMINAL_STATUSES or row["cancel_requested"]:
                return True
            conn.execute(
                "UPDATE project_map_runs SET cancel_requested = 1, updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            self._append_event_in_transaction(conn, run_id, {
                "type": "cancel_requested",
                "message": "正在取消项目地图生成",
            }, now)
        return True

    def cancel_requested(self, run_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM project_map_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def append_event(self, run_id: str, event: Dict[str, Any]) -> int:
        now = time.time()
        with self.connect(immediate=True) as conn:
            return self._append_event_in_transaction(conn, run_id, event, now)

    def events_after(self, run_id: str, seq: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_json FROM project_map_run_events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (run_id, max(0, int(seq))),
            ).fetchall()
        return [json.loads(row["event_json"]) for row in rows]

    def publish_snapshot(
        self,
        *,
        run_id: str,
        storage_key: str,
        canonical_cwd: str,
        base_revision: int,
        dataset: Dict[str, Any],
        files: List[Dict[str, Any]],
        source_root_hash: str,
        scanner_version: str,
        prompt_version: str,
    ) -> Optional[int]:
        now = time.time()
        with self.connect(immediate=True) as conn:
            run_row = conn.execute(
                """
                SELECT r.status, r.cancel_requested, r.storage_key, r.canonical_cwd,
                       s.workspace_mode, s.cwd AS session_cwd
                FROM project_map_runs r
                LEFT JOIN sessions s ON s.id = r.owner_session_id
                WHERE r.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run_row is None or run_row["storage_key"] != storage_key:
                return None
            if bool(run_row["cancel_requested"]):
                raise ProjectMapPublishCancelled()
            if run_row["status"] not in ACTIVE_STATUSES or run_row["workspace_mode"] != "code":
                return None
            try:
                session_cwd = Path(str(run_row["session_cwd"] or "")).expanduser().resolve()
            except OSError:
                return None
            if (
                str(session_cwd) != canonical_cwd
                or run_row["canonical_cwd"] != canonical_cwd
            ):
                return None
            row = conn.execute(
                "SELECT active_revision FROM project_map_workspaces WHERE storage_key = ?",
                (storage_key,),
            ).fetchone()
            current_revision = int(row["active_revision"]) if row else 0
            if current_revision != int(base_revision):
                return None
            revision = current_revision + 1
            manifest = dataset.setdefault("manifest", {})
            manifest.update({
                "revision": revision,
                "storage_key": storage_key,
                "workspace_path": canonical_cwd,
                "source_root_hash": source_root_hash,
                "updated_at": now,
                "last_run_id": run_id,
            })
            conn.execute(
                """
                INSERT INTO project_map_snapshots (
                    storage_key, revision, run_id, dataset_json, source_root_hash,
                    schema_version, scanner_version, prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    storage_key, revision, run_id,
                    json.dumps(dataset, ensure_ascii=False),
                    source_root_hash, scanner_version, prompt_version, now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO project_map_files (
                    storage_key, revision, path, hash, size, language, role, parse_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        storage_key, revision, item["path"], item["hash"],
                        int(item.get("size") or 0), item.get("language") or "",
                        item.get("role") or "", item.get("parse_quality") or "",
                    )
                    for item in files
                ],
            )
            conn.execute(
                """
                INSERT INTO project_map_workspaces (
                    storage_key, canonical_cwd, active_revision, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(storage_key) DO UPDATE SET
                    canonical_cwd = excluded.canonical_cwd,
                    active_revision = excluded.active_revision,
                    updated_at = excluded.updated_at
                """,
                (storage_key, canonical_cwd, revision, now),
            )
            conn.execute(
                """
                UPDATE project_map_runs
                SET status = 'completed', phase = 'completed', progress = 100, updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS value FROM project_map_run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row["value"]) + 1
            event = {
                "type": "status",
                "status": "completed",
                "phase": "completed",
                "progress": 100,
                "message": "项目地图已更新",
                "revision": revision,
                "seq": seq,
                "ts": now,
            }
            conn.execute(
                "INSERT INTO project_map_run_events (run_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
                (run_id, seq, json.dumps(event, ensure_ascii=False), now),
            )
        return revision
