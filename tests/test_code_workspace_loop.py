import asyncio
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException

from claude_web import server


class CodeWorkspaceLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session_id = "code-loop-" + uuid.uuid4().hex
        self.cwd = tempfile.mkdtemp(prefix="claude-web-code-loop-")
        server.upsert_session(self.session_id, "code loop", self.cwd, "code")
        server.set_session_runtime_origin(self.session_id, server._RUNTIME_ORIGIN_AGENT_SDK)

    def tearDown(self):
        server.save_events(self.session_id, [])
        with server.db_connect() as conn:
            conn.execute("DELETE FROM code_message_queue WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM code_turn_requests WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM code_project_settings WHERE cwd = ?", (str(Path(self.cwd).resolve()),))
            conn.execute("DELETE FROM code_permission_rules WHERE session_id = ?", (self.session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))

    async def test_persistent_queue_reorders_and_turn_idempotency_removes_accepted_item(self):
        first = "queue-" + uuid.uuid4().hex
        second = "queue-" + uuid.uuid4().hex
        await server.add_code_message_queue(
            self.session_id,
            server.CodeQueueRequest(id=first, payload={"message": "first"}),
        )
        await server.add_code_message_queue(
            self.session_id,
            server.CodeQueueRequest(id=second, payload={"message": "second"}),
        )
        await server.reorder_code_message_queue(
            self.session_id,
            server.CodeQueueOrderRequest(ids=[second, first]),
        )
        queued = await server.list_code_message_queue(self.session_id)
        self.assertEqual([second, first], [item["id"] for item in queued["items"]])

        reserved = server._reserve_code_turn(self.session_id, second)
        server._mark_code_turn_accepted(self.session_id, reserved)
        queued = await server.list_code_message_queue(self.session_id)
        self.assertEqual([first], [item["id"] for item in queued["items"]])
        with self.assertRaises(HTTPException) as raised:
            server._reserve_code_turn(self.session_id, second)
        self.assertEqual(409, raised.exception.status_code)

    async def test_auto_approval_and_project_validation_settings_are_session_and_project_scoped(self):
        enabled = await server.set_code_session_auto_approve(
            self.session_id,
            server.CodeAutoApproveRequest(enabled=True),
        )
        self.assertTrue(enabled["enabled"])
        with server.db_connect() as conn:
            row = conn.execute("SELECT auto_approve FROM sessions WHERE id = ?", (self.session_id,)).fetchone()
        self.assertEqual(1, row["auto_approve"])

        saved = await server.set_code_project_settings(
            self.session_id,
            server.CodeProjectSettingsRequest(
                validation_command="python -m unittest",
                validation_mode="auto",
                validation_timeout=45,
            ),
        )
        loaded = await server.get_code_project_settings(self.session_id)
        self.assertEqual("auto", saved["validation_mode"])
        self.assertEqual("python -m unittest", loaded["validation_command"])
        self.assertEqual(45, loaded["validation_timeout"])

    def test_history_page_starts_on_user_turn_and_preserves_absolute_offsets(self):
        events = []
        for index in range(5):
            events.extend([
                {"type": "user_input", "text": f"turn {index}"},
                {"type": "assistant", "message": {"content": []}},
                {"type": "result", "duration_ms": index},
            ])
        page = server.session_event_page(events, 2)
        self.assertTrue(page["has_more"])
        self.assertEqual(9, page["event_offset"])
        self.assertEqual(3, page["user_offset"])
        self.assertEqual("turn 3", page["events"][0]["text"])
        self.assertEqual(len(events), page["total_event_count"])

    def test_diff_hunks_are_independently_addressable(self):
        diff = """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1 +1 @@
-old one
+new one
@@ -5 +5 @@
-old two
+new two
"""
        hunks = server._diff_hunk_patches(diff)
        self.assertEqual([0, 1], [hunk["index"] for hunk in hunks])
        self.assertTrue(all(hunk["revertible"] for hunk in hunks))
        self.assertIn("@@ -1 +1 @@", hunks[0]["patch"])
        self.assertNotIn("@@ -5 +5 @@", hunks[0]["patch"])

    async def test_prewrite_diff_preview_is_code_scoped_and_does_not_write(self):
        target = Path(self.cwd) / "demo.txt"
        target.write_text("before\nkeep\n", encoding="utf-8")

        preview = await server.preview_code_tool_change(
            self.session_id,
            server.CodeToolPreviewRequest(
                tool_name="Edit",
                input={"file_path": "demo.txt", "old_string": "before", "new_string": "after"},
            ),
        )

        self.assertTrue(preview["supported"])
        self.assertEqual("before\nkeep\n", preview["old_text"])
        self.assertEqual("after\nkeep\n", preview["new_text"])
        self.assertEqual("before\nkeep\n", target.read_text(encoding="utf-8"))
        with self.assertRaises(HTTPException) as raised:
            await server.preview_code_tool_change(
                self.session_id,
                server.CodeToolPreviewRequest(
                    tool_name="Write",
                    input={"file_path": "../outside.txt", "content": "blocked"},
                ),
            )
        self.assertEqual(400, raised.exception.status_code)

    async def test_code_file_reader_resolves_lines_and_tracks_real_file_updates(self):
        target = Path(self.cwd) / "demo.py"
        target.write_text("first = 1\nsecond = 2\nthird = 3\n", encoding="utf-8")

        payload = await server.read_code_file(self.session_id, "demo.py:2-3", line_start=None, line_end=None)
        self.assertTrue(payload["ok"])
        self.assertEqual("demo.py", payload["path"])
        self.assertEqual("python", payload["language"])
        self.assertEqual((2, 3), (payload["line_start"], payload["line_end"]))
        self.assertIn("second = 2", payload["content"])
        old_etag = payload["etag"]

        target.write_text("first = 1\nsecond = 20\nthird = 3\n", encoding="utf-8")
        stat = await server.stat_code_file(self.session_id, "demo.py")
        self.assertNotEqual(old_etag, stat["etag"])

    async def test_code_file_reader_stays_inside_project_and_reports_ambiguous_names(self):
        (Path(self.cwd) / "src").mkdir()
        (Path(self.cwd) / "tests").mkdir()
        (Path(self.cwd) / "src" / "shared.py").write_text("SOURCE = True\n", encoding="utf-8")
        (Path(self.cwd) / "tests" / "shared.py").write_text("TEST = True\n", encoding="utf-8")

        ambiguous = await server.read_code_file(self.session_id, "shared.py")
        self.assertFalse(ambiguous["ok"])
        self.assertTrue(ambiguous["ambiguous"])
        self.assertEqual(["src/shared.py", "tests/shared.py"], [item["path"] for item in ambiguous["candidates"]])
        with self.assertRaises(HTTPException) as raised:
            await server.read_code_file(self.session_id, "../outside.py")
        self.assertEqual(400, raised.exception.status_code)

    async def test_code_file_reader_marks_binary_files_without_leaking_content(self):
        (Path(self.cwd) / "sample.bin").write_bytes(b"abc\x00secret")
        payload = server._code_file_payload(self.cwd, Path(self.cwd) / "sample.bin", include_content=True)
        self.assertTrue(payload["binary"])
        self.assertEqual("", payload["content"])
        self.assertEqual("binary", payload["encoding"])

    async def test_code_tree_is_lazy_searchable_and_project_scoped(self):
        root = Path(self.cwd)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (root / ".hidden.py").write_text("hidden\n", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "skip.js").write_text("skip\n", encoding="utf-8")

        listing = await server.read_code_tree(self.session_id, path="", q="", show_hidden=False)
        self.assertEqual(["src"], [item["name"] for item in listing["entries"]])
        children = await server.read_code_tree(self.session_id, path="src", q="", show_hidden=False)
        self.assertEqual(["src/main.py"], [item["path"] for item in children["entries"]])
        searched = await server.read_code_tree(self.session_id, path="", q="main", show_hidden=False)
        self.assertEqual(["src/main.py"], [item["path"] for item in searched["entries"]])
        with self.assertRaises(HTTPException) as raised:
            await server.read_code_tree(self.session_id, path="../", q="", show_hidden=False)
        self.assertEqual(400, raised.exception.status_code)

    def test_directory_picker_lists_directories_and_normalizes_selected_path(self):
        root = Path(self.cwd)
        (root / "alpha").mkdir()
        (root / "beta").mkdir()
        (root / ".hidden").mkdir()
        (root / "notes.txt").write_text("not a directory", encoding="utf-8")

        payload = server._directory_picker_payload(str(root))
        self.assertEqual(str(root.resolve()), payload["path"])
        self.assertEqual(["alpha", "beta"], [item["name"] for item in payload["entries"]])
        self.assertTrue(all(Path(item["path"]).is_absolute() for item in payload["entries"]))

        hidden_payload = server._directory_picker_payload(str(root), show_hidden=True)
        self.assertEqual([".hidden", "alpha", "beta"], [item["name"] for item in hidden_payload["entries"]])
        with self.assertRaises(HTTPException) as raised:
            server._directory_picker_payload(str(root / "missing"))
        self.assertEqual(404, raised.exception.status_code)

    @unittest.skipIf(os.name == "nt", "PTY terminal requires POSIX")
    async def test_code_terminal_has_independent_runtime_ownership_and_streams_output(self):
        runtime = await server._spawn_code_terminal(self.session_id, self.cwd, 80, 24)
        queue = asyncio.Queue()
        runtime.listeners.add(queue)
        try:
            self.assertNotIn(self.session_id, server._running_processes)
            self.assertNotIn(self.session_id, server._agent_sdk_running_sessions)
            server._write_code_terminal(runtime, "printf 'PTY_OK\\n'\r")
            output = bytearray()
            for _ in range(12):
                item = await asyncio.wait_for(queue.get(), timeout=2.0)
                if isinstance(item, bytes):
                    output.extend(item)
                if b"PTY_OK" in output:
                    break
            self.assertIn(b"PTY_OK", output)
            payload = server._code_terminal_payload(runtime)
            self.assertEqual(self.session_id, payload["session_id"])
            self.assertEqual(str(Path(self.cwd).resolve()), payload["cwd"])
        finally:
            runtime.listeners.discard(queue)
            await server._terminate_code_terminal(runtime)
            server._code_terminals.pop(runtime.id, None)


