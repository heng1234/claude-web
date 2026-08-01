from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence


_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACTIVE_QUEUE_STATES = {"queued", "paused", "dispatching"}
_ACTIVE_TURN_STATES = {"starting"}


class CodeWorktreeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CodeWorktreeManager:
    """Server-owned registry and safe git worktree operations for Code sessions."""

    def __init__(
        self,
        db_path: Path,
        *,
        activity_checker: Optional[Callable[[str], bool]] = None,
        git_timeout_seconds: float = 20.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.activity_checker = activity_checker
        self.git_timeout_seconds = max(1.0, float(git_timeout_seconds))

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
                CREATE TABLE IF NOT EXISTS code_worktrees (
                    id TEXT PRIMARY KEY,
                    source_session_id TEXT NOT NULL,
                    worktree_session_id TEXT NOT NULL,
                    repo_root TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    base_ref TEXT NOT NULL,
                    base_head TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'creating',
                    runtime_active INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(repo_root, slug),
                    UNIQUE(worktree_path),
                    UNIQUE(worktree_session_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_code_worktrees_source "
                "ON code_worktrees (source_session_id, status, updated_at)"
            )

    def create(
        self,
        source_session_id: str,
        *,
        slug: str,
        branch: str = "",
        base_ref: str = "HEAD",
    ) -> Dict[str, object]:
        source = self._code_session(source_session_id)
        repo_root = self._main_repo_root(
            Path(os.path.expanduser(str(source["cwd"]))).resolve()
        )
        normalized_slug = self._validate_slug(slug)
        normalized_branch = branch.strip() or f"claude-web/{normalized_slug}"
        self._validate_branch(repo_root, normalized_branch)
        normalized_base = str(base_ref or "HEAD").strip()
        if not normalized_base or normalized_base.startswith("-"):
            raise CodeWorktreeError("invalid_base_ref", "Worktree 基础版本无效")
        base_head = self._git(
            repo_root,
            ["rev-parse", "--verify", "--end-of-options", f"{normalized_base}^{{commit}}"],
        ).strip()
        container = repo_root.parent / f"{repo_root.name}.claude-web-worktrees"
        if container.exists() and (container.is_symlink() or not container.is_dir()):
            raise CodeWorktreeError("unsafe_worktree_root", "Worktree 专用目录不安全")
        container.mkdir(mode=0o700, exist_ok=True)
        if container.resolve() != (repo_root.parent / container.name).resolve():
            raise CodeWorktreeError("unsafe_worktree_root", "Worktree 专用目录已逃逸仓库父目录")
        target = container / normalized_slug
        if target.parent.resolve() != container.resolve():
            raise CodeWorktreeError("unsafe_worktree_path", "Worktree 路径必须位于专用目录内")
        if target.exists() or target.is_symlink():
            raise CodeWorktreeError("duplicate_worktree", "同名 Worktree 已存在")

        worktree_id = uuid.uuid4().hex
        worktree_session_id = uuid.uuid4().hex
        now = time.time()
        try:
            with self.connect(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO code_worktrees (
                        id, source_session_id, worktree_session_id, repo_root,
                        worktree_path, slug, branch, base_ref, base_head,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?)
                    """,
                    (
                        worktree_id, source_session_id, worktree_session_id,
                        str(repo_root), str(target), normalized_slug,
                        normalized_branch, normalized_base, base_head, now, now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CodeWorktreeError("duplicate_worktree", "同名 Worktree 已登记") from exc

        try:
            self._git(
                repo_root,
                ["worktree", "add", "-b", normalized_branch, str(target), base_head],
            )
            with self.connect(immediate=True) as conn:
                self._ensure_worktree_session(
                    conn,
                    session_id=worktree_session_id,
                    cwd=str(target),
                    title=f"Worktree: {normalized_slug}",
                )
                conn.execute(
                    """
                    UPDATE code_worktrees
                    SET status = 'active', error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (time.time(), worktree_id),
                )
        except Exception as exc:
            self._mark_failed(worktree_id, str(exc))
            if isinstance(exc, CodeWorktreeError):
                raise
            raise CodeWorktreeError("worktree_create_failed", str(exc)) from exc
        return self.get(source_session_id, worktree_id)

    def list(self, session_id: str) -> List[Dict[str, object]]:
        self._code_session(session_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM code_worktrees
                WHERE source_session_id = ? OR worktree_session_id = ?
                ORDER BY created_at DESC
                """,
                (session_id, session_id),
            ).fetchall()
        return [self._reconcile(dict(row)) for row in rows]

    def get(self, session_id: str, worktree_id: str) -> Dict[str, object]:
        self._code_session(session_id)
        row = self._registry_row(worktree_id)
        self._require_access(session_id, row)
        return self._reconcile(row)

    def set_runtime_active(self, session_id: str, worktree_id: str, active: bool) -> Dict[str, object]:
        row = self._registry_row(worktree_id)
        self._require_access(session_id, row)
        with self.connect(immediate=True) as conn:
            conn.execute(
                "UPDATE code_worktrees SET runtime_active = ?, updated_at = ? WHERE id = ?",
                (1 if active else 0, time.time(), worktree_id),
            )
        return self.get(session_id, worktree_id)

    def remove(self, session_id: str, worktree_id: str, *, confirm: bool = False) -> Dict[str, object]:
        if not confirm:
            raise CodeWorktreeError("confirmation_required", "删除 Worktree 需要显式确认")
        row = self._registry_row(worktree_id)
        self._require_access(session_id, row)
        state = self._reconcile(row)
        if state["status"] == "removed":
            with self.connect(immediate=True) as conn:
                self._archive_worktree_session(conn, str(row["worktree_session_id"]))
            return state
        if state["status"] in {"creating", "removing"}:
            raise CodeWorktreeError("worktree_active", "Worktree 正在执行管理操作")
        if bool(state["runtime_active"]) or self._session_is_active(str(row["worktree_session_id"])):
            raise CodeWorktreeError("worktree_active", "Worktree Code 会话仍在运行")
        if bool(state["dirty"]):
            raise CodeWorktreeError("worktree_dirty", "Worktree 存在未提交修改，拒绝删除")
        with self.connect(immediate=True) as conn:
            conn.execute(
                "UPDATE code_worktrees SET status = 'removing', updated_at = ? WHERE id = ?",
                (time.time(), worktree_id),
            )
        try:
            if bool(state["exists"]):
                self._git(Path(str(row["repo_root"])), ["worktree", "remove", str(row["worktree_path"])])
            with self.connect(immediate=True) as conn:
                conn.execute(
                    """
                    UPDATE code_worktrees
                    SET status = 'removed', runtime_active = 0, error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (time.time(), worktree_id),
                )
                self._archive_worktree_session(conn, str(row["worktree_session_id"]))
        except Exception as exc:
            with self.connect(immediate=True) as conn:
                conn.execute(
                    "UPDATE code_worktrees SET status = 'active', error = ?, updated_at = ? WHERE id = ?",
                    (str(exc)[:1000], time.time(), worktree_id),
                )
            if isinstance(exc, CodeWorktreeError):
                raise
            raise CodeWorktreeError("worktree_remove_failed", str(exc)) from exc
        return self.get(session_id, worktree_id)

    def _reconcile(self, row: Dict[str, object]) -> Dict[str, object]:
        repo_root = Path(str(row["repo_root"]))
        worktree_path = Path(str(row["worktree_path"]))
        registered = self._registered_worktree_paths(repo_root)
        exists = worktree_path.is_dir() and worktree_path.resolve() in registered
        status = str(row["status"])
        if exists and status in {"creating", "failed", "missing"}:
            try:
                with self.connect(immediate=True) as conn:
                    self._ensure_worktree_session(
                        conn,
                        session_id=str(row["worktree_session_id"]),
                        cwd=str(worktree_path),
                        title=f"Worktree: {row['slug']}",
                    )
                    conn.execute(
                        "UPDATE code_worktrees SET status = 'active', error = '', updated_at = ? WHERE id = ?",
                        (time.time(), row["id"]),
                    )
                status = "active"
            except sqlite3.Error:
                status = "failed"
        elif not exists and status == "removing":
            status = "removed"
            with self.connect(immediate=True) as conn:
                conn.execute(
                    "UPDATE code_worktrees SET status = ?, updated_at = ? WHERE id = ?",
                    (status, time.time(), row["id"]),
                )
                self._archive_worktree_session(conn, str(row["worktree_session_id"]))
        elif not exists and status == "active":
            status = "missing"
            self._update_registry_status(str(row["id"]), status)

        branch = ""
        head = ""
        dirty = False
        if exists:
            branch = self._git_optional(worktree_path, ["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
            head = self._git_optional(worktree_path, ["rev-parse", "HEAD"]).strip()
            dirty = bool(self._git_optional(worktree_path, ["status", "--porcelain", "--untracked-files=normal"]).strip())
        runtime_active = bool(row.get("runtime_active")) or self._session_is_active(str(row["worktree_session_id"]))
        return {
            "id": row["id"],
            "source_session_id": row["source_session_id"],
            "worktree_session_id": row["worktree_session_id"],
            "repo_root": str(repo_root),
            "path": str(worktree_path),
            "slug": row["slug"],
            "branch": branch or row["branch"],
            "registered_branch": row["branch"],
            "base_ref": row["base_ref"],
            "base_head": row["base_head"],
            "head": head,
            "status": status,
            "exists": exists,
            "dirty": dirty,
            "runtime_active": runtime_active,
            "error": row.get("error") or "",
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def _code_session(self, session_id: str) -> Dict[str, object]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, cwd, workspace_mode FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise CodeWorktreeError("session_not_found", "Code 会话不存在")
        if str(row["workspace_mode"] or "chat") != "code":
            raise CodeWorktreeError("code_session_required", "Worktree 仅支持 Code 会话")
        cwd = str(row["cwd"] or "").strip()
        if not cwd:
            raise CodeWorktreeError("project_required", "Code 会话尚未绑定项目目录")
        return dict(row)

    def _main_repo_root(self, cwd: Path) -> Path:
        try:
            current_root = Path(self._git(cwd, ["rev-parse", "--show-toplevel"]).strip()).resolve()
        except CodeWorktreeError as exc:
            raise CodeWorktreeError("git_repo_required", "Code 会话目录不是 Git 仓库") from exc
        output = self._git(current_root, ["worktree", "list", "--porcelain"])
        for line in output.splitlines():
            if line.startswith("worktree "):
                return Path(line[9:]).resolve()
        raise CodeWorktreeError("git_repo_required", "无法解析 Git 主工作区")

    @staticmethod
    def _validate_slug(slug: str) -> str:
        value = str(slug or "").strip()
        if not _SLUG_RE.fullmatch(value) or value in {".", ".."}:
            raise CodeWorktreeError("invalid_slug", "Worktree 名称只能包含字母、数字、点、下划线和短横线")
        return value

    def _validate_branch(self, repo_root: Path, branch: str) -> None:
        if not branch or branch.startswith("-"):
            raise CodeWorktreeError("invalid_branch", "Worktree 分支名无效")
        try:
            self._git(repo_root, ["check-ref-format", f"refs/heads/{branch}"])
        except CodeWorktreeError as exc:
            raise CodeWorktreeError("invalid_branch", "Worktree 分支名无效") from exc

    def _registered_worktree_paths(self, repo_root: Path) -> set[Path]:
        output = self._git(repo_root, ["worktree", "list", "--porcelain"])
        paths: List[Path] = []
        for line in output.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[9:]).resolve())
        return set(paths)

    def _registry_row(self, worktree_id: str) -> Dict[str, object]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM code_worktrees WHERE id = ?", (worktree_id,)).fetchone()
        if row is None:
            raise CodeWorktreeError("worktree_not_found", "Worktree 不存在")
        return dict(row)

    @staticmethod
    def _require_access(session_id: str, row: Dict[str, object]) -> None:
        if session_id not in {row["source_session_id"], row["worktree_session_id"]}:
            raise CodeWorktreeError("worktree_forbidden", "Worktree 不属于当前 Code 会话")

    def _session_is_active(self, session_id: str) -> bool:
        if self.activity_checker is not None and self.activity_checker(session_id):
            return True
        with self.connect() as conn:
            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            if "code_turn_requests" in tables:
                placeholders = ",".join("?" for _ in _ACTIVE_TURN_STATES)
                row = conn.execute(
                    f"SELECT 1 FROM code_turn_requests WHERE session_id = ? AND state IN ({placeholders}) LIMIT 1",
                    (session_id, *sorted(_ACTIVE_TURN_STATES)),
                ).fetchone()
                if row:
                    return True
            if "code_message_queue" in tables:
                placeholders = ",".join("?" for _ in _ACTIVE_QUEUE_STATES)
                row = conn.execute(
                    f"SELECT 1 FROM code_message_queue WHERE session_id = ? AND state IN ({placeholders}) LIMIT 1",
                    (session_id, *sorted(_ACTIVE_QUEUE_STATES)),
                ).fetchone()
                if row:
                    return True
        return False

    def _ensure_worktree_session(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        cwd: str,
        title: str,
    ) -> None:
        existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE sessions SET cwd = ?, workspace_mode = 'code' WHERE id = ?",
                (cwd, session_id),
            )
            return
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        values: Dict[str, object] = {"id": session_id, "cwd": cwd, "workspace_mode": "code"}
        now = time.time()
        if "title" in columns:
            values["title"] = title
        if "created_at" in columns:
            values["created_at"] = now
        if "updated_at" in columns:
            values["updated_at"] = now
        names = [name for name in values if name in columns]
        placeholders = ",".join("?" for _ in names)
        conn.execute(
            f"INSERT INTO sessions ({','.join(names)}) VALUES ({placeholders})",
            tuple(values[name] for name in names),
        )

    @staticmethod
    def _archive_worktree_session(conn: sqlite3.Connection, session_id: str) -> None:
        """Keep the task history recoverable while hiding a removed worktree session."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "archived" not in columns:
            return
        assignments = ["archived = 1"]
        values: List[object] = []
        if "updated_at" in columns:
            assignments.append("updated_at = ?")
            values.append(time.time())
        values.append(session_id)
        conn.execute(
            f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
            tuple(values),
        )

    def _mark_failed(self, worktree_id: str, error: str) -> None:
        with self.connect(immediate=True) as conn:
            conn.execute(
                "UPDATE code_worktrees SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error[:1000], time.time(), worktree_id),
            )

    def _update_registry_status(self, worktree_id: str, status: str) -> None:
        with self.connect(immediate=True) as conn:
            conn.execute(
                "UPDATE code_worktrees SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), worktree_id),
            )

    def _git(self, cwd: Path, args: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=self.git_timeout_seconds,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodeWorktreeError("git_failed", str(exc)) from exc
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "Git 命令执行失败").strip()
            raise CodeWorktreeError("git_failed", message[:1000])
        return completed.stdout

    def _git_optional(self, cwd: Path, args: Sequence[str]) -> str:
        try:
            return self._git(cwd, args)
        except CodeWorktreeError:
            return ""
