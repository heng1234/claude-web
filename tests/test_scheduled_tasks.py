"""Unit tests for scheduled task management.

Scheduled tasks let claude-web fire a Claude turn automatically on a cron
schedule, reusing the same _chat_response path a manual message takes. Unlike
Claude Code's in-session /loop (session-scoped, non-persistent, not externally
triggerable), these tasks survive restarts, run across sessions, and are
managed from a dedicated panel.

These tests pin:
  1. The self-contained 5-field cron parser + next-run computation.
  2. The scheduled_tasks CRUD helpers against an isolated temp DB.
  3. The due-selection logic used by the scheduler loop.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web import server


# ---------------------------------------------------------------------------
# Cron parser (pure functions — no DB)
# ---------------------------------------------------------------------------
class CronParseTest(unittest.TestCase):
    def test_all_wildcards_matches_every_minute(self):
        spec = server._cron_parse("* * * * *")
        # 2026-09-05 12:34 (a Saturday) should match.
        self.assertTrue(server._cron_matches(spec, minute=34, hour=12, day=5, month=9, weekday=5))

    def test_specific_minute_hour(self):
        spec = server._cron_parse("30 9 * * *")
        self.assertTrue(server._cron_matches(spec, minute=30, hour=9, day=1, month=1, weekday=0))
        self.assertFalse(server._cron_matches(spec, minute=31, hour=9, day=1, month=1, weekday=0))
        self.assertFalse(server._cron_matches(spec, minute=30, hour=10, day=1, month=1, weekday=0))

    def test_step_values(self):
        spec = server._cron_parse("*/15 * * * *")
        for m in (0, 15, 30, 45):
            self.assertTrue(server._cron_matches(spec, minute=m, hour=0, day=1, month=1, weekday=0))
        for m in (1, 14, 16, 31):
            self.assertFalse(server._cron_matches(spec, minute=m, hour=0, day=1, month=1, weekday=0))

    def test_range_values(self):
        spec = server._cron_parse("0 9 * * 1-5")  # weekdays at 09:00
        for wd in (0, 1, 2, 3, 4):  # Mon-Fri (0=Mon in our convention)
            self.assertTrue(server._cron_matches(spec, minute=0, hour=9, day=1, month=1, weekday=wd))
        for wd in (5, 6):  # Sat, Sun
            self.assertFalse(server._cron_matches(spec, minute=0, hour=9, day=1, month=1, weekday=wd))

    def test_list_values(self):
        spec = server._cron_parse("0 0,12 * * *")
        self.assertTrue(server._cron_matches(spec, minute=0, hour=0, day=1, month=1, weekday=0))
        self.assertTrue(server._cron_matches(spec, minute=0, hour=12, day=1, month=1, weekday=0))
        self.assertFalse(server._cron_matches(spec, minute=0, hour=6, day=1, month=1, weekday=0))

    def test_invalid_expression_raises(self):
        for bad in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * * * 8", "a b c d e"):
            with self.assertRaises(ValueError):
                server._cron_parse(bad)

    def test_weekday_7_is_sunday(self):
        # Cron traditionally allows 0 and 7 for Sunday; both map to Python 6.
        spec0 = server._cron_parse("0 0 * * 0")
        spec7 = server._cron_parse("0 0 * * 7")
        self.assertTrue(server._cron_matches(spec0, minute=0, hour=0, day=1, month=1, weekday=6))
        self.assertTrue(server._cron_matches(spec7, minute=0, hour=0, day=1, month=1, weekday=6))

    def test_next_run_after_advances(self):
        import datetime as dt
        # From 2026-09-05 08:59 local, "0 9 * * *" -> 2026-09-05 09:00.
        base = dt.datetime(2026, 9, 5, 8, 59, 0)
        nxt = server._cron_next_run("0 9 * * *", after=base)
        self.assertEqual((nxt.hour, nxt.minute), (9, 0))
        self.assertEqual(nxt.date(), base.date())

    def test_next_run_rolls_to_next_day(self):
        import datetime as dt
        base = dt.datetime(2026, 9, 5, 9, 1, 0)
        nxt = server._cron_next_run("0 9 * * *", after=base)
        self.assertEqual((nxt.hour, nxt.minute), (9, 0))
        self.assertEqual(nxt.day, 6)


# ---------------------------------------------------------------------------
# CRUD helpers (isolated temp DB)
# ---------------------------------------------------------------------------
class ScheduledTaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "sched.db"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.init_patch = patch.object(server, "_DB_INITIALIZED", False)
        self.init_patch.start()
        server.init_db()

    def tearDown(self) -> None:
        self.init_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_create_roundtrips(self):
        tid = server._scheduled_task_create({
            "name": "每日晨报",
            "cron_expr": "0 9 * * *",
            "message": "总结今天的待办",
            "execution_model": "new_session",
            "cwd": "/tmp/proj",
            "model": "claude-opus-4-8",
        })
        task = server._scheduled_task_get(tid)
        self.assertEqual(task["name"], "每日晨报")
        self.assertEqual(task["cron_expr"], "0 9 * * *")
        self.assertEqual(task["execution_model"], "new_session")
        self.assertEqual(task["enabled"], 1)
        self.assertIsNotNone(task["next_run_at"])

    def test_invalid_cron_rejected_on_create(self):
        with self.assertRaises(ValueError):
            server._scheduled_task_create({
                "name": "bad",
                "cron_expr": "not a cron",
                "message": "x",
                "execution_model": "new_session",
            })

    def test_bound_session_requires_session_id(self):
        with self.assertRaises(ValueError):
            server._scheduled_task_create({
                "name": "bound",
                "cron_expr": "* * * * *",
                "message": "x",
                "execution_model": "bound_session",
                "bound_session_id": "",
            })

    def test_update_recomputes_next_run(self):
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "0 9 * * *", "message": "x",
            "execution_model": "new_session",
        })
        before = server._scheduled_task_get(tid)["next_run_at"]
        server._scheduled_task_update(tid, {
            "name": "t", "cron_expr": "0 10 * * *", "message": "x",
            "execution_model": "new_session",
        })
        after = server._scheduled_task_get(tid)["next_run_at"]
        self.assertNotEqual(before, after)

    def test_toggle_disables(self):
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "* * * * *", "message": "x",
            "execution_model": "new_session",
        })
        server._scheduled_task_set_enabled(tid, False)
        self.assertEqual(server._scheduled_task_get(tid)["enabled"], 0)
        server._scheduled_task_set_enabled(tid, True)
        self.assertEqual(server._scheduled_task_get(tid)["enabled"], 1)

    def test_delete(self):
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "* * * * *", "message": "x",
            "execution_model": "new_session",
        })
        server._scheduled_task_delete(tid)
        self.assertIsNone(server._scheduled_task_get(tid))

    def test_due_selection_only_enabled_and_past(self):
        import time as _time
        now = _time.time()
        due = server._scheduled_task_create({
            "name": "due", "cron_expr": "* * * * *", "message": "x",
            "execution_model": "new_session",
        })
        server._scheduled_task_mark_next_run(due, now - 10)
        future = server._scheduled_task_create({
            "name": "future", "cron_expr": "* * * * *", "message": "x",
            "execution_model": "new_session",
        })
        server._scheduled_task_mark_next_run(future, now + 3600)
        disabled = server._scheduled_task_create({
            "name": "off", "cron_expr": "* * * * *", "message": "x",
            "execution_model": "new_session",
        })
        server._scheduled_task_mark_next_run(disabled, now - 10)
        server._scheduled_task_set_enabled(disabled, False)

        ids = {t["id"] for t in server._scheduled_task_due(now)}
        self.assertIn(due, ids)
        self.assertNotIn(future, ids)
        self.assertNotIn(disabled, ids)


# ---------------------------------------------------------------------------
# Runner (isolated temp DB + mocked _chat_response)
# ---------------------------------------------------------------------------
class ScheduledTaskRunnerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "run.db"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.init_patch = patch.object(server, "_DB_INITIALIZED", False)
        self.init_patch.start()
        server.init_db()

    def tearDown(self) -> None:
        self.init_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def _fake_response(self, events):
        class _Resp:
            def __init__(self, evs):
                self._evs = evs

            @property
            async def body_iterator(self):
                for e in self._evs:
                    import json as _json
                    yield f"data: {_json.dumps(e)}\n\n".encode("utf-8")
        return _Resp(events)

    async def test_new_session_run_records_ok(self):
        from unittest.mock import AsyncMock
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "0 9 * * *", "message": "hi",
            "execution_model": "new_session",
        })
        resp = self._fake_response([{"type": "assistant", "text": "done"}])
        with patch.object(server, "_chat_response", AsyncMock(return_value=resp)):
            await server._run_scheduled_task(server._scheduled_task_get(tid))
        task = server._scheduled_task_get(tid)
        self.assertEqual(task["last_status"], "ok")
        self.assertIsNotNone(task["last_run_at"])

    async def test_error_event_records_error(self):
        from unittest.mock import AsyncMock
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "0 9 * * *", "message": "hi",
            "execution_model": "new_session",
        })
        resp = self._fake_response([{"type": "error", "message": "boom"}])
        with patch.object(server, "_chat_response", AsyncMock(return_value=resp)):
            await server._run_scheduled_task(server._scheduled_task_get(tid))
        task = server._scheduled_task_get(tid)
        self.assertEqual(task["last_status"], "error")
        self.assertIn("boom", task["last_error"])

    async def test_bound_session_missing_is_skipped_not_run(self):
        from unittest.mock import AsyncMock
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "0 9 * * *", "message": "hi",
            "execution_model": "bound_session", "bound_session_id": "nope",
        })
        chat_mock = AsyncMock()
        with patch.object(server, "_chat_response", chat_mock):
            await server._run_scheduled_task(server._scheduled_task_get(tid))
        chat_mock.assert_not_awaited()
        self.assertEqual(server._scheduled_task_get(tid)["last_status"], "skipped")

    async def test_bound_session_busy_is_skipped(self):
        from unittest.mock import AsyncMock
        sid = "sched-bound-" + uuid_hex()
        server.upsert_session(sid, "bound", self.tmp.name, "code")
        tid = server._scheduled_task_create({
            "name": "t", "cron_expr": "0 9 * * *", "message": "hi",
            "execution_model": "bound_session", "bound_session_id": sid,
        })
        chat_mock = AsyncMock()
        with patch.object(server, "_session_runtime_busy", return_value=True), \
             patch.object(server, "_chat_response", chat_mock):
            await server._run_scheduled_task(server._scheduled_task_get(tid))
        chat_mock.assert_not_awaited()
        self.assertEqual(server._scheduled_task_get(tid)["last_status"], "skipped")


def uuid_hex() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex


if __name__ == "__main__":
    unittest.main()
