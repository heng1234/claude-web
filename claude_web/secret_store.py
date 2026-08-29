"""Local encrypted storage for connector secrets.

Remote MCP connectors (HTTP/SSE) authenticate with user-supplied third-party
API keys or bearer tokens. Writing those in cleartext into `.mcp.json` would
leave long-lived credentials readable by anything on the machine, so instead we
store an opaque reference in the MCP config and keep the ciphertext here.

    .mcp.json  ->  "headers": {"Authorization": "cwsecret://<uuid>"}
    this store ->  <uuid> : Fernet(master_key, plaintext)

The master key lives in the OS keychain when one is usable (macOS Keychain,
Windows Credential Locker, Linux Secret Service). Headless Linux often has no
Secret Service, so we fall back to a 0600 key file. The fallback is weaker than
a keychain but still far better than cleartext credentials in a project file.

OAuth-based connectors are NOT handled here: the `claude` CLI already performs
those flows and stores their tokens where the Agent SDK reads them, so this
store never sees an OAuth client secret.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

SECRET_REF_SCHEME = "cwsecret://"

_KEYCHAIN_SERVICE = "claude-web-connectors"
_KEYCHAIN_USERNAME = "master-key"


class SecretStoreError(Exception):
    """Base error for the connector secret store."""


class SecretRefError(SecretStoreError):
    """Raised when a value is not a usable `cwsecret://` reference."""


def is_secret_ref(value: Optional[str]) -> bool:
    """True when `value` is a `cwsecret://` reference rather than a raw secret."""
    return isinstance(value, str) and value.startswith(SECRET_REF_SCHEME)


def parse_secret_ref(value: str) -> str:
    """Return the opaque id inside a `cwsecret://<id>` reference."""
    if not is_secret_ref(value):
        raise SecretRefError(f"not a connector secret ref: {value!r}")
    secret_id = value[len(SECRET_REF_SCHEME):].strip()
    if not secret_id:
        raise SecretRefError("connector secret ref is missing its id")
    return secret_id


def _load_keychain_key() -> Optional[bytes]:
    """Read the master key from the OS keychain, or None when unavailable.

    Any keyring failure (no backend, locked keyring, permission denial) is
    treated as "unavailable" so callers transparently use the file fallback.
    """
    try:
        import keyring
    except Exception:
        return None
    try:
        stored = keyring.get_password(_KEYCHAIN_SERVICE, _KEYCHAIN_USERNAME)
    except Exception:
        return None
    if not stored:
        return None
    try:
        return base64.urlsafe_b64decode(stored.encode("ascii"))
    except Exception:
        return None


def _save_keychain_key(key: bytes) -> bool:
    try:
        import keyring
    except Exception:
        return False
    try:
        keyring.set_password(
            _KEYCHAIN_SERVICE,
            _KEYCHAIN_USERNAME,
            base64.urlsafe_b64encode(key).decode("ascii"),
        )
        return True
    except Exception:
        return False


class SecretStore:
    """Encrypts connector secrets at rest and resolves them on demand."""

    def __init__(
        self,
        db_path: Path,
        key_file: Path,
        use_keychain: bool = True,
    ) -> None:
        self._db_path = Path(db_path)
        self._key_file = Path(key_file)
        self._use_keychain = use_keychain
        self._fernet: Optional[Fernet] = None

    # ----- master key -------------------------------------------------

    def _read_key_file(self) -> Optional[bytes]:
        if not self._key_file.exists():
            return None
        try:
            raw = self._key_file.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise SecretStoreError(f"cannot read connector key file: {exc}") from exc
        if not raw:
            return None
        try:
            return base64.urlsafe_b64decode(raw.encode("ascii"))
        except Exception as exc:
            raise SecretStoreError("connector key file is corrupt") from exc

    def _write_key_file(self, key: bytes) -> None:
        encoded = base64.urlsafe_b64encode(key).decode("ascii")
        try:
            self._key_file.parent.mkdir(parents=True, exist_ok=True)
            # Create with 0600 from the start so the key is never briefly
            # world-readable between write and chmod.
            fd = os.open(self._key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, encoded.encode("ascii"))
            finally:
                os.close(fd)
            if os.name == "posix":
                os.chmod(self._key_file, 0o600)
        except OSError as exc:
            raise SecretStoreError(f"cannot write connector key file: {exc}") from exc

    def _master_key(self) -> bytes:
        if self._use_keychain:
            existing = _load_keychain_key()
            if existing:
                return existing
        existing = self._read_key_file()
        if existing:
            return existing
        key = Fernet.generate_key()
        # Prefer the keychain; only fall back to a file when it is unusable so
        # the secret material stays out of the filesystem where possible.
        if not (self._use_keychain and _save_keychain_key(key)):
            self._write_key_file(key)
        return key

    def _cipher(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._master_key())
        return self._fernet

    def key_location(self) -> str:
        """Human-readable description of where the master key lives (for UI)."""
        if self._use_keychain and _load_keychain_key():
            return "os-keychain"
        return f"file:{self._key_file}"

    # ----- storage ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_secrets (
                id TEXT PRIMARY KEY,
                ciphertext BLOB NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        return conn

    def store_secret(self, plaintext: str, label: str = "") -> str:
        """Encrypt `plaintext` and return the `cwsecret://` ref to store in config."""
        if not isinstance(plaintext, str) or not plaintext:
            raise SecretStoreError("connector secret must be a non-empty string")
        secret_id = uuid.uuid4().hex
        ciphertext = self._cipher().encrypt(plaintext.encode("utf-8"))
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO connector_secrets (id, ciphertext, label, created_at)"
                " VALUES (?, ?, ?, ?)",
                (secret_id, ciphertext, str(label or ""), time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        return f"{SECRET_REF_SCHEME}{secret_id}"

    def resolve_secret(self, ref: str) -> str:
        """Decrypt the secret behind a `cwsecret://` ref."""
        secret_id = parse_secret_ref(ref)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT ciphertext FROM connector_secrets WHERE id = ?", (secret_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise SecretRefError(f"connector secret not found: {ref}")
        try:
            return self._cipher().decrypt(row["ciphertext"]).decode("utf-8")
        except InvalidToken as exc:
            # Wrong/rotated master key, or tampered ciphertext.
            raise SecretStoreError(
                f"cannot decrypt connector secret {ref}; the master key may have changed"
            ) from exc

    def delete_secret(self, ref: str) -> bool:
        """Delete a stored secret. Returns False when the ref is unknown."""
        secret_id = parse_secret_ref(ref)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM connector_secrets WHERE id = ?", (secret_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def resolve_mapping(self, values: Optional[Dict[str, str]]) -> Dict[str, str]:
        """Decrypt any `cwsecret://` values in a headers/env mapping.

        Plain values pass through untouched, so a connector can mix a stored
        bearer token with ordinary non-secret headers.
        """
        if not values:
            return {}
        resolved: Dict[str, str] = {}
        for key, value in values.items():
            text = "" if value is None else str(value)
            resolved[str(key)] = self.resolve_secret(text) if is_secret_ref(text) else text
        return resolved
