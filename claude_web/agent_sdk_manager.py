"""Pinned, app-owned Claude Agent SDK installation metadata and installer."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional


PACKAGE_NAME = "@anthropic-ai/claude-agent-sdk"
BRIDGE_DIR = Path(__file__).with_name("agent_bridge")
BRIDGE_PACKAGE_JSON = BRIDGE_DIR / "package.json"
BRIDGE_PACKAGE_LOCK = BRIDGE_DIR / "package-lock.json"
DEFAULT_INSTALL_ROOT = Path.home() / ".claude-web" / "dependencies" / "claude-sdk"
SDK_SELECTION_FILE = ".claude-web-sdk.json"
VERSION_CACHE_TTL_SECONDS = 10 * 60
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

_version_catalog_cache: Optional[dict] = None
_version_catalog_cached_at = 0.0


class AgentSdkInstallError(RuntimeError):
    pass


def normalize_requested_version(value: object) -> str:
    version = str(value or "").strip()
    if version[:1].lower() == "v":
        version = version[1:]
    if not version or not SEMVER_PATTERN.fullmatch(version) or "-" in version or "+" in version:
        raise AgentSdkInstallError(f"invalid Claude Agent SDK version: {value!r}")
    return version


def _version_sort_key(value: str) -> tuple:
    match = SEMVER_PATTERN.fullmatch(value)
    if not match:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups()[:3])


def parse_version_list(payload: object) -> List[str]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, list):
        payload = [payload]
    versions = set()
    for item in payload:
        try:
            version = normalize_requested_version(item)
        except AgentSdkInstallError:
            continue
        versions.add(version)
    return sorted(versions, key=_version_sort_key, reverse=True)


def required_version() -> str:
    try:
        payload = json.loads(BRIDGE_PACKAGE_JSON.read_text(encoding="utf-8"))
        value = str((payload.get("dependencies") or {}).get(PACKAGE_NAME) or "").strip()
    except (OSError, ValueError, TypeError) as exc:
        raise AgentSdkInstallError(f"cannot read Agent SDK lock: {exc}") from exc
    if not value or any(marker in value for marker in ("^", "~", "*", ">", "<", "||", " ")):
        raise AgentSdkInstallError(f"Agent SDK dependency must be an exact version, got {value!r}")
    try:
        lock = json.loads(BRIDGE_PACKAGE_LOCK.read_text(encoding="utf-8"))
        locked = str(
            (((lock.get("packages") or {}).get("node_modules/@anthropic-ai/claude-agent-sdk") or {}).get("version"))
            or ""
        ).strip()
    except (OSError, ValueError, TypeError) as exc:
        raise AgentSdkInstallError(f"cannot read Agent SDK package-lock: {exc}") from exc
    if locked != value:
        raise AgentSdkInstallError(f"Agent SDK package-lock has {locked or 'no version'}, expected {value}")
    return value


def selection_metadata(root: Optional[Path] = None) -> Optional[dict]:
    prefix = root or install_root()
    try:
        payload = json.loads((prefix / SDK_SELECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if payload.get("package") != PACKAGE_NAME:
        return None
    try:
        selected = normalize_requested_version(payload.get("version"))
    except AgentSdkInstallError:
        return None
    installed = package_version(installed_package_dir(prefix))
    if installed != selected:
        return None
    return {**payload, "version": selected}


def _write_selection_metadata(root: Path, version: str) -> None:
    recommended = required_version()
    payload = {
        "package": PACKAGE_NAME,
        "version": version,
        "recommendedVersion": recommended,
        "selectionMode": "recommended" if version == recommended else "custom",
        "installedAt": int(time.time()),
    }
    (root / SDK_SELECTION_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def install_root() -> Path:
    configured = os.environ.get("CLAUDE_WEB_AGENT_SDK_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_INSTALL_ROOT.expanduser().resolve()


def installed_package_dir(root: Optional[Path] = None) -> Path:
    return (root or install_root()) / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"


def package_version(package_dir: Path) -> Optional[str]:
    try:
        payload = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
        value = str(payload.get("version") or "").strip()
        return value or None
    except (OSError, ValueError, TypeError):
        return None


def node_version(node: Optional[str] = None) -> Optional[str]:
    executable = node or os.environ.get("CLAUDE_WEB_NODE") or shutil.which("node")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or result.stderr or "").strip().lstrip("v")
    return value or None


def node_version_compatible(value: Optional[str]) -> bool:
    try:
        return int(str(value or "").split(".", 1)[0]) >= 18
    except (TypeError, ValueError):
        return False


def classify_sdk_path(value: object) -> str:
    try:
        path = Path(str(value or "")).expanduser().resolve()
    except (OSError, ValueError):
        return "unknown"
    roots = {
        "managed": install_root(),
        "bundled": BRIDGE_DIR,
        "ccgui_migration": Path.home() / ".codemoss" / "dependencies" / "claude-sdk",
    }
    for label, root in roots.items():
        try:
            path.relative_to(root.expanduser().resolve())
            return label
        except ValueError:
            continue
    configured = os.environ.get("CLAUDE_AGENT_SDK_PATH", "").strip()
    if configured:
        try:
            path.relative_to(Path(configured).expanduser().resolve())
            return "environment_override"
        except ValueError:
            pass
    return "external"


def status_payload(active_sdk: Optional[dict] = None, *, running: bool = False, error: str = "") -> dict:
    required = required_version()
    root = install_root()
    installed = package_version(installed_package_dir(root))
    selection = selection_metadata(root)
    selected = str((selection or {}).get("version") or "") or (required if installed == required else None)
    active_sdk = active_sdk or {}
    active_version = str(active_sdk.get("version") or "") or None
    active_path = str(active_sdk.get("path") or "") or None
    active_source = classify_sdk_path(active_path) if active_path else None
    active_compatible = bool(active_sdk.get("compatible")) if active_sdk else False
    if active_sdk and "compatible" not in active_sdk:
        active_compatible = active_version == required
    npm = shutil.which("npm")
    node = os.environ.get("CLAUDE_WEB_NODE") or shutil.which("node")
    detected_node_version = node_version(node)
    return {
        "package": PACKAGE_NAME,
        "required_version": required,
        "install_root": str(root),
        "installed_version": installed,
        "installed": bool(installed),
        "installed_compatible": bool(installed and selected == installed),
        "installed_recommended": installed == required,
        "selected_version": selected,
        "selection_mode": (selection or {}).get("selectionMode") or ("recommended" if selected == required else None),
        "active_version": active_version,
        "active_path": active_path,
        "active_source": active_source,
        "active_compatible": active_compatible,
        "active_recommended": active_version == required if active_version else False,
        "running": bool(running),
        "node_available": bool(node),
        "node_path": node,
        "node_version": detected_node_version,
        "node_compatible": node_version_compatible(detected_node_version),
        "npm_available": bool(npm),
        "npm_path": npm,
        "error": error or None,
        "migration_compatibility": active_source == "ccgui_migration",
        "upgrade_policy": "selectable_pinned",
        "auto_upgrade": False,
    }


async def version_catalog(*, force: bool = False, timeout: float = 30.0) -> dict:
    """Return all stable registry versions with an offline-safe fallback."""

    global _version_catalog_cache, _version_catalog_cached_at
    recommended = required_version()
    installed = package_version(installed_package_dir())
    fallback = parse_version_list([recommended, installed])
    now = time.monotonic()
    if (
        not force
        and _version_catalog_cache is not None
        and now - _version_catalog_cached_at < VERSION_CACHE_TTL_SECONDS
    ):
        return {
            **_version_catalog_cache,
            "versions": parse_version_list([*_version_catalog_cache.get("versions", []), *fallback]),
            "installed_version": installed,
            "cached": True,
        }

    npm = shutil.which("npm")
    if not npm:
        return {
            "package": PACKAGE_NAME,
            "versions": fallback,
            "latest_version": None,
            "recommended_version": recommended,
            "installed_version": installed,
            "source": "fallback",
            "cached": False,
            "error": "npm is not available",
        }

    command = [npm, "view", PACKAGE_NAME, "versions", "--json"]
    env = os.environ.copy()
    env.setdefault("npm_config_update_notifier", "false")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AgentSdkInstallError("npm version lookup timed out") from exc
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise AgentSdkInstallError(message[-1000:] or f"npm exited with {process.returncode}")
        remote_versions = parse_version_list(stdout.decode("utf-8", errors="replace"))
        if not remote_versions:
            raise AgentSdkInstallError("npm returned no stable Claude Agent SDK versions")
        versions = parse_version_list([*remote_versions, *fallback])
        result = {
            "package": PACKAGE_NAME,
            "versions": versions,
            "latest_version": remote_versions[0],
            "recommended_version": recommended,
            "installed_version": installed,
            "source": "registry",
            "cached": False,
            "error": None,
        }
        _version_catalog_cache = result
        _version_catalog_cached_at = now
        return result
    except (AgentSdkInstallError, OSError) as exc:
        return {
            "package": PACKAGE_NAME,
            "versions": fallback,
            "latest_version": None,
            "recommended_version": recommended,
            "installed_version": installed,
            "source": "fallback",
            "cached": False,
            "error": str(exc),
        }


async def install_version(requested_version: object = None, timeout: float = 300.0) -> dict:
    """Install one exact SDK version into a temporary prefix.

    The caller owns activation. Keeping npm away from the live prefix prevents
    a failed/interrupted install from corrupting the currently usable runtime.
    """

    recommended = required_version()
    version = normalize_requested_version(requested_version or recommended)
    detected_node_version = node_version()
    if not node_version_compatible(detected_node_version):
        raise AgentSdkInstallError(
            f"Node.js 18+ is required, found {detected_node_version or 'no usable Node.js'}"
        )
    npm = shutil.which("npm")
    if not npm:
        raise AgentSdkInstallError("npm is required to install the Claude Agent SDK")
    root = install_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = root.parent / f".{root.name}.install-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    if version == recommended:
        shutil.copy2(BRIDGE_PACKAGE_JSON, staging / "package.json")
        shutil.copy2(BRIDGE_PACKAGE_LOCK, staging / "package-lock.json")
        command = [
            npm,
            "ci",
            "--prefix",
            str(staging),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ]
    else:
        (staging / "package.json").write_text(
            json.dumps(
                {
                    "name": "claude-web-agent-sdk-install",
                    "private": True,
                    "dependencies": {PACKAGE_NAME: version},
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        command = [
            npm,
            "install",
            "--prefix",
            str(staging),
            "--save-exact",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            f"{PACKAGE_NAME}@{version}",
        ]
    env = os.environ.copy()
    env.setdefault("npm_config_update_notifier", "false")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AgentSdkInstallError("Agent SDK installation timed out") from exc
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise AgentSdkInstallError(output[-4000:] or f"npm exited with {process.returncode}")
        actual = package_version(installed_package_dir(staging))
        if actual != version:
            raise AgentSdkInstallError(f"npm installed Agent SDK {actual or 'unknown'}, expected {version}")
        _write_selection_metadata(staging, version)
        return {
            "staging": staging,
            "version": actual,
            "recommended": version == recommended,
            "output": output[-4000:],
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


async def install_pinned(timeout: float = 300.0) -> dict:
    """Backward-compatible helper for installing the recommended locked SDK."""

    return await install_version(required_version(), timeout=timeout)


def activate_staging(staging: Path) -> Optional[Path]:
    """Atomically replace the managed prefix and return its rollback path."""

    root = install_root()
    backup = root.parent / f".{root.name}.backup-{uuid.uuid4().hex}"
    if root.exists():
        os.replace(root, backup)
    else:
        backup = None
    try:
        os.replace(staging, root)
    except Exception:
        if backup is not None and backup.exists() and not root.exists():
            os.replace(backup, root)
        raise
    return backup


def rollback_activation(backup: Optional[Path]) -> None:
    root = install_root()
    shutil.rmtree(root, ignore_errors=True)
    if backup is not None and backup.exists():
        os.replace(backup, root)


def discard_backup(backup: Optional[Path]) -> None:
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
