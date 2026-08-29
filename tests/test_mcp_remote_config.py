"""Tests for remote (HTTP/SSE) MCP server configuration support.

The MCP write path used to be stdio-only, so remote connectors — which is what
most marketplace-style connectors are — could not be added from the UI at all.
These tests cover building a config from a request, validation, and the catalog
endpoint that backs the connector grid.
"""

import unittest

from fastapi import HTTPException

from claude_web import server


class RemoteMcpConfigTest(unittest.TestCase):
    def test_http_request_builds_an_http_config(self):
        req = server.McpServerRequest(type="http", url="https://mcp.example.com/mcp")
        cfg = server._mcp_config_from_request(req)
        self.assertEqual(cfg["type"], "http")
        self.assertEqual(cfg["url"], "https://mcp.example.com/mcp")
        self.assertNotIn("command", cfg)

    def test_sse_request_builds_an_sse_config(self):
        req = server.McpServerRequest(type="sse", url="https://mcp.example.com/sse")
        cfg = server._mcp_config_from_request(req)
        self.assertEqual(cfg["type"], "sse")
        self.assertEqual(cfg["url"], "https://mcp.example.com/sse")

    def test_headers_are_preserved_for_remote_servers(self):
        req = server.McpServerRequest(
            type="http",
            url="https://mcp.example.com/mcp",
            headers={"Authorization": "Bearer abc"},
        )
        cfg = server._mcp_config_from_request(req)
        self.assertEqual(cfg["headers"], {"Authorization": "Bearer abc"})

    def test_stdio_still_builds_a_stdio_config(self):
        req = server.McpServerRequest(command="npx", args=["-y", "pkg"], env={"K": "v"})
        cfg = server._mcp_config_from_request(req)
        self.assertEqual(cfg["type"], "stdio")
        self.assertEqual(cfg["command"], "npx")
        self.assertEqual(cfg["args"], ["-y", "pkg"])
        self.assertEqual(cfg["env"], {"K": "v"})
        self.assertNotIn("url", cfg)

    def test_remote_without_url_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server._mcp_config_from_request(server.McpServerRequest(type="http"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_remote_with_non_http_scheme_is_rejected(self):
        # Guard against file:// / javascript: style values reaching the SDK.
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "notaurl"):
            with self.assertRaises(HTTPException) as ctx:
                server._mcp_config_from_request(
                    server.McpServerRequest(type="http", url=bad)
                )
            self.assertEqual(ctx.exception.status_code, 400, bad)

    def test_stdio_without_command_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server._mcp_config_from_request(server.McpServerRequest(type="stdio"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_unknown_transport_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server._mcp_config_from_request(
                server.McpServerRequest(type="telepathy", url="https://x/y")
            )
        self.assertEqual(ctx.exception.status_code, 400)


class McpCatalogEndpointTest(unittest.TestCase):
    def test_catalog_loader_returns_connectors(self):
        catalog = server._load_mcp_catalog()
        self.assertIn("connectors", catalog)
        self.assertGreaterEqual(len(catalog["connectors"]), 6)
        first = catalog["connectors"][0]
        for field in ("id", "name", "category", "transport", "auth"):
            self.assertIn(field, first)

    def test_catalog_entries_never_leak_a_stored_secret_value(self):
        # The catalog declares which secret fields to collect, never values.
        for entry in server._load_mcp_catalog()["connectors"]:
            for field in entry.get("secret_fields") or []:
                self.assertNotIn("value", field)


class PersistRequestSecretsTest(unittest.TestCase):
    """Flagged plaintext secrets in a config must be swapped for cwsecret://
    refs (and stored encrypted) before the config is written to disk."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from claude_web.secret_store import SecretStore

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SecretStore(
            db_path=root / "s.db", key_file=root / ".k", use_keychain=False
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_flagged_header_value_becomes_a_ref_and_is_recoverable(self):
        cfg = {"type": "http", "url": "https://x/y", "headers": {"Authorization": "Bearer raw-secret"}}
        out = server._persist_config_secrets(
            cfg, encrypt_secrets=["Authorization"], store=self.store
        )
        stored_value = out["headers"]["Authorization"]
        self.assertTrue(stored_value.startswith("cwsecret://"))
        self.assertEqual(self.store.resolve_secret(stored_value), "Bearer raw-secret")

    def test_unflagged_values_stay_plaintext(self):
        cfg = {"type": "http", "url": "https://x/y", "headers": {"X-Plain": "keep"}}
        out = server._persist_config_secrets(cfg, encrypt_secrets=[], store=self.store)
        self.assertEqual(out["headers"]["X-Plain"], "keep")

    def test_already_a_ref_is_left_untouched(self):
        cfg = {"type": "http", "url": "https://x/y", "headers": {"Authorization": "cwsecret://existing"}}
        out = server._persist_config_secrets(
            cfg, encrypt_secrets=["Authorization"], store=self.store
        )
        self.assertEqual(out["headers"]["Authorization"], "cwsecret://existing")

    def test_env_secrets_are_encrypted_too(self):
        cfg = {"type": "stdio", "command": "npx", "env": {"TOKEN": "abc123"}}
        out = server._persist_config_secrets(cfg, encrypt_secrets=["TOKEN"], store=self.store)
        self.assertTrue(out["env"]["TOKEN"].startswith("cwsecret://"))
        self.assertEqual(self.store.resolve_secret(out["env"]["TOKEN"]), "abc123")


if __name__ == "__main__":
    unittest.main()
