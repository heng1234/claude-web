"""Static contract tests for the Connector Hub UI.

These assert against the file the server actually serves
(``claude_web/static/index.html``, mounted from ``_PKG_DIR / "static"``).
The repository-root ``static/index.html`` is a stale duplicate and is
deliberately not checked here.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
INDEX = ROOT / "claude_web" / "static" / "index.html"
CATALOG = ROOT / "claude_web" / "mcp_catalog.json"


class ConnectorHubMarkupTest(unittest.TestCase):
    def setUp(self):
        self.source = INDEX.read_text(encoding="utf-8")

    def test_transport_selector_offers_stdio_and_remote_options(self):
        self.assertIn('id="mcpNewTransport"', self.source)
        self.assertIn('<option value="stdio"', self.source)
        self.assertIn('<option value="http"', self.source)
        self.assertIn('<option value="sse"', self.source)

    def test_remote_fields_exist_and_start_hidden(self):
        self.assertIn('id="mcpStdioFields"', self.source)
        self.assertIn('id="mcpRemoteFields" class="space-y-2 hidden"', self.source)
        self.assertIn('id="mcpNewUrl"', self.source)
        self.assertIn('id="mcpNewHeaders"', self.source)

    def test_catalog_container_is_present(self):
        self.assertIn('id="mcpCatalogWrap"', self.source)
        self.assertIn('id="mcpCatalogCats"', self.source)
        self.assertIn('id="mcpCatalogGrid"', self.source)

    def test_encrypt_secrets_is_opt_out_not_opt_in(self):
        self.assertIn('id="mcpEncryptSecrets" type="checkbox" checked', self.source)


class ConnectorHubBehaviourTest(unittest.TestCase):
    def setUp(self):
        self.source = INDEX.read_text(encoding="utf-8")

    def test_transport_toggle_swaps_field_groups(self):
        self.assertIn("function applyMcpTransportVisibility()", self.source)
        self.assertIn("$('mcpStdioFields')?.classList.toggle('hidden', remote)", self.source)
        self.assertIn("$('mcpRemoteFields')?.classList.toggle('hidden', !remote)", self.source)
        self.assertIn(
            "$('mcpNewTransport')?.addEventListener('change', applyMcpTransportVisibility)",
            self.source,
        )

    def test_add_request_sends_transport_and_encrypt_secrets(self):
        self.assertIn("const transport = ($('mcpNewTransport')?.value || 'stdio').toLowerCase()", self.source)
        self.assertIn("body = { type: transport, url,", self.source)
        self.assertIn("encrypt_secrets: encryptSecrets", self.source)
        # Secret key names are collected for both remote headers and stdio env.
        self.assertIn("if (encrypt) encryptSecrets.push(key)", self.source)

    def test_remote_add_requires_a_url(self):
        self.assertIn("if (!url) { alert(t('errors.cannot_be_empty')); return; }", self.source)

    def test_catalog_is_lazy_loaded_on_expand(self):
        self.assertIn("async function loadMcpCatalog()", self.source)
        self.assertIn("await fetch('/api/mcp/catalog')", self.source)
        self.assertIn("$('mcpCatalogWrap')?.addEventListener('toggle'", self.source)
        self.assertIn("if (e.target.open) loadMcpCatalog()", self.source)

    def test_catalog_render_is_category_filtered(self):
        self.assertIn("function renderMcpCatalog()", self.source)
        self.assertIn("mcpCatalogCategory === 'all'", self.source)
        self.assertIn("data-mcp-cat=", self.source)
        self.assertIn("data-mcp-prefill=", self.source)

    def test_prefill_fills_transport_specific_fields_only(self):
        self.assertIn("function prefillMcpConnector(id)", self.source)
        self.assertIn("$('mcpNewTransport').value = transport", self.source)
        self.assertIn("(c.secret_fields || []).filter(f => f.target === 'header')", self.source)
        self.assertIn("(c.secret_fields || []).filter(f => f.target === 'env')", self.source)

    def test_needs_auth_health_result_offers_authorize(self):
        self.assertIn("data.status === 'needs-auth' || data.needs_auth", self.source)
        self.assertIn("/authorize'", self.source)

    def test_catalog_escapes_untrusted_catalog_strings(self):
        # Catalog entries are rendered into innerHTML; every interpolated field
        # must go through escapeHtml so a hand-edited catalog cannot inject markup.
        self.assertIn("escapeHtml(c.name || c.id)", self.source)
        self.assertIn("escapeHtml(c.description || '')", self.source)
        self.assertIn("escapeHtml(c.icon || '🧩')", self.source)
        self.assertIn('escapeHtml(c.id)', self.source)


class ConnectorCatalogDataTest(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    def test_every_connector_declares_the_fields_the_ui_reads(self):
        for entry in self.catalog["connectors"]:
            with self.subTest(entry.get("id")):
                self.assertTrue(entry.get("id"))
                self.assertTrue(entry.get("name"))
                self.assertIn(entry.get("transport"), {"stdio", "http", "sse"})
                self.assertIn(entry.get("auth"), {"none", "api_key", "oauth"})
                self.assertTrue(entry.get("category"))
                if entry["transport"] == "stdio":
                    self.assertTrue(entry.get("command"))
                else:
                    self.assertTrue(str(entry.get("url", "")).startswith("http"))

    def test_secret_fields_target_a_channel_the_prefill_understands(self):
        for entry in self.catalog["connectors"]:
            for field in entry.get("secret_fields", []):
                with self.subTest(entry.get("id"), key=field.get("key")):
                    self.assertIn(field.get("target"), {"header", "env"})
                    self.assertTrue(field.get("key"))

    def test_catalog_ships_no_baked_in_credentials(self):
        raw = CATALOG.read_text(encoding="utf-8")
        for needle in ("ghp_", "sk-", "Bearer ey", "password"):
            self.assertNotIn(needle, raw)


if __name__ == "__main__":
    unittest.main()
