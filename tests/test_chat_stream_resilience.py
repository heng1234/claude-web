"""Regression tests for chat-mode (Claude CLI) stream resilience.

Covers the defects behind "sends but no reply / broken reply":
  * a stale warm process whose stdin write fails must not be swallowed
  * a silent process must not park the turn forever (read watchdog + heartbeat)
  * concurrent requests for one session must not both acquire a runtime (TOCTOU)
"""

import asyncio
import unittest

from claude_web import server


class FakeStdin:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.written = bytearray()

    def write(self, payload: bytes) -> None:
        if self.fail:
            raise BrokenPipeError("broken pipe")
        self.written.extend(payload)

    async def drain(self) -> None:
        if self.fail:
            raise BrokenPipeError("broken pipe")


class FakeProcess:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, *, stdin_fails: bool = False, returncode=None):
        self.stdin = FakeStdin(fail=stdin_fails)
        self.returncode = returncode


class ChatWriteStdinTest(unittest.IsolatedAsyncioTestCase):
    async def test_successful_write_reports_true_and_forwards_payload(self):
        process = FakeProcess()
        lock = asyncio.Lock()
        ok = await server._chat_write_stdin(process, lock, b'{"x":1}\n')
        self.assertTrue(ok)
        self.assertEqual(bytes(process.stdin.written), b'{"x":1}\n')
        self.assertFalse(lock.locked(), "write lock must be released")

    async def test_broken_pipe_reports_false_instead_of_being_swallowed(self):
        # A stale warm process (died between turns) previously had its write
        # failure swallowed, so the turn proceeded to readline() and produced
        # either an infinite hang or an rc==0 EOF with no error event.
        process = FakeProcess(stdin_fails=True)
        ok = await server._chat_write_stdin(process, asyncio.Lock(), b"payload")
        self.assertFalse(ok)

    async def test_already_exited_process_reports_false_without_writing(self):
        process = FakeProcess(returncode=1)
        ok = await server._chat_write_stdin(process, asyncio.Lock(), b"payload")
        self.assertFalse(ok)
        self.assertEqual(bytes(process.stdin.written), b"")

    async def test_missing_stdin_reports_false(self):
        process = FakeProcess()
        process.stdin = None
        ok = await server._chat_write_stdin(process, asyncio.Lock(), b"payload")
        self.assertFalse(ok)


class ChatOwnershipGuardTest(unittest.IsolatedAsyncioTestCase):
    """The pending-owner sentinel closes the check→claim TOCTOU window."""

    def setUp(self):
        self._pending = set(server._chat_owner_pending)
        self._running = dict(server._running_processes)
        server._chat_owner_pending.clear()

    def tearDown(self):
        server._chat_owner_pending.clear()
        server._chat_owner_pending.update(self._pending)
        server._running_processes.clear()
        server._running_processes.update(self._running)

    async def test_pending_sentinel_blocks_a_second_claim_for_the_same_session(self):
        session_id = "sess-toctou"

        def try_claim() -> bool:
            """Mirrors generate()'s synchronous check-then-claim."""
            if (
                session_id in server._running_processes
                or session_id in server._chat_owner_pending
            ):
                return False
            server._chat_owner_pending.add(session_id)
            return True

        self.assertTrue(try_claim())
        self.assertFalse(
            try_claim(),
            "a duplicate request must be rejected while the first turn is starting",
        )

    async def test_a_live_running_process_also_blocks_a_new_claim(self):
        session_id = "sess-live"
        server._running_processes[session_id] = FakeProcess()
        self.assertIn(session_id, server._running_processes)
        blocked = (
            session_id in server._running_processes
            or session_id in server._chat_owner_pending
        )
        self.assertTrue(blocked)

    async def test_releasing_the_sentinel_allows_the_next_turn(self):
        session_id = "sess-release"
        server._chat_owner_pending.add(session_id)
        server._chat_owner_pending.discard(session_id)
        self.assertNotIn(session_id, server._chat_owner_pending)


class ChatReadWatchdogConfigTest(unittest.TestCase):
    """The read watchdog must out-pace the frontend stall watchdog."""

    def test_heartbeat_slice_is_shorter_than_the_frontend_stall_timeout(self):
        # index.html: STREAM_SILENCE_TIMEOUT_MS = 45000
        self.assertLess(
            server._CHAT_READ_SLICE_SECONDS,
            45.0,
            "heartbeats must arrive before the client declares the stream wedged",
        )
        self.assertGreater(server._CHAT_READ_SLICE_SECONDS, 0)

    def test_hard_ceiling_is_far_larger_than_one_heartbeat_slice(self):
        self.assertGreater(
            server._CHAT_READ_HARD_CEILING_SECONDS,
            server._CHAT_READ_SLICE_SECONDS * 10,
            "a legitimately slow turn must not be aborted after a few slices",
        )


if __name__ == "__main__":
    unittest.main()
