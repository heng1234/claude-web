"""Tests for the built-in connector catalog: the curated list of MCP
connectors surfaced in the UI grid (like Doubao's connector marketplace).

The catalog is a static JSON file so it can be extended via PRs without code
changes. Each entry must carry enough for one-click prefill of the add form.
"""

import json
import unittest
from pathlib import Path

CATALOG_PATH = Path(__file__).parents[1] / "claude_web" / "mcp_catalog.json"

VALID_TRANSPORTS = {"stdio", "http", "sse"}
VALID_AUTH = {"none", "api_key", "oauth"}


def _load_catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


class ConnectorCatalogStructureTest(unittest.TestCase):
    def setUp(self):
        self.catalog = _load_catalog()

    def test_catalog_is_a_list_of_entries(self):
        self.assertIsInstance(self.catalog, dict)
        self.assertIn("connectors", self.catalog)
        self.assertIsInstance(self.catalog["connectors"], list)
        self.assertGreaterEqual(len(self.catalog["connectors"]), 6)

    def test_every_entry_has_required_fields(self):
        for entry in self.catalog["connectors"]:
            for field in ("id", "name", "category", "transport", "auth", "description"):
                self.assertIn(field, entry, f"{entry.get('id')} missing {field}")
            self.assertIn(entry["transport"], VALID_TRANSPORTS, entry["id"])
            self.assertIn(entry["auth"], VALID_AUTH, entry["id"])

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.catalog["connectors"]]
        self.assertEqual(len(ids), len(set(ids)), "duplicate connector ids")

    def test_remote_entries_carry_a_url_and_stdio_entries_a_command(self):
        for entry in self.catalog["connectors"]:
            if entry["transport"] in ("http", "sse"):
                self.assertTrue(entry.get("url"), f"{entry['id']} remote entry needs url")
            else:
                self.assertTrue(entry.get("command"), f"{entry['id']} stdio entry needs command")

    def test_api_key_entries_declare_where_the_key_goes(self):
        # api_key connectors must tell the UI where the secret goes: either via
        # secret_fields (header/env inputs routed to the secret store) OR by an
        # inline "<...>" placeholder in the URL (e.g. Tushare's .../token=<token>).
        for entry in self.catalog["connectors"]:
            if entry["auth"] == "api_key":
                fields = entry.get("secret_fields")
                url = entry.get("url", "")
                inline_placeholder = "<" in url and ">" in url
                if fields is None:
                    self.assertTrue(
                        inline_placeholder,
                        f"{entry['id']} needs secret_fields or an inline <...> URL placeholder",
                    )
                    continue
                self.assertIsInstance(fields, list, f"{entry['id']} needs secret_fields")
                self.assertGreaterEqual(len(fields), 1, entry["id"])
                for field in fields:
                    self.assertIn("target", field)  # header | env | url | arg
                    self.assertIn("key", field)
                    self.assertIn(field["target"], ("header", "env", "url", "arg"), entry["id"])
                    # arg-target secrets fill a "<...>" token in args, so the
                    # placeholder token must actually exist in the args list.
                    if field["target"] == "arg":
                        token = field.get("placeholder") or ("<" + field["key"] + ">")
                        self.assertIn(token, entry.get("args", []),
                                      f"{entry['id']} arg secret token {token} not in args")

    def test_categories_are_from_a_known_set(self):
        known = {
            "finance", "office", "dev", "media", "web", "research", "other",
        }
        for entry in self.catalog["connectors"]:
            self.assertIn(entry["category"], known, f"{entry['id']} bad category")

    def test_doubao_staple_office_connectors_are_present(self):
        # The connectors the user asked for (Feishu / DingTalk / WeCom / email),
        # all backed by verified public/free MCP packages.
        ids = {e["id"] for e in self.catalog["connectors"]}
        for expected in ("feishu", "dingtalk", "wecom", "email"):
            self.assertIn(expected, ids, f"missing {expected}")

    def test_feishu_uses_arg_target_secrets_matching_official_cli(self):
        feishu = next(e for e in self.catalog["connectors"] if e["id"] == "feishu")
        self.assertEqual(feishu["command"], "npx")
        self.assertIn("@larksuiteoapi/lark-mcp", feishu["args"])
        targets = {f["target"] for f in feishu["secret_fields"]}
        self.assertEqual(targets, {"arg"})

    def test_logo_entries_point_to_bundled_svgs(self):
        static_dir = CATALOG_PATH.parents[0] / "static"
        for entry in self.catalog["connectors"]:
            if entry.get("logo"):
                svg = static_dir / entry["logo"]
                self.assertTrue(svg.is_file(), f"{entry['id']} logo {entry['logo']} missing")

    def test_bundled_logos_are_white_filled_for_dark_tiles(self):
        # simple-icons glyphs default to black; on dark brand tiles they must be
        # forced white or they're invisible.
        static_dir = CATALOG_PATH.parents[0] / "static"
        for entry in self.catalog["connectors"]:
            if entry.get("logo"):
                text = (static_dir / entry["logo"]).read_text(encoding="utf-8")
                self.assertIn('fill="#ffffff"', text, f"{entry['id']} logo not white-filled")

    def test_abbr_entries_carry_a_brand_color(self):
        # Word-mark tiles (no CC0 logo) need both abbr text and a brand color.
        for entry in self.catalog["connectors"]:
            if entry.get("abbr") and not entry.get("logo"):
                self.assertTrue(entry.get("brand"), f"{entry['id']} abbr tile needs brand")

    def test_capability_values_are_from_a_known_set(self):
        # capability is an optional tier hint; only known values are allowed so
        # the UI's label/badge map stays in sync.
        for entry in self.catalog["connectors"]:
            cap = entry.get("capability")
            if cap is not None:
                self.assertIn(cap, ("notify",), f"{entry['id']} bad capability {cap}")

    def test_push_only_bots_are_marked_notify(self):
        # WeCom / DingTalk group robots can only push, not read/write — they must
        # carry capability:notify so the UI badges them "仅通知".
        by_id = {e["id"]: e for e in self.catalog["connectors"]}
        for pid in ("wecom", "dingtalk"):
            self.assertEqual(by_id[pid].get("capability"), "notify",
                             f"{pid} should be capability:notify")

    def test_full_read_write_tools_are_not_marked_notify(self):
        # Feishu / email / GitHub are real read/write tools, not push-only bots;
        # mislabeling them "notify" would misinform users.
        by_id = {e["id"]: e for e in self.catalog["connectors"]}
        for fid in ("feishu", "email", "github"):
            if fid in by_id:
                self.assertNotEqual(by_id[fid].get("capability"), "notify",
                                    f"{fid} is a full tool, not notify-only")


if __name__ == "__main__":
    unittest.main()
