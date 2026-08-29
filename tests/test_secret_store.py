"""Tests for the connector secret store: local encrypted storage for
third-party API keys / bearer tokens used by remote MCP connectors.

Design under test (claude_web/secret_store.py):
  - Symmetric encryption (Fernet) of user-supplied connector secrets.
  - Master key resolved from OS keychain when available, else a 0600 file.
  - Ciphertext persisted in SQLite; plaintext never written to disk.
  - Values are referenced by an opaque `cwsecret://<uuid>` ref that is what
    lands in .mcp.json, so the cleartext key is never in the MCP config.
"""

import os
import tempfile
import unittest
from pathlib import Path

from claude_web.secret_store import (
    SecretRefError,
    SecretStore,
    is_secret_ref,
    parse_secret_ref,
)


class SecretStoreRoundTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # Force the file-key fallback so tests never touch the real OS keychain
        # and stay hermetic on headless CI.
        self.store = SecretStore(
            db_path=root / "secrets.db",
            key_file=root / ".cw_connector_key",
            use_keychain=False,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_store_then_resolve_round_trips_the_plaintext(self):
        # Not a real credential: a fixed dummy string so the round trip is exact.
        dummy_value = "DUMMY-round-trip-value-0001"
        ref = self.store.store_secret(dummy_value)
        self.assertTrue(is_secret_ref(ref))
        self.assertEqual(self.store.resolve_secret(ref), dummy_value)

    def test_ciphertext_on_disk_is_not_the_plaintext(self):
        dummy_value = "DUMMY-plaintext-must-not-appear"
        ref = self.store.store_secret(dummy_value)
        raw = (Path(self._tmp.name) / "secrets.db").read_bytes()
        self.assertNotIn(dummy_value.encode(), raw)
        self.assertEqual(self.store.resolve_secret(ref), dummy_value)

    def test_key_file_is_created_with_owner_only_permissions(self):
        self.store.store_secret("x")
        key_file = Path(self._tmp.name) / ".cw_connector_key"
        self.assertTrue(key_file.exists())
        if os.name == "posix":
            mode = key_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_delete_removes_the_secret(self):
        ref = self.store.store_secret("to-be-deleted")
        self.assertTrue(self.store.delete_secret(ref))
        with self.assertRaises(SecretRefError):
            self.store.resolve_secret(ref)
        # Deleting an unknown ref is a no-op returning False, not an error.
        self.assertFalse(self.store.delete_secret(ref))

    def test_a_reopened_store_decrypts_prior_secrets(self):
        ref = self.store.store_secret("persist-me")
        reopened = SecretStore(
            db_path=Path(self._tmp.name) / "secrets.db",
            key_file=Path(self._tmp.name) / ".cw_connector_key",
            use_keychain=False,
        )
        self.assertEqual(reopened.resolve_secret(ref), "persist-me")


class SecretRefParsingTest(unittest.TestCase):
    def test_is_secret_ref_only_matches_the_scheme(self):
        self.assertTrue(is_secret_ref("cwsecret://abc123"))
        self.assertFalse(is_secret_ref("ghp_plain_token"))
        self.assertFalse(is_secret_ref(""))
        self.assertFalse(is_secret_ref(None))

    def test_parse_secret_ref_returns_the_id(self):
        self.assertEqual(parse_secret_ref("cwsecret://abc123"), "abc123")

    def test_parse_secret_ref_rejects_non_refs(self):
        with self.assertRaises(SecretRefError):
            parse_secret_ref("not-a-ref")


class SecretResolveMappingTest(unittest.TestCase):
    """resolve_mapping decrypts any cwsecret:// values in a headers/env dict,
    leaving plain values untouched — this is what the runtime injector uses."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = SecretStore(
            db_path=root / "secrets.db",
            key_file=root / ".cw_connector_key",
            use_keychain=False,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolve_mapping_decrypts_only_refs(self):
        ref = self.store.store_secret("Bearer secret-xyz")
        resolved = self.store.resolve_mapping(
            {"Authorization": ref, "X-Plain": "keep-me"}
        )
        self.assertEqual(
            resolved, {"Authorization": "Bearer secret-xyz", "X-Plain": "keep-me"}
        )

    def test_resolve_mapping_handles_none_and_empty(self):
        self.assertEqual(self.store.resolve_mapping(None), {})
        self.assertEqual(self.store.resolve_mapping({}), {})


if __name__ == "__main__":
    unittest.main()
