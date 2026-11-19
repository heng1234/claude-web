import asyncio
import json
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_web.agent_sdk_bridge import AgentSdkBridge, AgentSdkBridgeError, AgentSdkTurn


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the bridge protocol test")
class AgentSdkBridgeProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.daemon = Path(self.temp_dir.name) / "fake-daemon.mjs"
        self.daemon.write_text(
            textwrap.dedent(
                """
                import { createInterface } from 'node:readline';
                const write = (value) => {
                  const body = Buffer.from(JSON.stringify(value), 'utf8');
                  const header = Buffer.allocUnsafe(4);
                  header.writeUInt32BE(body.length, 0);
                  process.stdout.write(Buffer.concat([header, body]));
                };
                const writeInvalidOversizedHeader = () => {
                  const header = Buffer.allocUnsafe(4);
                  header.writeUInt32BE(64 * 1024 * 1024 + 1, 0);
                  process.stdout.write(header);
                };
                write({type: 'ready', protocol: 2, sdk: {version: 'test', path: '/fake/sdk'}});
                const reader = createInterface({input: process.stdin, crlfDelay: Infinity});
                let pendingPermissionTurn = null;
                reader.on('line', (line) => {
                  const command = JSON.parse(line);
                  if (command.method === 'send') {
                    const sessionId = command.params.resumeSessionId || 'native-session';
                    write({id: command.id, type: 'accepted', sessionId});
                    if (command.params.message === 'needs permission') {
                      pendingPermissionTurn = {id: command.id, sessionId};
                      write({id: command.id, type: 'permission_request', approvalId: 'approval-1',
                        toolName: 'Bash', input: {command: 'pwd'}, suggestions: [{type: 'addRules'}]});
                      return;
                    }
                    if (command.params.message === 'large event') {
                      write({id: command.id, type: 'event', event: {
                        type: 'assistant', payload: 'x'.repeat(1024 * 1024)
                      }});
                      write({id: command.id, type: 'done', success: true, sessionId});
                      return;
                    }
                    if (command.params.message === 'corrupt frame') {
                      writeInvalidOversizedHeader();
                      return;
                    }
                    write({id: command.id, type: 'event', event: {
                      type: 'system', subtype: 'init', session_id: sessionId
                    }});
                    write({id: command.id, type: 'event', event: {
                      type: 'result', subtype: 'success', is_error: false,
                      session_id: sessionId, usage: {input_tokens: 12, output_tokens: 3}
                    }});
                    write({id: command.id, type: 'done', success: true, sessionId});
                  } else if (command.method === 'context') {
                    write({id: command.id, type: 'response', ok: true,
                      usage: {totalTokens: 4300, maxTokens: 1000000, percentage: 0.43}});
                  } else if (command.method === 'reconnect_session') {
                    write({id: command.id, type: 'response', ok: true, reconnected: true,
                      sessionId: command.params.resumeSessionId || command.params.sessionId});
                  } else if (command.method === 'pending_permissions') {
                    const pending = pendingPermissionTurn ? [{
                      approvalId: 'approval-1', sessionKey: command.params.sessionKey,
                      toolName: 'Bash', input: {command: 'pwd'}, suggestions: [{type: 'addRules'}],
                      toolUseId: 'tool-1', agentId: 'agent-1', blockedPath: '/tmp/demo'
                    }] : [];
                    write({id: command.id, type: 'response', ok: true, pending});
                  } else if (command.method === 'set_model') {
                    write({id: command.id, type: 'response', ok: true, applied: true,
                      model: command.params.model});
                  } else if (command.method === 'set_permission_mode') {
                    write({id: command.id, type: 'response', ok: true, applied: true,
                      permissionMode: command.params.permissionMode});
                  } else if (command.method === 'fork_session') {
                    write({id: command.id, type: 'response', ok: true,
                      sessionId: 'forked-native', sourceSessionId: command.params.sourceSessionId});
                  } else if (command.method === 'session_messages') {
                    write({id: command.id, type: 'response', ok: true, messages: [
                      {type: 'user', uuid: 'native-user-0001', message: {content: [{type: 'text', text: 'hello'}]}}
                    ]});
                  } else if (command.method === 'rewind_files') {
                    write({id: command.id, type: 'response', ok: true,
                      result: {canRewind: true, filesChanged: ['demo.py']}});
                  } else if (command.method === 'permission_response') {
                    write({id: command.id, type: 'response', ok: true, approvalId: command.params.approvalId});
                    const pending = pendingPermissionTurn;
                    pendingPermissionTurn = null;
                    if (pending) {
                      write({id: pending.id, type: 'event', event: {
                        type: 'result', subtype: 'success', is_error: false,
                        session_id: pending.sessionId, permissionAllowed: command.params.allow
                      }});
                      write({id: pending.id, type: 'done', success: true, sessionId: pending.sessionId});
                    }
                  } else if (command.method === 'interrupt' || command.method === 'close_session') {
                    write({id: command.id, type: 'response', ok: true});
                  } else if (command.method === 'shutdown') {
                    write({id: command.id, type: 'response', ok: true});
                    setTimeout(() => process.exit(0), 10);
                  }
                });
                """
            ),
            encoding="utf-8",
        )
        self.bridge = AgentSdkBridge(self.daemon)

    async def asyncTearDown(self):
        await self.bridge.shutdown()
        self.temp_dir.cleanup()

    async def test_routes_native_turn_events_and_control_responses(self):
        self.assertTrue(await self.bridge.ensure_started())
        self.assertEqual("test", self.bridge.status()["sdk"]["version"])

        turn = await self.bridge.open_turn(
            "local-session",
            {"message": "hello", "cwd": self.temp_dir.name, "sessionId": "requested-session"},
        )
        envelopes = [envelope async for envelope in turn.events()]
        self.assertEqual(["event", "event", "done"], [item["type"] for item in envelopes])
        self.assertEqual("native-session", envelopes[-1]["sessionId"])

        context = await self.bridge.context_usage("local-session")
        self.assertEqual(1_000_000, context["usage"]["maxTokens"])

        reconnect = await self.bridge.reconnect_session(
            "local-session",
            {"resumeSessionId": "native-session", "cwd": self.temp_dir.name},
        )
        self.assertTrue(reconnect["reconnected"])
        self.assertEqual("native-session", reconnect["sessionId"])

    async def test_permission_response_resumes_the_same_native_turn(self):
        turn = await self.bridge.open_turn(
            "permission-session",
            {"message": "needs permission", "cwd": self.temp_dir.name, "sessionId": "permission-native"},
        )
        iterator = turn.events().__aiter__()
        request = await iterator.__anext__()
        self.assertEqual("permission_request", request["type"])
        self.assertEqual("Bash", request["toolName"])

        pending = await self.bridge.pending_permissions("permission-session")
        self.assertEqual("approval-1", pending["pending"][0]["approvalId"])
        self.assertEqual("tool-1", pending["pending"][0]["toolUseId"])

        response = await self.bridge.respond_permission(
            "permission-session",
            "approval-1",
            allow=True,
            use_suggestions=True,
            updated_input={"command": "pwd"},
        )
        self.assertTrue(response["ok"])
        result = await iterator.__anext__()
        done = await iterator.__anext__()
        self.assertTrue(result["event"]["permissionAllowed"])
        self.assertEqual("done", done["type"])

    async def test_native_controls_and_session_operations(self):
        model = await self.bridge.set_model("local-session", "sonnet", runtime_epoch="native-session")
        self.assertTrue(model["applied"])
        self.assertEqual("sonnet", model["model"])

        permission = await self.bridge.set_permission_mode(
            "local-session", "acceptEdits", runtime_epoch="native-session"
        )
        self.assertEqual("acceptEdits", permission["permissionMode"])

        forked = await self.bridge.fork_session(
            "native-session", cwd=self.temp_dir.name, up_to_message_id="native-user-0001"
        )
        self.assertEqual("forked-native", forked["sessionId"])

        messages = await self.bridge.session_messages("native-session", cwd=self.temp_dir.name)
        self.assertEqual("native-user-0001", messages[0]["uuid"])

        rewind = await self.bridge.rewind_files(
            "local-session",
            "native-user-0001",
            {"cwd": self.temp_dir.name, "resumeSessionId": "native-session"},
        )
        self.assertEqual(["demo.py"], rewind["result"]["filesChanged"])

    async def test_large_sdk_event_is_not_limited_by_asyncio_readline(self):
        turn = await self.bridge.open_turn(
            "large-event-session",
            {"message": "large event", "cwd": self.temp_dir.name, "sessionId": "large-native"},
        )
        envelopes = [envelope async for envelope in turn.events()]
        self.assertEqual(["event", "done"], [item["type"] for item in envelopes])
        self.assertEqual(1024 * 1024, len(envelopes[0]["event"]["payload"]))
        self.assertTrue(self.bridge.running)

    async def test_reader_failure_marks_bridge_unhealthy_and_restarts_daemon(self):
        self.assertTrue(await self.bridge.ensure_started())
        original_pid = self.bridge.process.pid
        turn = await self.bridge.open_turn(
            "corrupt-frame-session",
            {"message": "corrupt frame", "cwd": self.temp_dir.name, "sessionId": "corrupt-native"},
        )
        envelopes = [envelope async for envelope in turn.events()]
        self.assertEqual(["error", "done"], [item["type"] for item in envelopes])
        self.assertEqual("bridge_transport_lost", envelopes[0]["code"])
        self.assertIn("frame size", envelopes[0]["message"])
        self.assertFalse(self.bridge.running)

        self.assertTrue(await self.bridge.ensure_started())
        self.assertNotEqual(original_pid, self.bridge.process.pid)
        recovered = await self.bridge.open_turn(
            "recovered-session",
            {"message": "hello", "cwd": self.temp_dir.name, "sessionId": "recovered-native"},
        )
        recovered_envelopes = [envelope async for envelope in recovered.events()]
        self.assertEqual("done", recovered_envelopes[-1]["type"])

    async def test_explicit_restart_finishes_old_turn_before_starting_new_reader(self):
        turn = await self.bridge.open_turn(
            "restart-session",
            {"message": "needs permission", "cwd": self.temp_dir.name, "sessionId": "restart-native"},
        )
        original_pid = self.bridge.process.pid
        # Acknowledgement does not guarantee the next frame has arrived. Wait
        # for the permission frame whose preservation this test exercises.
        permission = await asyncio.wait_for(turn.queue.get(), timeout=2.0)
        self.assertEqual("permission_request", permission["type"])
        turn.queue.put_nowait(permission)

        self.assertTrue(await self.bridge.restart("test restart"))
        self.assertNotEqual(original_pid, self.bridge.process.pid)
        envelopes = [envelope async for envelope in turn.events()]
        self.assertEqual("permission_request", envelopes[0]["type"])
        self.assertEqual(["error", "done"], [item["type"] for item in envelopes[-2:]])

        recovered = await self.bridge.open_turn(
            "post-restart-session",
            {"message": "hello", "cwd": self.temp_dir.name, "sessionId": "post-restart-native"},
        )
        self.assertEqual("done", [item async for item in recovered.events()][-1]["type"])

    async def test_transport_failure_unblocks_a_full_turn_queue(self):
        queue = asyncio.Queue(maxsize=2)
        queue.put_nowait({"type": "event", "sequence": 1})
        queue.put_nowait({"type": "event", "sequence": 2})

        self.bridge._force_fail_queue(queue, "reader stopped")

        self.assertEqual("error", queue.get_nowait()["type"])
        self.assertEqual("done", queue.get_nowait()["type"])


class AgentSdkEventBufferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = AgentSdkBridge()
        self.stdout = asyncio.StreamReader()
        self.bridge.process = SimpleNamespace(stdout=self.stdout)
        self.bridge._best_effort_interrupt = AsyncMock()
        self.queue = asyncio.Queue(maxsize=8)
        self.bridge._queues["turn"] = self.queue
        self.bridge._turn_sessions["turn"] = "session"
        self.turn = AgentSdkTurn("turn", "session", self.queue)
        self.reader_task = asyncio.create_task(self.bridge._read_stdout())

    async def asyncTearDown(self):
        self.reader_task.cancel()
        await asyncio.gather(self.reader_task, return_exceptions=True)

    async def feed(self, envelopes):
        response = asyncio.get_running_loop().create_future()
        self.bridge._responses["barrier"] = response
        for envelope in [*envelopes, {"id": "barrier", "type": "response", "ok": True}]:
            body = json.dumps(envelope).encode("utf-8")
            self.stdout.feed_data(len(body).to_bytes(4, "big") + body)
        await asyncio.wait_for(response, timeout=2.0)

    def event(self, event_type, **fields):
        return {"id": "turn", "type": "event", "event": {"type": event_type, **fields}}

    async def test_incoming_token_delta_does_not_interrupt_full_essential_queue(self):
        essential = [self.event("assistant", sequence=index) for index in range(8)]
        for envelope in essential:
            self.queue.put_nowait(envelope)

        await self.feed([self.event("stream_event")])

        self.assertIs(self.bridge._queues.get("turn"), self.queue)
        self.assertEqual(essential, [self.queue.get_nowait() for _ in range(8)])
        self.bridge._best_effort_interrupt.assert_not_awaited()

    async def test_buffered_burst_gives_active_consumer_time_to_drain(self):
        consumer = asyncio.create_task(self._collect_turn(self.turn))
        try:
            events = [self.event("assistant", sequence=index) for index in range(128)]
            events.extend([
                self.event("user", message={"content": [{"type": "tool_result", "tool_use_id": "task-1"}]}),
                self.event("result", subtype="success"),
                {"id": "turn", "type": "done", "success": True},
            ])
            await self.feed(events)
            received = await asyncio.wait_for(consumer, timeout=2.0)
            self.assertEqual(events, received)
            self.bridge._best_effort_interrupt.assert_not_awaited()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_burst_does_not_evict_turn_acknowledgement(self):
        self.bridge.ensure_started = AsyncMock(return_value=True)

        async def send(command):
            envelopes = [
                {"type": "accepted"},
                *[{"type": "event", "event": {"type": "assistant", "sequence": index}} for index in range(24)],
                {"type": "done", "success": True},
            ]
            for envelope in envelopes:
                body = json.dumps({"id": command["id"], **envelope}).encode("utf-8")
                self.stdout.feed_data(len(body).to_bytes(4, "big") + body)

        self.bridge._write = AsyncMock(side_effect=send)
        with patch("claude_web.agent_sdk_bridge.DEFAULT_EVENT_QUEUE_SIZE", 8):
            turn = await self.bridge.open_turn("burst-session", {"message": "hello"}, timeout=1.0)
        received = await asyncio.wait_for(
            self._collect_turn(turn), timeout=2.0,
        )
        self.assertEqual(list(range(24)), [item["event"]["sequence"] for item in received[:-1]])
        self.assertEqual({"id": turn.request_id, "type": "done", "success": True}, received[-1])
        self.bridge._best_effort_interrupt.assert_not_awaited()

    async def _collect_turn(self, turn):
        return [envelope async for envelope in turn.events(idle_heartbeat=15.0)]

    async def test_shedding_deltas_keeps_permission_tool_result_and_final_result(self):
        essential = [
            {"id": "turn", "type": "permission_request", "approvalId": "approval-1"},
            self.event("assistant", sequence=1),
            self.event("assistant", sequence=2),
        ]
        for envelope in [*essential, *[self.event("stream_event") for _ in range(5)]]:
            self.queue.put_nowait(envelope)
        terminal = [
            self.event("user", message={"content": [{"type": "tool_result", "tool_use_id": "task-1"}]}),
            self.event("result", subtype="success"),
            {"id": "turn", "type": "done", "success": True},
        ]

        await self.feed(terminal)

        self.assertEqual(essential + terminal, [item async for item in self.turn.events()])
        self.bridge._best_effort_interrupt.assert_not_awaited()

    async def test_stalled_consumer_still_fails_without_losing_control_response(self):
        for index in range(8):
            self.queue.put_nowait(self.event("assistant", sequence=index))

        await self.feed([self.event("result", subtype="success")])

        received = [item async for item in self.turn.events()]
        self.assertEqual(["error", "done"], [item["type"] for item in received[-2:]])
        self.assertIn("event buffer overflowed", received[-2]["message"])
        self.assertNotIn("turn", self.bridge._queues)
        self.bridge._best_effort_interrupt.assert_awaited_once_with("session")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the bridge runtime-limit test")
class AgentSdkRuntimeLimitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sdk_dir = Path(self.temp_dir.name) / "claude-agent-sdk"
        self.sdk_dir.mkdir()
        (self.sdk_dir / "package.json").write_text(
            '{"name":"@anthropic-ai/claude-agent-sdk","version":"0.2.112","type":"module","exports":"./sdk.mjs"}',
            encoding="utf-8",
        )
        (self.sdk_dir / "sdk.mjs").write_text(
            textwrap.dedent(
                """
                export function query({prompt}) {
                  let closed = false;
                  let release = null;
                  return {
                    [Symbol.asyncIterator]() { return this; },
                    async next() {
                      const input = await prompt.next();
                      if (closed || input.done) return {done: true};
                      return await new Promise((resolve) => {
                        release = () => resolve({done: true});
                        if (closed) release();
                      });
                    },
                    close() { closed = true; if (release) release(); },
                    async interrupt() {},
                    async setModel() {},
                    async setPermissionMode() {},
                    async getContextUsage() { return {totalTokens: 0, maxTokens: 200000, percentage: 0}; },
                    async rewindFiles() { return {canRewind: true, filesChanged: []}; },
                  };
                }
                export async function forkSession() { return {sessionId: 'forked'}; }
                export async function getSessionMessages() { return []; }
                """
            ),
            encoding="utf-8",
        )
        self.env = patch.dict(
            os.environ,
            {
                "CLAUDE_AGENT_SDK_PATH": str(self.sdk_dir),
                "CLAUDE_WEB_CODE_RUNTIME": "agent-sdk",
            },
        )
        self.env.start()
        self.bridge = AgentSdkBridge()

    async def asyncTearDown(self):
        await self.bridge.shutdown()
        self.env.stop()
        self.temp_dir.cleanup()

    async def test_ninth_concurrent_runtime_is_rejected(self):
        turns = []
        for index in range(8):
            turns.append(await self.bridge.open_turn(
                f"local-{index}",
                {
                    "message": "hold",
                    "cwd": self.temp_dir.name,
                    "sessionId": f"native-{index}",
                    "runtimeEpoch": f"native-{index}",
                },
            ))
        with self.assertRaises(AgentSdkBridgeError) as raised:
            await self.bridge.open_turn(
                "local-8",
                {
                    "message": "overflow",
                    "cwd": self.temp_dir.name,
                    "sessionId": "native-8",
                    "runtimeEpoch": "native-8",
                },
            )
        self.assertIn("runtime limit reached (8)", str(raised.exception))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the bridge concurrency test")
class AgentSdkCrossSessionConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """A slow control call on one session must not stall another session's send.

    The daemon used to serialize every session through one global mutex, so a
    long getContextUsage (Python allows up to 180s) held the lock that
    reserveSend needed to emit its 'accepted' frame. Any other session's send
    then blew past open_turn's acknowledgement window and surfaced as
    "bridge did not queue the turn".
    """

    CONTEXT_DELAY_SECONDS = 3.0

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sdk_dir = Path(self.temp_dir.name) / "claude-agent-sdk"
        self.sdk_dir.mkdir()
        (self.sdk_dir / "package.json").write_text(
            '{"name":"@anthropic-ai/claude-agent-sdk","version":"0.2.112","type":"module","exports":"./sdk.mjs"}',
            encoding="utf-8",
        )
        context_delay_ms = int(self.CONTEXT_DELAY_SECONDS * 1000)
        (self.sdk_dir / "sdk.mjs").write_text(
            textwrap.dedent(
                f"""
                const CONTEXT_DELAY_MS = {context_delay_ms};
                export function query({{prompt}}) {{
                  let closed = false;
                  let release = null;
                  return {{
                    [Symbol.asyncIterator]() {{ return this; }},
                    async next() {{
                      const input = await prompt.next();
                      if (closed || input.done) return {{done: true}};
                      return await new Promise((resolve) => {{
                        release = () => resolve({{done: true}});
                        if (closed) release();
                      }});
                    }},
                    close() {{ closed = true; if (release) release(); }},
                    async interrupt() {{}},
                    async setModel() {{}},
                    async setPermissionMode() {{}},
                    async getContextUsage() {{
                      // Deliberately slow: emulates a real context inspection that
                      // takes far longer than the send acknowledgement window.
                      await new Promise((resolve) => setTimeout(resolve, CONTEXT_DELAY_MS));
                      return {{totalTokens: 0, maxTokens: 200000, percentage: 0}};
                    }},
                    async rewindFiles() {{ return {{canRewind: true, filesChanged: []}}; }},
                  }};
                }}
                export async function forkSession() {{ return {{sessionId: 'forked'}}; }}
                export async function getSessionMessages() {{ return []; }}
                """
            ),
            encoding="utf-8",
        )
        self.env = patch.dict(
            os.environ,
            {
                "CLAUDE_AGENT_SDK_PATH": str(self.sdk_dir),
                "CLAUDE_WEB_CODE_RUNTIME": "agent-sdk",
            },
        )
        self.env.start()
        self.bridge = AgentSdkBridge()

    async def asyncTearDown(self):
        await self.bridge.shutdown()
        self.env.stop()
        self.temp_dir.cleanup()

    def _params(self, name: str) -> dict:
        return {
            "message": "hold",
            "cwd": self.temp_dir.name,
            "sessionId": f"native-{name}",
            "runtimeEpoch": f"native-{name}",
        }

    async def test_slow_context_on_one_session_does_not_block_another_send(self):
        # Session A: start a context query that blocks inside the SDK.
        context_task = asyncio.create_task(
            self.bridge.context_usage("local-slow", self._params("slow"), timeout=60.0)
        )
        # Give the daemon time to enter getContextUsage and hold its session lock.
        await asyncio.sleep(0.75)
        self.assertFalse(context_task.done(), "context query should still be in flight")

        # Session B: a send must be acknowledged promptly despite A holding its lock.
        started = asyncio.get_running_loop().time()
        turn = await self.bridge.open_turn("local-fast", self._params("fast"))
        elapsed = asyncio.get_running_loop().time() - started

        self.assertIsNotNone(turn)
        # Comfortably below the remaining context delay: proves no cross-session wait.
        self.assertLess(
            elapsed,
            self.CONTEXT_DELAY_SECONDS / 2,
            f"send waited {elapsed:.2f}s — it is serialized behind the other session",
        )
        usage = await context_task
        self.assertTrue(usage.get("ok"))

    async def test_concurrent_sends_for_one_session_accept_only_one(self):
        # The pre-claim in reserveSend must still reject a second concurrent send
        # for the SAME session, so Python never treats two turns as acknowledged.
        results = await asyncio.gather(
            self.bridge.open_turn("local-dup", self._params("dup")),
            self.bridge.open_turn("local-dup", self._params("dup")),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, BaseException)]
        accepted = [item for item in results if not isinstance(item, BaseException)]
        self.assertEqual(len(accepted), 1, f"expected exactly one accepted turn, got {results}")
        self.assertEqual(len(failures), 1)
        self.assertIn("already running", str(failures[0]))


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the selected SDK version test")
class AgentSdkSelectedVersionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.managed_root = Path(self.temp_dir.name) / "managed-sdk"
        self.sdk_dir = (
            self.managed_root / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"
        )
        self.sdk_dir.mkdir(parents=True)
        self.selected_version = "0.2.111"
        (self.sdk_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": "@anthropic-ai/claude-agent-sdk",
                    "version": self.selected_version,
                    "type": "module",
                    "exports": "./sdk.mjs",
                }
            ),
            encoding="utf-8",
        )
        (self.managed_root / ".claude-web-sdk.json").write_text(
            json.dumps(
                {
                    "package": "@anthropic-ai/claude-agent-sdk",
                    "version": self.selected_version,
                    "selectionMode": "custom",
                }
            ),
            encoding="utf-8",
        )
        (self.sdk_dir / "sdk.mjs").write_text(
            textwrap.dedent(
                """
                export function query({prompt}) {
                  return (async function* () {
                    for await (const _message of prompt) {
                      yield {type: 'system', subtype: 'init', session_id: 'selected-native'};
                      yield {type: 'result', subtype: 'success', is_error: false,
                        session_id: 'selected-native', usage: {input_tokens: 1, output_tokens: 1}};
                    }
                  })();
                }
                export async function forkSession() { return {sessionId: 'forked'}; }
                export async function getSessionMessages() { return []; }
                """
            ),
            encoding="utf-8",
        )
        self.env = patch.dict(
            os.environ,
            {
                "CLAUDE_AGENT_SDK_PATH": "",
                "CLAUDE_WEB_AGENT_SDK_HOME": str(self.managed_root),
                "CLAUDE_WEB_CODE_RUNTIME": "agent-sdk",
            },
        )
        self.env.start()
        self.bridge = AgentSdkBridge()

    async def asyncTearDown(self):
        await self.bridge.shutdown()
        self.env.stop()
        self.temp_dir.cleanup()

    async def test_explicit_managed_version_is_loaded_and_smoke_tested(self):
        self.assertTrue(await self.bridge.ensure_started())
        status = self.bridge.status()
        self.assertEqual(self.selected_version, status["sdk"]["version"])
        self.assertTrue(status["sdk"]["compatible"])
        self.assertFalse(status["sdk"]["recommended"])
        self.assertTrue(status["sdk"]["selected"])

        turn = await self.bridge.open_turn(
            "selected-session",
            {"content": [{"type": "text", "text": "hello"}], "cwd": self.temp_dir.name},
        )
        envelopes = [item async for item in turn.events()]
        self.assertEqual("done", envelopes[-1]["type"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for plugin runtime tests')
class AgentSdkPluginRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sdk_dir = Path(self.temp_dir.name) / 'sdk'
        self.sdk_dir.mkdir()
        (self.sdk_dir / 'package.json').write_text('{"name":"@anthropic-ai/claude-agent-sdk","version":"0.2.112","type":"module","exports":"./sdk.mjs"}')
        (self.sdk_dir / 'sdk.mjs').write_text(textwrap.dedent('''
            let generation = 0;
            export function query({prompt, options}) {
              const current = ++generation;
              return {
                async *[Symbol.asyncIterator]() {
                  for await (const message of prompt) {
                    const decision = await options.hooks.PreToolUse[0].hooks[0]({tool_name:'Bash',tool_input:{command:'pwd'}});
                    yield {type:'assistant',message:{content:[{type:'text',text:JSON.stringify({generation:current,plugins:options.plugins||[],marker:options.env?.CCW_TEST_MARKER,decision})}]}};
                    yield {type:'result',subtype:'success',is_error:false,session_id:'plugin-native'};
                  }
                },
                async setModel() {}, async setPermissionMode() {}, async interrupt() {},
                close() {},
              };
            }
            export async function forkSession() { return {sessionId:'forked'}; }
            export async function getSessionMessages() { return []; }
        '''))
        self.env = patch.dict(os.environ, {'CLAUDE_AGENT_SDK_PATH':str(self.sdk_dir),'CLAUDE_WEB_CODE_RUNTIME':'agent-sdk'})
        self.env.start()
        self.bridge = AgentSdkBridge()

    async def asyncTearDown(self):
        await self.bridge.shutdown()
        self.env.stop()
        self.temp_dir.cleanup()

    async def send(self, plugins, **extra):
        turn=await self.bridge.open_turn('plugin-session',{'message':'test','cwd':self.temp_dir.name,'sessionId':'plugin-native','plugins':plugins,**extra})
        frames=[item async for item in turn.events()]
        assistant=next(item['event'] for item in frames if item.get('event',{}).get('type')=='assistant')
        return json.loads(assistant['message']['content'][0]['text'])

    async def test_plugins_and_provider_environment_reach_sdk_with_readonly_policy(self):
        plugins=[{'type':'local','path':self.temp_dir.name+'/plugin'}]
        result=await self.send(plugins,providerEnv={'CCW_TEST_MARKER':'isolated-provider'},providerSignature='provider-test',readOnly=True,permissionMode='bypassPermissions')
        self.assertEqual(plugins,result['plugins'])
        self.assertEqual('isolated-provider',result['marker'])
        self.assertEqual('deny',result['decision']['hookSpecificOutput']['permissionDecision'])

    async def test_changing_extensions_rebuilds_idle_query(self):
        first=await self.send([])
        second=await self.send([{'type':'local','path':self.temp_dir.name+'/plugin'}])
        self.assertGreater(second['generation'],first['generation'])
