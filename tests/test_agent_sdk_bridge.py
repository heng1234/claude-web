import asyncio
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web.agent_sdk_bridge import AgentSdkBridge, AgentSdkBridgeError


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


if __name__ == "__main__":
    unittest.main()
