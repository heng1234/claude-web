from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence


ENTRY_TYPES = frozenset({
    "project_map_pack",
    "sdk_context_usage",
    "native_compact",
    "user_pinned",
    "auto_retrieval",
})
LIFECYCLE_STATES = frozenset({"active", "compacted", "dropped"})

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CONTENT_KEYS = frozenset({
    "body",
    "code",
    "content",
    "contents",
    "excerpt",
    "file_content",
    "file_contents",
    "full_text",
    "prompt",
    "system_prompt",
    "raw",
    "raw_content",
    "snippet",
    "source_code",
    "text",
    "user_prompt",
})


class CodeContextLedgerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CodeContextLedger:
    """Persistent descriptor ledger for one exact Code session and workspace.

    The ledger intentionally stores references, provenance and token estimates,
    not source excerpts or application-generated summaries.  Session mode and
    cwd are always resolved from the server-owned ``sessions`` table.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_entries_per_session: int = 500,
        max_descriptor_bytes: int = 8 * 1024,
        max_total_descriptor_bytes: int = 512 * 1024,
        max_token_estimate: int = 2_000_000,
        retention_seconds: float = 30 * 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        self.max_entries_per_session = max(1, int(max_entries_per_session))
        self.max_descriptor_bytes = max(256, int(max_descriptor_bytes))
        self.max_total_descriptor_bytes = max(
            self.max_descriptor_bytes,
            int(max_total_descriptor_bytes),
        )
        self.max_token_estimate = max(0, int(max_token_estimate))
        self.retention_seconds = max(0.0, float(retention_seconds))
        self.clock = clock

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
                CREATE TABLE IF NOT EXISTS code_context_ledger (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    canonical_cwd TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    version TEXT NOT NULL,
                    descriptor_json TEXT NOT NULL,
                    descriptor_bytes INTEGER NOT NULL,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    stale INTEGER NOT NULL DEFAULT 0,
                    revision TEXT NOT NULL DEFAULT '',
                    lifecycle_state TEXT NOT NULL DEFAULT 'active',
                    compact_category TEXT NOT NULL DEFAULT '',
                    compact_event_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_code_context_ledger_session "
                "ON code_context_ledger (session_id, canonical_cwd, created_at DESC, id DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_code_context_ledger_lifecycle "
                "ON code_context_ledger (session_id, lifecycle_state, entry_type)"
            )

    def record_descriptor(
        self,
        session_id: str,
        *,
        entry_type: str,
        source: str,
        descriptor: Mapping[str, Any],
        version: str = "1",
        token_estimate: int = 0,
        stale: bool = False,
        revision: object = "",
        expected_cwd: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        canonical_cwd = self._code_session_cwd(session_id)
        if expected_cwd is not None and self._canonical_cwd(expected_cwd) != canonical_cwd:
            raise CodeContextLedgerError(
                "workspace_mismatch",
                "上下文描述符不属于当前 Code 会话绑定的项目目录",
            )
        normalized_type = self._entry_type(entry_type)
        normalized_source = self._name(source, "source")
        normalized_version = self._name(version, "version")
        normalized_revision = self._revision(revision)
        normalized_tokens = self._token_estimate(token_estimate)
        descriptor_json, descriptor_bytes = self._descriptor_json(descriptor)
        now = self._timestamp(created_at)
        entry_id = uuid.uuid4().hex
        with self.connect(immediate=True) as conn:
            self._recheck_code_session(conn, session_id, canonical_cwd)
            conn.execute(
                """
                INSERT INTO code_context_ledger (
                    id, session_id, canonical_cwd, entry_type, source, version,
                    descriptor_json, descriptor_bytes, token_estimate, stale,
                    revision, lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    entry_id,
                    session_id,
                    canonical_cwd,
                    normalized_type,
                    normalized_source,
                    normalized_version,
                    descriptor_json,
                    descriptor_bytes,
                    normalized_tokens,
                    1 if stale else 0,
                    normalized_revision,
                    now,
                    now,
                ),
            )
            self._prune_in_transaction(conn, session_id, canonical_cwd, now)
            row = conn.execute(
                "SELECT * FROM code_context_ledger WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise CodeContextLedgerError("budget_exhausted", "上下文账本预算不足")
        return self._payload(row)

    def record_project_map_pack(
        self,
        session_id: str,
        *,
        pack_id: str,
        revision: object,
        descriptor: Optional[Mapping[str, Any]] = None,
        token_estimate: int = 0,
        stale: bool = False,
        version: str = "1",
        expected_cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        value = dict(descriptor or {})
        value["pack_id"] = self._name(pack_id, "pack_id")
        return self.record_descriptor(
            session_id,
            entry_type="project_map_pack",
            source="project-map",
            descriptor=value,
            version=version,
            token_estimate=token_estimate,
            stale=stale,
            revision=revision,
            expected_cwd=expected_cwd,
        )

    def record_sdk_context_usage(
        self,
        session_id: str,
        *,
        descriptor: Mapping[str, Any],
        token_estimate: int,
        revision: object = "",
        version: str = "1",
        expected_cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.record_descriptor(
            session_id,
            entry_type="sdk_context_usage",
            source="claude-agent-sdk",
            descriptor=descriptor,
            version=version,
            token_estimate=token_estimate,
            revision=revision,
            expected_cwd=expected_cwd,
        )

    def record_user_pinned(
        self,
        session_id: str,
        *,
        descriptor: Mapping[str, Any],
        token_estimate: int = 0,
        revision: object = "",
        version: str = "1",
        expected_cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.record_descriptor(
            session_id,
            entry_type="user_pinned",
            source="user",
            descriptor=descriptor,
            version=version,
            token_estimate=token_estimate,
            revision=revision,
            expected_cwd=expected_cwd,
        )

    def record_auto_retrieval(
        self,
        session_id: str,
        *,
        descriptor: Mapping[str, Any],
        token_estimate: int = 0,
        revision: object = "",
        version: str = "1",
        expected_cwd: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.record_descriptor(
            session_id,
            entry_type="auto_retrieval",
            source="retrieval",
            descriptor=descriptor,
            version=version,
            token_estimate=token_estimate,
            revision=revision,
            expected_cwd=expected_cwd,
        )

    def record_native_compact(
        self,
        session_id: str,
        *,
        descriptor: Mapping[str, Any],
        token_estimate: int = 0,
        revision: object = "",
        version: str = "1",
        expected_cwd: Optional[str] = None,
        compact_entry_types: Sequence[str] = (
            "project_map_pack",
            "sdk_context_usage",
            "native_compact",
        ),
        dropped_entry_types: Sequence[str] = ("auto_retrieval",),
    ) -> Dict[str, Any]:
        canonical_cwd = self._code_session_cwd(session_id)
        if expected_cwd is not None and self._canonical_cwd(expected_cwd) != canonical_cwd:
            raise CodeContextLedgerError("workspace_mismatch", "原生 compact 不属于当前 Code 项目")
        compact_types = {self._entry_type(value) for value in compact_entry_types}
        dropped_types = {self._entry_type(value) for value in dropped_entry_types}
        if compact_types & dropped_types:
            raise CodeContextLedgerError("invalid_compact_categories", "compact 与 dropped 类型不能重叠")
        normalized_revision = self._revision(revision)
        normalized_tokens = self._token_estimate(token_estimate)
        descriptor_json, descriptor_bytes = self._descriptor_json(descriptor)
        now = self._timestamp(None)
        entry_id = uuid.uuid4().hex
        with self.connect(immediate=True) as conn:
            self._recheck_code_session(conn, session_id, canonical_cwd)
            conn.execute(
                """
                INSERT INTO code_context_ledger (
                    id, session_id, canonical_cwd, entry_type, source, version,
                    descriptor_json, descriptor_bytes, token_estimate, stale,
                    revision, lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, 'native_compact', 'claude-agent-sdk', ?, ?, ?, ?, 0, ?, 'active', ?, ?)
                """,
                (
                    entry_id,
                    session_id,
                    canonical_cwd,
                    self._name(version, "version"),
                    descriptor_json,
                    descriptor_bytes,
                    normalized_tokens,
                    normalized_revision,
                    now,
                    now,
                ),
            )
            self._mark_lifecycle_in_transaction(
                conn,
                session_id=session_id,
                canonical_cwd=canonical_cwd,
                excluded_id=entry_id,
                compact_event_id=entry_id,
                compact_types=compact_types,
                dropped_types=dropped_types,
                now=now,
            )
            self._prune_in_transaction(conn, session_id, canonical_cwd, now)
            row = conn.execute(
                "SELECT * FROM code_context_ledger WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise CodeContextLedgerError("budget_exhausted", "上下文账本预算不足")
        return self._payload(row)

    def get(self, session_id: str, entry_id: str) -> Dict[str, Any]:
        canonical_cwd = self._code_session_cwd(session_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM code_context_ledger WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise CodeContextLedgerError("entry_not_found", "上下文账本条目不存在")
        if row["session_id"] != session_id:
            raise CodeContextLedgerError("ledger_forbidden", "条目不属于当前 Code 会话")
        if row["canonical_cwd"] != canonical_cwd:
            raise CodeContextLedgerError("workspace_mismatch", "条目不属于当前 Code 项目")
        return self._payload(row)

    def list(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        lifecycle_state: Optional[str] = None,
        entry_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        canonical_cwd = self._code_session_cwd(session_id)
        bounded_limit = max(1, min(200, int(limit)))
        bounded_offset = max(0, int(offset))
        clauses = ["session_id = ?", "canonical_cwd = ?"]
        params: List[object] = [session_id, canonical_cwd]
        if lifecycle_state is not None:
            if lifecycle_state not in LIFECYCLE_STATES:
                raise CodeContextLedgerError("invalid_lifecycle_state", "上下文生命周期状态无效")
            clauses.append("lifecycle_state = ?")
            params.append(lifecycle_state)
        if entry_type is not None:
            clauses.append("entry_type = ?")
            params.append(self._entry_type(entry_type))
        where = " AND ".join(clauses)
        with self.connect(immediate=True) as conn:
            self._recheck_code_session(conn, session_id, canonical_cwd)
            self._prune_in_transaction(conn, session_id, canonical_cwd, self.clock())
            total = int(conn.execute(
                f"SELECT COUNT(*) AS value FROM code_context_ledger WHERE {where}",
                tuple(params),
            ).fetchone()["value"])
            rows = conn.execute(
                f"SELECT * FROM code_context_ledger WHERE {where} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
        return {
            "session_id": session_id,
            "canonical_cwd": canonical_cwd,
            "total": total,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "items": [self._payload(row) for row in rows],
        }

    def summary(self, session_id: str) -> Dict[str, Any]:
        canonical_cwd = self._code_session_cwd(session_id)
        with self.connect(immediate=True) as conn:
            self._recheck_code_session(conn, session_id, canonical_cwd)
            self._prune_in_transaction(conn, session_id, canonical_cwd, self.clock())
            rows = conn.execute(
                """
                SELECT entry_type, lifecycle_state, COUNT(*) AS count,
                       COALESCE(SUM(descriptor_bytes), 0) AS descriptor_bytes,
                       COALESCE(SUM(token_estimate), 0) AS tokens,
                       COALESCE(SUM(stale), 0) AS stale_count
                FROM code_context_ledger
                WHERE session_id = ? AND canonical_cwd = ?
                GROUP BY entry_type, lifecycle_state
                """,
                (session_id, canonical_cwd),
            ).fetchall()
            latest_usage = conn.execute(
                """
                SELECT * FROM code_context_ledger
                WHERE session_id = ? AND canonical_cwd = ?
                  AND entry_type = 'sdk_context_usage'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (session_id, canonical_cwd),
            ).fetchone()
            latest_compact = conn.execute(
                """
                SELECT * FROM code_context_ledger
                WHERE session_id = ? AND canonical_cwd = ?
                  AND entry_type = 'native_compact'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (session_id, canonical_cwd),
            ).fetchone()
        by_type: Dict[str, Dict[str, int]] = {}
        by_lifecycle = {state: 0 for state in sorted(LIFECYCLE_STATES)}
        total_count = 0
        total_bytes = 0
        total_tokens = 0
        active_tokens = 0
        stale_count = 0
        for row in rows:
            count = int(row["count"])
            tokens = int(row["tokens"])
            state = str(row["lifecycle_state"])
            by_type.setdefault(str(row["entry_type"]), {})[state] = count
            by_lifecycle[state] += count
            total_count += count
            total_bytes += int(row["descriptor_bytes"])
            total_tokens += tokens
            stale_count += int(row["stale_count"])
            if state == "active":
                active_tokens += tokens
        return {
            "session_id": session_id,
            "canonical_cwd": canonical_cwd,
            "count": total_count,
            "descriptor_bytes": total_bytes,
            "token_estimate": total_tokens,
            "active_token_estimate": active_tokens,
            "stale_count": stale_count,
            "by_type": by_type,
            "by_lifecycle": by_lifecycle,
            "limits": {
                "max_entries": self.max_entries_per_session,
                "max_descriptor_bytes": self.max_descriptor_bytes,
                "max_total_descriptor_bytes": self.max_total_descriptor_bytes,
                "max_token_estimate": self.max_token_estimate,
                "retention_seconds": self.retention_seconds,
            },
            "latest_sdk_context_usage": self._payload(latest_usage) if latest_usage else None,
            "latest_native_compact": self._payload(latest_compact) if latest_compact else None,
        }

    def mark_stale(self, session_id: str, entry_ids: Sequence[str]) -> int:
        canonical_cwd = self._code_session_cwd(session_id)
        unique_ids = list(dict.fromkeys(str(value) for value in entry_ids if str(value)))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect(immediate=True) as conn:
            self._recheck_code_session(conn, session_id, canonical_cwd)
            rows = conn.execute(
                f"SELECT id, session_id, canonical_cwd FROM code_context_ledger WHERE id IN ({placeholders})",
                tuple(unique_ids),
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise CodeContextLedgerError("entry_not_found", "部分上下文账本条目不存在")
            if any(row["session_id"] != session_id for row in rows):
                raise CodeContextLedgerError("ledger_forbidden", "条目不属于当前 Code 会话")
            if any(row["canonical_cwd"] != canonical_cwd for row in rows):
                raise CodeContextLedgerError("workspace_mismatch", "条目不属于当前 Code 项目")
            conn.execute(
                f"UPDATE code_context_ledger SET stale = 1, updated_at = ? WHERE id IN ({placeholders})",
                (self.clock(), *unique_ids),
            )
        return len(unique_ids)

    def _prune_in_transaction(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        canonical_cwd: str,
        now: float,
    ) -> None:
        cutoff = float(now) - self.retention_seconds
        conn.execute(
            "DELETE FROM code_context_ledger WHERE session_id = ? AND canonical_cwd = ? AND created_at < ?",
            (session_id, canonical_cwd, cutoff),
        )
        rows = conn.execute(
            """
            SELECT id, descriptor_bytes FROM code_context_ledger
            WHERE session_id = ? AND canonical_cwd = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (session_id, canonical_cwd),
        ).fetchall()
        kept_count = 0
        kept_bytes = 0
        delete_ids: List[str] = []
        for row in rows:
            size = int(row["descriptor_bytes"])
            if (
                kept_count >= self.max_entries_per_session
                or kept_bytes + size > self.max_total_descriptor_bytes
            ):
                delete_ids.append(str(row["id"]))
                continue
            kept_count += 1
            kept_bytes += size
        if delete_ids:
            placeholders = ",".join("?" for _ in delete_ids)
            conn.execute(
                f"DELETE FROM code_context_ledger WHERE id IN ({placeholders})",
                tuple(delete_ids),
            )

    @staticmethod
    def _mark_lifecycle_in_transaction(
        conn: sqlite3.Connection,
        *,
        session_id: str,
        canonical_cwd: str,
        excluded_id: str,
        compact_event_id: str,
        compact_types: set[str],
        dropped_types: set[str],
        now: float,
    ) -> None:
        for state, types in (("compacted", compact_types), ("dropped", dropped_types)):
            if not types:
                continue
            placeholders = ",".join("?" for _ in types)
            conn.execute(
                f"""
                UPDATE code_context_ledger
                SET lifecycle_state = ?, compact_category = entry_type,
                    compact_event_id = ?, updated_at = ?
                WHERE session_id = ? AND canonical_cwd = ? AND id != ?
                  AND lifecycle_state = 'active' AND entry_type IN ({placeholders})
                """,
                (
                    state,
                    compact_event_id,
                    now,
                    session_id,
                    canonical_cwd,
                    excluded_id,
                    *sorted(types),
                ),
            )

    def _code_session_cwd(self, session_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, cwd, workspace_mode FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise CodeContextLedgerError("session_not_found", "Code 会话不存在")
        if str(row["workspace_mode"] or "chat") != "code":
            raise CodeContextLedgerError("code_session_required", "上下文账本仅支持 Code 会话")
        cwd = str(row["cwd"] or "").strip()
        if not cwd:
            raise CodeContextLedgerError("project_required", "Code 会话尚未绑定项目目录")
        return self._canonical_cwd(cwd)

    def _recheck_code_session(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        canonical_cwd: str,
    ) -> None:
        row = conn.execute(
            "SELECT cwd, workspace_mode FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise CodeContextLedgerError("session_not_found", "Code 会话不存在")
        if str(row["workspace_mode"] or "chat") != "code":
            raise CodeContextLedgerError("code_session_required", "上下文账本仅支持 Code 会话")
        if self._canonical_cwd(str(row["cwd"] or "")) != canonical_cwd:
            raise CodeContextLedgerError("workspace_changed", "Code 会话项目目录已变更，请重试")

    @staticmethod
    def _canonical_cwd(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return str(Path(raw).expanduser().resolve(strict=False))

    @staticmethod
    def _entry_type(value: object) -> str:
        normalized = str(value or "").strip()
        if normalized not in ENTRY_TYPES:
            raise CodeContextLedgerError("invalid_entry_type", "上下文账本条目类型无效")
        return normalized

    @staticmethod
    def _name(value: object, field: str) -> str:
        normalized = str(value or "").strip()
        if not _NAME_RE.fullmatch(normalized):
            raise CodeContextLedgerError(f"invalid_{field}", f"{field} 格式无效")
        return normalized

    @staticmethod
    def _revision(value: object) -> str:
        if value is None:
            return ""
        normalized = str(value).strip()
        if len(normalized.encode("utf-8")) > 128:
            raise CodeContextLedgerError("invalid_revision", "revision 过长")
        return normalized

    def _token_estimate(self, value: object) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise CodeContextLedgerError("invalid_token_estimate", "token estimate 必须是整数") from exc
        if number < 0 or number > self.max_token_estimate:
            raise CodeContextLedgerError("token_budget_exceeded", "token estimate 超出上下文账本预算")
        return number

    def _timestamp(self, value: Optional[float]) -> float:
        number = self.clock() if value is None else float(value)
        if not math.isfinite(number) or number < 0:
            raise CodeContextLedgerError("invalid_created_at", "created_at 无效")
        return number

    def _descriptor_json(self, descriptor: Mapping[str, Any]) -> tuple[str, int]:
        if not isinstance(descriptor, Mapping):
            raise CodeContextLedgerError("invalid_descriptor", "descriptor 必须是对象")
        normalized = self._normalize_descriptor(dict(descriptor), depth=0)
        payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload_bytes = len(payload.encode("utf-8"))
        if payload_bytes > self.max_descriptor_bytes:
            raise CodeContextLedgerError("descriptor_too_large", "descriptor 超出上下文账本大小限制")
        return payload, payload_bytes

    def _normalize_descriptor(self, value: Any, *, depth: int) -> Any:
        if depth > 8:
            raise CodeContextLedgerError("descriptor_too_deep", "descriptor 嵌套过深")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise CodeContextLedgerError("invalid_descriptor", "descriptor 包含无效数字")
            return value
        if isinstance(value, str):
            if len(value.encode("utf-8")) > 2048:
                raise CodeContextLedgerError("descriptor_value_too_large", "descriptor 字段过长")
            return value
        if isinstance(value, Mapping):
            if len(value) > 100:
                raise CodeContextLedgerError("descriptor_too_large", "descriptor 字段过多")
            result: Dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key)
                if normalized_key.lower() in _CONTENT_KEYS:
                    raise CodeContextLedgerError(
                        "raw_content_forbidden",
                        "上下文账本只保存描述符，禁止保存源码、Prompt 或长文本正文",
                    )
                if not normalized_key or len(normalized_key.encode("utf-8")) > 128:
                    raise CodeContextLedgerError("invalid_descriptor_key", "descriptor 字段名无效")
                result[normalized_key] = self._normalize_descriptor(item, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            if len(value) > 200:
                raise CodeContextLedgerError("descriptor_too_large", "descriptor 列表过长")
            return [self._normalize_descriptor(item, depth=depth + 1) for item in value]
        raise CodeContextLedgerError("invalid_descriptor", "descriptor 包含不支持的数据类型")

    @staticmethod
    def _payload(row: sqlite3.Row) -> Dict[str, Any]:
        revision = str(row["revision"] or "")
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "canonical_cwd": row["canonical_cwd"],
            "type": row["entry_type"],
            "source": row["source"],
            "version": row["version"],
            "descriptor": json.loads(row["descriptor_json"]),
            "descriptor_bytes": int(row["descriptor_bytes"]),
            "token_estimate": int(row["token_estimate"]),
            "stale": bool(row["stale"]),
            "revision": revision,
            "lifecycle_state": row["lifecycle_state"],
            "compact_category": row["compact_category"],
            "compact_event_id": row["compact_event_id"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }
