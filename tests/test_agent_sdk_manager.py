import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from claude_web import agent_sdk_manager


class AgentSdkManagerTest(unittest.TestCase):
    def test_activation_rollback_restores_previous_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            root.mkdir()
            (root / "marker").write_text("old", encoding="utf-8")
            staging = Path(temp) / "staging"
            staging.mkdir()
            (staging / "marker").write_text("new", encoding="utf-8")
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                backup = agent_sdk_manager.activate_staging(staging)
                self.assertEqual("new", (root / "marker").read_text(encoding="utf-8"))
                agent_sdk_manager.rollback_activation(backup)
            self.assertEqual("old", (root / "marker").read_text(encoding="utf-8"))

    def test_first_install_rollback_removes_failed_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            staging = Path(temp) / "staging"
            staging.mkdir()
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                backup = agent_sdk_manager.activate_staging(staging)
                self.assertIsNone(backup)
                self.assertTrue(root.exists())
                agent_sdk_manager.rollback_activation(backup)
            self.assertFalse(root.exists())

    def test_node_version_compatibility_requires_node_18_or_newer(self):
        self.assertTrue(agent_sdk_manager.node_version_compatible("18.0.0"))
        self.assertTrue(agent_sdk_manager.node_version_compatible("22.14.0"))
        self.assertFalse(agent_sdk_manager.node_version_compatible("17.9.1"))
        self.assertFalse(agent_sdk_manager.node_version_compatible(None))
        self.assertFalse(agent_sdk_manager.node_version_compatible("unknown"))

    def test_lock_is_exact_and_status_detects_managed_install(self):
        version = agent_sdk_manager.required_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        lock = json.loads(agent_sdk_manager.BRIDGE_PACKAGE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(
            version,
            lock["packages"]["node_modules/@anthropic-ai/claude-agent-sdk"]["version"],
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            package = agent_sdk_manager.installed_package_dir(root)
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                status = agent_sdk_manager.status_payload(
                    {"version": version, "path": str(package)},
                    running=True,
                )
        self.assertTrue(status["installed_compatible"])
        self.assertTrue(status["active_compatible"])
        self.assertEqual("managed", status["active_source"])
        self.assertFalse(status["auto_upgrade"])

    def test_requested_version_validation_and_stable_sorting(self):
        self.assertEqual("0.2.112", agent_sdk_manager.normalize_requested_version("v0.2.112"))
        with self.assertRaises(agent_sdk_manager.AgentSdkInstallError):
            agent_sdk_manager.normalize_requested_version("latest")
        with self.assertRaises(agent_sdk_manager.AgentSdkInstallError):
            agent_sdk_manager.normalize_requested_version("0.2.112 --force")
        self.assertEqual(
            ["1.0.0", "0.10.0", "0.2.112"],
            agent_sdk_manager.parse_version_list(
                ["0.2.112", "0.2.113-beta.1", "1.0.0", "0.10.0", "bad"]
            ),
        )

    def test_status_accepts_an_explicit_managed_selection(self):
        recommended = agent_sdk_manager.required_version()
        selected = "0.2.111" if recommended != "0.2.111" else "0.2.110"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            package = agent_sdk_manager.installed_package_dir(root)
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"version": selected}), encoding="utf-8")
            (root / agent_sdk_manager.SDK_SELECTION_FILE).write_text(
                json.dumps(
                    {
                        "package": agent_sdk_manager.PACKAGE_NAME,
                        "version": selected,
                        "recommendedVersion": recommended,
                        "selectionMode": "custom",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(agent_sdk_manager, "install_root", return_value=root):
                status = agent_sdk_manager.status_payload(
                    {"version": selected, "path": str(package), "compatible": True},
                    running=True,
                )
        self.assertTrue(status["installed_compatible"])
        self.assertFalse(status["installed_recommended"])
        self.assertTrue(status["active_compatible"])
        self.assertFalse(status["active_recommended"])
        self.assertEqual(selected, status["selected_version"])
        self.assertEqual("custom", status["selection_mode"])


class AgentSdkManagerAsyncTest(unittest.IsolatedAsyncioTestCase):
    class FakeProcess:
        def __init__(self, stdout=b"", stderr=b"", returncode=0):
            self._stdout = stdout
            self._stderr = stderr
            self.returncode = returncode
            self.killed = False

        async def communicate(self):
            return self._stdout, self._stderr

        def kill(self):
            self.killed = True

        async def wait(self):
            return self.returncode

    async def test_registry_catalog_filters_and_orders_stable_versions(self):
        process = self.FakeProcess(
            json.dumps(["0.2.110", "0.2.113-beta.1", "0.2.112", "0.3.0"]).encode()
        )
        with patch.object(agent_sdk_manager.shutil, "which", return_value="/usr/bin/npm"), \
                patch.object(agent_sdk_manager, "package_version", return_value="0.2.110"), \
                patch.object(
                    agent_sdk_manager.asyncio,
                    "create_subprocess_exec",
                    AsyncMock(return_value=process),
                ) as spawn:
            catalog = await agent_sdk_manager.version_catalog(force=True)
        self.assertEqual("registry", catalog["source"])
        self.assertEqual("0.3.0", catalog["latest_version"])
        self.assertEqual(["0.3.0", "0.2.112", "0.2.110"], catalog["versions"])
        self.assertIn("--json", spawn.await_args.args)

    async def test_registry_catalog_keeps_the_complete_stable_version_list(self):
        available = [f"0.1.{index}" for index in range(75)]
        process = self.FakeProcess(json.dumps(available).encode())
        with patch.object(agent_sdk_manager.shutil, "which", return_value="/usr/bin/npm"), \
                patch.object(agent_sdk_manager, "package_version", return_value=None), \
                patch.object(
                    agent_sdk_manager.asyncio,
                    "create_subprocess_exec",
                    AsyncMock(return_value=process),
                ):
            catalog = await agent_sdk_manager.version_catalog(force=True)
        self.assertTrue(set(available).issubset(catalog["versions"]))
        self.assertGreaterEqual(len(catalog["versions"]), len(available))

    async def test_custom_version_install_is_exact_and_writes_selection_metadata(self):
        selected = "0.2.111"
        process = self.FakeProcess()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sdk"
            with patch.object(agent_sdk_manager, "install_root", return_value=root), \
                    patch.object(agent_sdk_manager, "node_version", return_value="22.14.0"), \
                    patch.object(agent_sdk_manager.shutil, "which", return_value="/usr/bin/npm"), \
                    patch.object(agent_sdk_manager, "package_version", return_value=selected), \
                    patch.object(
                        agent_sdk_manager.asyncio,
                        "create_subprocess_exec",
                        AsyncMock(return_value=process),
                    ) as spawn:
                result = await agent_sdk_manager.install_version(selected)
            staging = Path(result["staging"])
            try:
                command = spawn.await_args.args
                self.assertIn("install", command)
                self.assertIn(f"{agent_sdk_manager.PACKAGE_NAME}@{selected}", command)
                self.assertIn("--save-exact", command)
                self.assertIn("--ignore-scripts", command)
                metadata = json.loads(
                    (staging / agent_sdk_manager.SDK_SELECTION_FILE).read_text(encoding="utf-8")
                )
                self.assertEqual(selected, metadata["version"])
                self.assertEqual("custom", metadata["selectionMode"])
                self.assertFalse(result["recommended"])
            finally:
                shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
