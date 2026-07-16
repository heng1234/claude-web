"""Pinned, app-owned Claude Agent SDK installation metadata and installer."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional


PACKAGE_NAME = "@anthropic-ai/claude-agent-sdk"
BRIDGE_DIR = Path(__file__).with_name("agent_bridge")
BRIDGE_PACKAGE_JSON = BRIDGE_DIR / "package.json"
BRIDGE_PACKAGE_LOCK = BRIDGE_DIR / "package-lock.json"
DEFAULT_INSTALL_ROOT = Path.home() / ".claude-web" / "dependencies" / "claude-sdk"


class AgentSdkInstallError(RuntimeError):
    pass


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
        "legacy_migration": Path.home() / ".codemoss" / "dependencies" / "claude-sdk",
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
    active_sdk = active_sdk or {}
    active_version = str(active_sdk.get("version") or "") or None
    active_path = str(active_sdk.get("path") or "") or None
    active_source = classify_sdk_path(active_path) if active_path else None
    npm = shutil.which("npm")
    node = os.environ.get("CLAUDE_WEB_NODE") or shutil.which("node")
    detected_node_version = node_version(node)
    return {
        "package": PACKAGE_NAME,
        "required_version": required,
        "install_root": str(root),
        "installed_version": installed,
        "installed": bool(installed),
        "installed_compatible": installed == required,
        "active_version": active_version,
        "active_path": active_path,
        "active_source": active_source,
        "active_compatible": active_version == required if active_version else False,
        "running": bool(running),
        "node_available": bool(node),
        "node_path": node,
        "node_version": detected_node_version,
        "node_compatible": node_version_compatible(detected_node_version),
        "npm_available": bool(npm),
        "npm_path": npm,
        "error": error or None,
        "migration_compatibility": active_source == "legacy_migration",
        "upgrade_policy": "pinned",
        "auto_upgrade": False,
    }


async def install_pinned(timeout: float = 300.0) -> dict:
    """Install the exact locked SDK into a temporary prefix.

    The caller owns activation. Keeping npm away from the live prefix prevents
    a failed/interrupted install from corrupting the currently usable runtime.
    """

    version = required_version()
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
    shutil.copy2(BRIDGE_PACKAGE_JSON, staging / "package.json")
    shutil.copy2(BRIDGE_PACKAGE_LOCK, staging / "package-lock.json")
    command = [
        npm,
        "ci",
        "--prefix",
        str(staging),
        "--no-audit",
        "--no-fund",
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
        return {"staging": staging, "version": actual, "output": output[-4000:]}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
