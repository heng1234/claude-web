import os
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import StreamingResponse

from claude_web import server


class BrowserExtensionModeTest(unittest.TestCase):
    def test_workspace_mode_defaults_to_chat_and_rejects_unknown_values(self):
        self.assertEqual("chat", server._sanitize_extension_workspace_mode(None))
        self.assertEqual("chat", server._sanitize_extension_workspace_mode("workspace"))
        self.assertEqual("code", server._sanitize_extension_workspace_mode(" CODE "))

    def test_legacy_request_with_project_cwd_defaults_to_code(self):
        with tempfile.TemporaryDirectory(prefix="claude-web-extension-") as cwd:
            mode, permission, resolved_cwd, _, disallowed_tools = server._extension_execution_settings(
                workspace_mode=None,
                session_id=None,
                cwd=cwd,
                permission_mode="plan",
            )

        self.assertEqual("code", mode)
        self.assertEqual("plan", permission)
        self.assertEqual(str(server.Path(cwd).resolve()), resolved_cwd)
        self.assertIsNone(disallowed_tools)

    def test_chat_mode_forces_home_and_readonly_tools(self):
        with tempfile.TemporaryDirectory(prefix="claude-web-extension-") as cwd:
            mode, permission, resolved_cwd, cli_permission, disallowed_tools = (
                server._extension_execution_settings(
                    workspace_mode="chat",
                    session_id=None,
                    cwd=cwd,
                    permission_mode="default",
                )
            )

        self.assertEqual("chat", mode)
        self.assertEqual("readonly", permission)
        self.assertEqual(str(server.Path(os.path.expanduser("~")).resolve()), resolved_cwd)
        self.assertIsNone(cli_permission)
        self.assertEqual(server._EXTENSION_READONLY_DISALLOWED_TOOLS, disallowed_tools)

    def test_omitted_mode_uses_existing_code_session(self):
        session_id = "extension-mode-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="claude-web-extension-") as cwd:
            server.upsert_session(session_id, "Code", cwd, "code")
            try:
                mode, permission, resolved_cwd, _, _ = server._extension_execution_settings(
                    workspace_mode=None,
                    session_id=session_id,
                    cwd=None,
                    permission_mode="default",
                )
            finally:
                with server.db_connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

        self.assertEqual("code", mode)
        self.assertEqual("default", permission)
        self.assertEqual(str(server.Path(cwd).resolve()), resolved_cwd)

    def test_existing_session_rejects_cross_mode_takeover(self):
        session_id = "extension-mode-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="claude-web-extension-") as cwd:
            server.upsert_session(session_id, "Code", cwd, "code")
            try:
                with self.assertRaises(HTTPException) as raised:
                    server._resolve_extension_workspace_mode(
                        "chat",
                        session_id=session_id,
                        cwd=None,
                    )
            finally:
                with server.db_connect() as conn:
                    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

        self.assertEqual(409, raised.exception.status_code)

    def test_extension_draft_preserves_explicit_code_mode(self):
        with tempfile.TemporaryDirectory(prefix="claude-web-extension-") as cwd:
            payload = server._draft_payload_from_request(
                server.ExtensionDraftRequest(
                    message="Inspect the selected page",
                    cwd=cwd,
                    workspace_mode="code",
                    permission_mode="plan",
                    auto_run=False,
                )
            )

        self.assertEqual("code", payload["workspace_mode"])
        self.assertEqual("plan", payload["permission_mode"])
        self.assertFalse(payload["auto_run"])

    def test_extension_draft_normalizes_unknown_mode_to_chat(self):
        payload = server._draft_payload_from_request(
            server.ExtensionDraftRequest(
                message="Summarize this page",
                workspace_mode="unknown",
                permission_mode="readonly",
            )
        )

        self.assertEqual("chat", payload["workspace_mode"])
        self.assertEqual("readonly", payload["permission_mode"])


class BrowserExtensionSourceContractTest(unittest.TestCase):
    def test_mode_switch_stops_server_turn_before_restoring_other_mode(self):
        root = server.Path(__file__).resolve().parents[1]
        for relative_path in (
            "browser-extension/src/sidepanel.js",
            "claude_web/browser_extension/src/sidepanel.js",
        ):
            source = (root / relative_path).read_text(encoding="utf-8")
            self.assertIn("const stopped = await stopAsk();", source)
            self.assertIn("if (!currentSessionId) currentSessionId = crypto.randomUUID();", source)
            self.assertIn("unresolvedServerSessionId = currentSessionId;", source)
            self.assertIn("if (!response.ok && response.status !== 404)", source)
            self.assertIn("session_id: currentSessionId,", source)
            self.assertIn(
                "await fetch(`${serviceUrl}/api/extension/stop/${encodeURIComponent(sessionId)}`",
                source,
            )

    def test_task_rows_are_noninteractive_without_an_action(self):
        root = server.Path(__file__).resolve().parents[1]
        for relative_path in ("static/index.html", "claude_web/static/index.html"):
            source = (root / relative_path).read_text(encoding="utf-8")
            self.assertIn("const tag = row.key ? 'button' : 'div';", source)
            self.assertIn(".cw-activity-row.is-interactive { cursor:pointer; }", source)
            self.assertNotIn(
                '<button type="button" class="cw-activity-row" ${row.key ?',
                source,
            )


class BrowserExtensionEndpointModeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "server": ("testserver", 80),
                "path": "/api/extension/ask",
                "root_path": "",
                "headers": [],
                "query_string": b"",
            }
        )

    async def test_extension_ask_enforces_chat_isolation_at_endpoint(self):
        async def body():
            yield b"data: {}\n\n"

        with tempfile.TemporaryDirectory(prefix="claude-web-extension-") as project_cwd:
            response = StreamingResponse(body(), media_type="text/event-stream")
            with patch.object(server, "_require_extension_token"), patch.object(
                server,
                "_chat_response",
                AsyncMock(return_value=response),
            ) as chat_response:
                await server.extension_ask(
                    self._request(),
                    server.ExtensionAskRequest(
                        selected_text="Review this page",
                        workspace_mode="chat",
                        cwd=project_cwd,
                        permission_mode="default",
                    ),
                    "token",
                )

        chat_req = chat_response.await_args.args[0]
        self.assertEqual("chat", chat_req.workspace_mode)
        self.assertEqual(str(server.Path(os.path.expanduser("~")).resolve()), chat_req.cwd)
        self.assertIsNone(chat_req.permission_mode)
        self.assertEqual(server._EXTENSION_READONLY_DISALLOWED_TOOLS, chat_req.disallowed_tools)


if __name__ == "__main__":
    unittest.main()