class CodeWorkspaceStaticContractTest(unittest.TestCase):
    def test_code_turn_controls_are_owned_by_session_not_shared_globally(self):
        source = (
            Path(__file__).parents[1] / "claude_web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("const codeSessionRuns = new Map()", source)
        self.assertIn("function activeCodeSessionRun()", source)
        self.assertIn("function syncActiveCodeRunControls()", source)
        self.assertIn("session_id: turnSessionId", source)
        self.assertIn("const run = targetSessionId ? codeRunForSession(targetSessionId)", source)
        self.assertIn("if (run?.controller) run.controller.abort()", source)
        self.assertIn("if (turnViewIsActive()) fetchFollowupSuggestions()", source)
        self.assertNotIn(
            "owner === activeCodeQueueOwner && codeMode && !stopBtn.classList.contains('hidden')",
            source,
        )

    def test_sdk_result_errors_and_reconnect_controls_are_not_silently_completed(self):
        source = (
            Path(__file__).parents[1] / "claude_web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("function nativeResultErrorMessage(result)", source)
        self.assertIn("result.is_error === true || subtype.startsWith('error_')", source)
        self.assertIn("Claude Agent SDK 连接在返回最终结果前结束", source)
        self.assertIn("function reconnectCurrentCodeSession(options = {})", source)
        self.assertIn("/runtime/reconnect", source)
        self.assertIn("正在重新连接 ${reconnectAttempt}/${maxReconnectAttempts}", source)
        self.assertIn("当前任务不会自动重复执行", source)

    def test_code_generation_status_persists_until_authoritative_turn_end(self):
        source = (
            Path(__file__).parents[1] / "claude_web" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("el.dataset.codeRunStatus = 'true'", source)
        self.assertIn("function hideThinking(container, options = {})", source)
        self.assertIn("options.force !== true", source)
        self.assertIn("hideThinking(asstContainer, { force: true })", source)
        self.assertIn("hideThinking(container, { force: true })", source)
        self.assertIn("startedAt: turnStartedAt, container", source)
        self.assertIn("container.lastElementChild !== existing", source)
        self.assertIn("if (run?.container && !run.container.querySelector", source)
        self.assertIn("function ensureThinkingElapsedTimer(el, startedAt = Date.now())", source)
        self.assertNotIn("if (!el.isConnected)", source)
        self.assertIn("function nativeApiRetryStatusText(obj)", source)
        self.assertIn("return `${reason}，正在重试", source)
        self.assertIn("nativeTurnRecoveryRunningSessionId !== run.sessionId", source)

    def test_light_context_stays_default_on_and_permission_copy_is_current(self):
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("lightContextMode: LS.get('lightContextMode', true)", source)
        self.assertIn("LIGHT_CONTEXT_DEFAULT_POLICY = 'native-compact-default-on-v1'", source)
        self.assertIn("LS.set('lightContextMode', true)", source)
        self.assertNotIn("Web 版不能在运行中批准权限", source)
        self.assertIn("替我审批", source)
        self.assertIn('id="cwModeAutoApprove"', source)
        self.assertNotIn('id="cwStatsApproval"', source)

    def test_ten_ccgui_borrowed_features_are_code_only_contracts(self):
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        required = [
            'id="cwContextUsageModal"',
            'data-mcp-health=',
            'id="cwConversationSearch"',
            'codeInputHistoryV1',
            '/permissions/preview-change',
            'cw-tool-group',
            'id="cwPermissionRulesModal"',
            'cw-code-diagnostic',
            'id="cwWorkspacePresetsModal"',
        ]
        for marker in required:
            self.assertIn(marker, source)
        server_source = (Path(__file__).parents[1] / "claude_web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"code.question_pending"', server_source)
        self.assertIn('body:not(.code-mode) .cw-code-only', source)
        self.assertIn("if (!codeMode) return el;", source)

    def test_code_inspector_is_code_only_and_uses_project_scoped_file_api(self):
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        for marker in [
            'id="cwCodeInspector"',
            'id="cwOpenCodeFileBtn"',
            'function openCodeFileInspector',
            '/code-file/stat?',
            '/code-file/open-external',
            "body.code-mode.cw-code-inspector-open",
            "if (codeMode) return openCodeDiffInspector",
        ]:
            self.assertIn(marker, source)
        self.assertIn('class="cw-code-inspector cw-code-only"', source)

        server_source = (Path(__file__).parents[1] / "claude_web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('def _resolve_code_file_target', server_source)
        self.assertIn('@app.get("/api/sessions/{session_id}/code-file")', server_source)
        self.assertIn('_require_not_mobile_access(request, "远程设备不能启动电脑上的本机编辑器")', server_source)

    def test_code_tree_and_terminal_are_code_only_inspector_features(self):
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        for marker in [
            'id="cwCodeTree"',
            'id="cwCodeTreeToggle"',
            'id="cwCodeTerminalNew"',
            'function loadCodeTree',
            'function createCodeTerminal',
            '/terminals/${encodeURIComponent',
        ]:
            self.assertIn(marker, source)
        server_source = (Path(__file__).parents[1] / "claude_web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/sessions/{session_id}/code-tree")', server_source)
        self.assertIn('@app.websocket("/api/sessions/{session_id}/terminals/{terminal_id}/ws")', server_source)
        self.assertIn('_terminate_session_code_terminals(session_id)', server_source)

    def test_project_manager_has_server_directory_picker(self):
        source = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
        for marker in [
            'id="cwProjectManagerBrowse"',
            'id="cwDirectoryPicker"',
            'function loadProjectDirectory',
            "'/api/directories?'",
            "'/api/projects/register'",
            '选择此目录',
        ]:
            self.assertIn(marker, source)
        server_source = (Path(__file__).parents[1] / "claude_web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/directories")', server_source)
        self.assertIn('@app.post("/api/projects/register")', server_source)


if __name__ == "__main__":
    unittest.main()
