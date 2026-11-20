"""Tes halaman dan API admin (tes 16..18, 20..25)."""

import http.client
import json
import os
import re
import shutil
import subprocess
import unittest

from .helpers import ADMIN_HTML_PATH, GatewayTestCase


class AdminTests(GatewayTestCase):
    def test_16_admin_404_without_token_and_401_with_wrong_token(self):
        status, _ = self.request_port(
            self.gateway_port, "GET", "/admin/api/groups"
        )
        self.assertEqual(status, 404)

        server, thread = self.start_admin_server("admin-secret-test")
        try:
            port = server.server_address[1]
            status, _ = self.request_port(port, "GET", "/admin/api/groups")
            self.assertEqual(status, 401)
            status, _ = self.request_port(
                port,
                "GET",
                "/admin/api/groups",
                authorization="admin-secret-test",
            )
            self.assertEqual(status, 200)
        finally:
            self.stop_admin_server(server, thread)

    def test_17_admin_add_key_validation_failure_not_saved(self):
        store = self.gateway_server.keystore
        store.add_group("gpt", "GPT", "responses")
        server, thread = self.start_admin_server("admin-secret-test")
        try:
            port = server.server_address[1]
            self.upstream.fixture["models_status"] = 403
            self.upstream.fixture["models_payload"] = {
                "error": {"message": "client_not_allowed", "code": "client_not_allowed"}
            }
            status, body = self.request_port(
                port,
                "POST",
                "/admin/api/groups/gpt/keys",
                {"secret": "test-key-bukan-asli", "label": "ditolak"},
                authorization="admin-secret-test",
            )
            self.assertEqual(status, 403)
            payload = json.loads(body)
            self.assertIn("error", payload)
            self.assertEqual(store.get_group("gpt").get("keys"), [])
        finally:
            self.stop_admin_server(server, thread)

    def test_18_admin_groups_masks_full_secret(self):
        store = self.gateway_server.keystore
        store.add_group("gpt", "GPT", "responses")
        store.add_key("gpt", "test-key-bukan-asli-1234", "utama")
        server, thread = self.start_admin_server("admin-secret-test")
        try:
            port = server.server_address[1]
            status, body = self.request_port(
                port,
                "GET",
                "/admin/api/groups",
                authorization="admin-secret-test",
            )
            self.assertEqual(status, 200)
            text = body.decode("utf-8")
            self.assertNotIn("test-key-bukan-asli-1234", text)
            payload = json.loads(text)
            self.assertEqual(payload["groups"][0]["keys"][0]["last4"], "1234")
        finally:
            self.stop_admin_server(server, thread)

    def test_20_admin_page_served_without_authorization(self):
        server, thread = self.start_admin_server("admin-secret-test")
        try:
            port = server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/admin")
            response = connection.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "")
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn("<html", body)
            self.assertIn("Admin token", body)
        finally:
            self.stop_admin_server(server, thread)

    def test_21_admin_api_still_requires_token(self):
        server, thread = self.start_admin_server("admin-secret-test")
        try:
            port = server.server_address[1]
            status, _ = self.request_port(port, "GET", "/admin/api/groups")
            self.assertEqual(status, 401)
        finally:
            self.stop_admin_server(server, thread)

    def test_22_admin_page_404_without_configured_token(self):
        status, _ = self.request_port(self.gateway_port, "GET", "/admin")
        self.assertEqual(status, 404)

    @unittest.skipUnless(shutil.which("node"), "node tidak tersedia")
    def test_23_admin_page_script_parses_with_node(self):
        page = ADMIN_HTML_PATH.read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", page, re.S)
        self.assertIsNotNone(match)
        path = os.path.join(self.keys_dir.name, "admin-page.js")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(match.group(1))
        result = subprocess.run(
            ["node", "--check", path], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_24_admin_page_missing_returns_500(self):
        from xingchi_gateway import admin as admin_module

        server, thread = self.start_admin_server("admin-secret-test")
        original = admin_module.ADMIN_PAGE
        admin_module.ADMIN_PAGE = None
        try:
            port = server.server_address[1]
            status, body = self.request_port(port, "GET", "/admin")
            self.assertEqual(status, 500)
            payload = json.loads(body)
            self.assertEqual(payload["error"]["code"], "admin_page_missing")
        finally:
            admin_module.ADMIN_PAGE = original
            self.stop_admin_server(server, thread)

    def test_25_admin_page_group_form_reports_errors(self):
        page = ADMIN_HTML_PATH.read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", page, re.S)
        self.assertIsNotNone(match)
        script = match.group(1)
        self.assertIn('id="groupResult"', page)
        self.assertIn("groupResult", script)
        self.assertIn("catch", script)
