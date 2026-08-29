"""Tests for remote (HTTP) MCP health checks.

The health endpoint used to bail out with "只支持 stdio" for remote servers,
so a connector added from the catalog could never be verified. These tests run
a real local HTTP MCP endpoint (streamable-http style JSON responses) and check
that the probe reports tools, and that secret refs are resolved before use.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claude_web import server


class _McpHandler(BaseHTTPRequestHandler):
    """Minimal MCP-over-HTTP endpoint: responds to initialize and tools/list."""

    captured_headers = []

    def log_message(self, *args):  # silence test output
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        _McpHandler.captured_headers.append(dict(self.headers))
        method = body.get("method")
        if method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake-remote", "version": "1.2.3"},
                },
            }
        elif method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "tools": [
                        {"name": "get_quote", "description": "查询行情"},
                        {"name": "get_fund", "description": "查询基金"},
                    ]
                },
            }
        else:
            # Notifications carry no id and expect no response body.
            self.send_response(202)
            self.end_headers()
            return
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class RemoteMcpHealthTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _McpHandler.captured_headers.clear()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    async def test_http_probe_reports_tools_and_server_info(self):
        result = await server._probe_remote_mcp_server(
            {"type": "http", "url": self.url}, {}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["server_info"]["name"], "fake-remote")
        self.assertEqual([t["name"] for t in result["tools"]], ["get_quote", "get_fund"])

    async def test_probe_sends_configured_headers(self):
        await server._probe_remote_mcp_server(
            {"type": "http", "url": self.url, "headers": {"X-Api-Key": "plain-key"}}, {}
        )
        self.assertTrue(
            any(h.get("X-Api-Key") == "plain-key" for h in _McpHandler.captured_headers),
            "configured header was not sent",
        )

    async def test_probe_uses_resolved_secret_not_the_ref(self):
        # A cwsecret:// ref must be decrypted before the request goes out, and
        # the raw ref must never appear on the wire.
        await server._probe_remote_mcp_server(
            {"type": "http", "url": self.url, "headers": {"Authorization": "cwsecret://xyz"}},
            {"Authorization": "Bearer decrypted-value"},
        )
        sent = [h.get("Authorization") for h in _McpHandler.captured_headers]
        self.assertIn("Bearer decrypted-value", sent)
        self.assertNotIn("cwsecret://xyz", sent)

    async def test_unreachable_remote_returns_error_status(self):
        result = await server._probe_remote_mcp_server(
            {"type": "http", "url": "http://127.0.0.1:1/mcp"}, {}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
