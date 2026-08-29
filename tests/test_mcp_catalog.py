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

    def test_api_key_entries_declare_their_secret_fields(self):
        # api_key connectors must tell the UI which header/env keys to collect,
        # so the add form can render inputs and route them to the secret store.
        for entry in self.catalog["connectors"]:
            if entry["auth"] == "api_key":
                fields = entry.get("secret_fields")
                self.assertIsInstance(fields, list, f"{entry['id']} needs secret_fields")
                self.assertGreaterEqual(len(fields), 1, entry["id"])
                for field in fields:
                    self.assertIn("target", field)  # "header" | "env"
                    self.assertIn("key", field)
                    self.assertIn(field["target"], ("header", "env"), entry["id"])

    def test_categories_are_from_a_known_set(self):
        known = {
            "finance", "office", "dev", "media", "web", "research", "other",
        }
        for entry in self.catalog["connectors"]:
            self.assertIn(entry["category"], known, f"{entry['id']} bad category")


if __name__ == "__main__":
    unittest.main()
