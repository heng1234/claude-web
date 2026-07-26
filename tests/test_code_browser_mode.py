import unittest
from pathlib import Path
from unittest.mock import patch

from claude_web import server


class CodeBrowserModeTest(unittest.TestCase):
    def test_browser_is_hard_disabled_outside_code(self):
        self.assertFalse(server._effective_browser_enabled("chat", True))
        self.assertFalse(server._effective_browser_enabled(None, True))

    def test_code_browser_defaults_on_and_respects_session_or_request(self):
        self.assertTrue(server._effective_browser_enabled("code", None))
        self.assertFalse(server._effective_browser_enabled("code", None, False))
        self.assertTrue(server._effective_browser_enabled("code", True, False))
        self.assertFalse(server._effective_browser_enabled("code", False, True))

    def test_cli_args_are_explicit_for_both_modes(self):
        with patch.object(server, "claude_cli_argv", side_effect=lambda: ["claude"]), \
                patch.object(server, "_claude_cli_supports_chrome_flags", return_value=True):
            chat_args = server.build_args("hello", "chat-session", False, None, None)
            code_args = server.build_persistent_args(
                "code-session",
                False,
                None,
                None,
                browser_enabled=True,
            )
        self.assertIn("--no-chrome", chat_args)
        self.assertNotIn("--chrome", chat_args)
        self.assertIn("--chrome", code_args)
        self.assertNotIn("--no-chrome", code_args)

    def test_old_cli_keeps_chat_working_but_rejects_code_browser(self):
        with patch.object(server, "claude_cli_argv", side_effect=lambda: ["claude"]), \
                patch.object(server, "_claude_cli_supports_chrome_flags", return_value=False):
            chat_args = server.build_args("hello", "chat-session", False, None, None)
            with self.assertRaises(server.ClaudeCliResolutionError):
                server.build_persistent_args(
                    "code-session",
                    False,
                    None,
                    None,
                    browser_enabled=True,
                )
        self.assertNotIn("--chrome", chat_args)
        self.assertNotIn("--no-chrome", chat_args)

    def test_agent_sdk_bridge_passes_official_chrome_extra_arg(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "claude_web"
            / "agent_bridge"
            / "daemon.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("[browserEnabled(params) ? 'chrome' : 'no-chrome']: null", source)
        self.assertIn("browserEnabled: browserEnabled(params)", source)

    def test_browser_control_is_code_only_in_both_static_entries(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ("static/index.html", "claude_web/static/index.html"):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertIn("body.code-mode #codeBrowserBtn", source)
            self.assertIn('id="codeBrowserBtn"', source)
            self.assertIn("browser_enabled:", source)
            self.assertIn("turnIsCode ? !!settings.codeBrowser : false", source.replace("codeMode ?", "turnIsCode ?"))
            self.assertIn("settings.codeBrowser = data.browser_enabled", source)


if __name__ == "__main__":
    unittest.main()
