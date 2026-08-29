"""Regression tests for runtime MCP injection.

The bug: encrypted connectors stored a `cwsecret://` ref in .mcp.json, but the
turn request never resolved and injected them, so the daemon (reading .mcp.json
via settingSources) received refs the CLI cannot decrypt — encrypted connectors
silently failed to connect during real turns.

The fix under test:
  - _sdk_mcp_config(cfg) shapes one stored config into the SDK mcpServers entry
    with secrets decrypted (headers, env, AND cli args).
  - _runtime_mcp_servers(cwd) collects enabled servers, decrypting each, so the
    turn can inject plaintext-just-for-this-run.
"""

import tempfile
import unittest
from pathlib import Path

import claude_web.server as server
from claude_web.secret_store import SecretStore, is_secret_ref


class RuntimeMcpInjectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # Hermetic file-key store; never touches the real keychain or db.
        self._store = SecretStore(
            db_path=root / "secrets.db",
            key_file=root / ".cw_connector_key",
            use_keychain=False,
        )
        self._orig_store = server._connector_secret_store
        server._connector_secret_store = self._store

    def tearDown(self):
        server._connector_secret_store = self._orig_store
        self._tmp.cleanup()

    def test_http_header_ref_is_decrypted_for_runtime(self):
        ref = self._store.store_secret("Bearer sk-live-123", label="test")
        self.assertTrue(is_secret_ref(ref))
        cfg = {"type": "http", "url": "https://example.com/mcp",
               "headers": {"Authorization": ref}}
        entry = server._sdk_mcp_config(cfg)
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "https://example.com/mcp")
        # The wire value must be the decrypted plaintext, never the ref.
        self.assertEqual(entry["headers"]["Authorization"], "Bearer sk-live-123")
        self.assertFalse(is_secret_ref(entry["headers"]["Authorization"]))

    def test_stdio_env_ref_is_decrypted_for_runtime(self):
        ref = self._store.store_secret("ghp_secret", label="test")
        cfg = {"type": "stdio", "command": "npx",
               "args": ["-y", "@zereight/mcp-gitlab"],
               "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": ref}}
        entry = server._sdk_mcp_config(cfg)
        self.assertEqual(entry["command"], "npx")
        self.assertEqual(entry["env"]["GITLAB_PERSONAL_ACCESS_TOKEN"], "ghp_secret")

    def test_stdio_arg_ref_is_decrypted_for_runtime(self):
        # Feishu-style: the secret is a CLI arg (-s <secret>), not an env var.
        ref = self._store.store_secret("app-secret-xyz", label="test")
        cfg = {"type": "stdio", "command": "npx",
               "args": ["-y", "@larksuiteoapi/lark-mcp", "mcp", "-a", "cli_app", "-s", ref]}
        entry = server._sdk_mcp_config(cfg)
        self.assertEqual(entry["args"][-1], "app-secret-xyz")
        self.assertFalse(any(is_secret_ref(a) for a in entry["args"]))

    def test_plaintext_config_passes_through_untouched(self):
        cfg = {"type": "stdio", "command": "uvx", "args": ["akshare-one-mcp"]}
        entry = server._sdk_mcp_config(cfg)
        self.assertEqual(entry, {"type": "stdio", "command": "uvx",
                                 "args": ["akshare-one-mcp"]})

    def test_unusable_config_returns_none(self):
        self.assertIsNone(server._sdk_mcp_config({"type": "http", "url": ""}))
        self.assertIsNone(server._sdk_mcp_config({"type": "stdio", "command": ""}))


if __name__ == "__main__":
    unittest.main()
