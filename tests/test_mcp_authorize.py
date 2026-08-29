"""Tests for connector authorization status + the register/authorize helper.

OAuth-based remote connectors are authorized by the `claude` CLI, which stores
tokens where the Agent SDK reads them. A non-TTY web backend cannot reliably
drive the interactive browser callback, so the endpoint registers the server
and reports whether it still needs auth, guiding the user to finish in a
terminal when required — rather than blocking on a flow that would hang.
"""

import unittest
from unittest.mock import patch

from claude_web import server


class ConnectorAuthStatusTest(unittest.TestCase):
    def test_needs_auth_status_is_surfaced_from_probe(self):
        # A 401/403 from a remote server maps to needs-auth, which the UI badges
        # as "待授权" rather than a hard error.
        import asyncio

        class _Resp:
            status_code = 401
            headers = {}
            text = ""

            def raise_for_status(self):
                raise AssertionError("should not reach raise_for_status on 401")

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        with patch("httpx.AsyncClient", return_value=_Client()):
            result = asyncio.run(
                server._probe_remote_mcp_server({"type": "http", "url": "https://x/y"}, {})
            )
        self.assertEqual(result["status"], "needs-auth")
        self.assertFalse(result["ok"])

    def test_authorize_argv_uses_transport_and_url(self):
        argv = server._mcp_authorize_argv("sentry", "https://mcp.sentry.dev/mcp", "http", scope="user")
        self.assertIn("mcp", argv)
        self.assertIn("add", argv)
        self.assertIn("--transport", argv)
        self.assertIn("http", argv)
        self.assertIn("sentry", argv)
        self.assertIn("https://mcp.sentry.dev/mcp", argv)
        self.assertIn("--scope", argv)
        self.assertIn("user", argv)


if __name__ == "__main__":
    unittest.main()
