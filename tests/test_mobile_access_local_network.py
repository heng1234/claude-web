import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("CLAUDE_WEB_DATA_DIR", tempfile.mkdtemp(prefix="claude-web-test-"))

from claude_web import server  # noqa: E402


class MobileAccessLocalNetworkTest(unittest.TestCase):
    def request(self, direct_host, path="/", headers=None):
        return SimpleNamespace(
            client=SimpleNamespace(host=direct_host),
            headers=headers or {},
            url=SimpleNamespace(path=path),
        )

    def test_loopback_clients_do_not_need_access_code(self):
        self.assertFalse(server._mobile_access_auth_required(self.request("127.0.0.1")))
        self.assertFalse(server._mobile_access_auth_required(self.request("::1")))

    def test_rfc1918_lan_clients_do_not_need_access_code(self):
        self.assertFalse(server._mobile_access_auth_required(self.request("192.168.1.82")))
        self.assertFalse(server._mobile_access_auth_required(self.request("10.0.0.8")))
        self.assertFalse(server._mobile_access_auth_required(self.request("172.16.2.9")))

    def test_ipv4_mapped_ipv6_lan_client_does_not_need_access_code(self):
        self.assertFalse(server._mobile_access_auth_required(self.request("::ffff:192.168.1.82")))

    def test_forwarded_lan_client_does_not_need_access_code(self):
        req = self.request("127.0.0.1", headers={"x-forwarded-for": "192.168.1.82"})
        self.assertFalse(server._mobile_access_auth_required(req))

    def test_public_forwarded_client_still_needs_access_code(self):
        req = self.request("127.0.0.1", headers={"x-forwarded-for": "8.8.8.8"})
        self.assertTrue(server._mobile_access_auth_required(req))

    def test_authenticated_remote_chat_keeps_full_tool_permissions(self):
        req = server.ChatRequest(
            message="run the task",
            cwd="/saved/project",
            workspace_mode="code",
            permission_mode="bypassPermissions",
            allowed_tools=["Bash", "Write"],
            system_prompt="trusted remote prompt",
        )
        request = self.request("192.168.1.82", path="/api/chat")

        with patch.object(server, "_is_mobile_access_request", return_value=True), patch.object(
            server, "_require_mobile_cwd_is_known"
        ) as require_known:
            result = server._authenticated_remote_chat_request(request, req)

        require_known.assert_called_once_with(request, "/saved/project")
        self.assertEqual("bypassPermissions", result.permission_mode)
        self.assertEqual(["Bash", "Write"], result.allowed_tools)
        self.assertEqual("trusted remote prompt", result.system_prompt)

    def test_code_mode_defaults_to_full_auto(self):
        self.assertEqual(
            "bypassPermissions",
            server._effective_permission_mode_for_workspace("code", None),
        )

    def test_root_code_mode_uses_full_allowlist_compatible_mode(self):
        with patch.object(server, "_running_with_root_or_sudo_privileges", return_value=True):
            self.assertEqual(
                "acceptEdits",
                server._effective_permission_mode_for_workspace("code", None),
            )


if __name__ == "__main__":
    unittest.main()
