"""Tes statistik usage (tes 31..35)."""

import json
import os
import tempfile
from datetime import datetime, timezone

from xingchi_gateway import stats
from xingchi_gateway.config import create_config

from .helpers import GATEWAY_KEY, UPSTREAM_KEY, GatewayTestCase

CHAT_BODY = {
    "model": "gpt-6.1-sol",
    "messages": [{"role": "user", "content": "Halo statistik"}],
}

RESPONSES_BODY = {"model": "gpt-6.1-sol", "input": "Halo statistik"}

class StatsTests(GatewayTestCase):
    def read_lines(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_31_records_chat_and_responses_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stats.jsonl")
            self.upstream.fixture["responses_payload"] = {
                "id": "resp_stats",
                "model": "gpt-6.1-sol",
                "output": [],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
            }
            server, thread = self.start_server(stats_file=path)
            try:
                port = server.server_address[1]
                status, _ = self.request_port(
                    port, "POST", "/v1/chat/completions", CHAT_BODY,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
                status, _ = self.request_port(
                    port, "POST", "/v1/responses", RESPONSES_BODY,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
            finally:
                self.stop_admin_server(server, thread)

            entries = self.read_lines(path)
            self.assertEqual(len(entries), 2)
            chat, responses = entries
            for entry in entries:
                for field in (
                    "ts", "route", "group", "endpoint", "model", "status",
                    "stream", "duration_ms", "prompt_tokens",
                    "completion_tokens", "key_label",
                ):
                    self.assertIn(field, entry)
            self.assertEqual(chat["endpoint"], "chat")
            self.assertEqual(chat["group"], "default")
            self.assertEqual(chat["key_label"], "env")
            self.assertEqual(chat["status"], 200)
            self.assertFalse(chat["stream"])
            self.assertEqual(chat["prompt_tokens"], 7)
            self.assertEqual(chat["completion_tokens"], 3)
            self.assertEqual(responses["endpoint"], "responses")
            self.assertEqual(responses["prompt_tokens"], 7)
            self.assertEqual(responses["completion_tokens"], 3)

    def test_32_summarize_groups_by_group_model_and_day(self):
        with tempfile.TemporaryDirectory() as directory:
            config = create_config(
                api_key=UPSTREAM_KEY,
                base_url=self.base_url,
                keys_file=self.keys_path,
                stats_file=os.path.join(directory, "stats.jsonl"),
            )
            rows = [
                ("2026-11-20T01:00:00Z", "gpt", "gpt-6.1-sol", 200, 10, 5),
                ("2026-11-20T02:00:00Z", "gpt", "gpt-6.1-sol", 500, 2, 0),
                ("2026-11-18T02:00:00Z", "lama", "model-lama", 200, 4, 4),
                ("2026-11-01T02:00:00Z", "lama", "model-lama", 200, 99, 99),
            ]
            for ts, group, model, status, prompt, completion in rows:
                stats.record(config, {
                    "ts": ts,
                    "group": group,
                    "model": model,
                    "status": status,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                })

            now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
            summary = stats.summarize(config, now=now)

            today = summary["today"]["groups"]["gpt"]
            self.assertEqual(today["count"], 2)
            self.assertEqual(today["errors"], 1)
            self.assertEqual(today["prompt_tokens"], 12)
            self.assertEqual(today["completion_tokens"], 5)
            self.assertEqual(
                summary["today"]["models"]["gpt-6.1-sol"]["count"], 2
            )
            self.assertNotIn("lama", summary["today"]["groups"])

            week = summary["last_7_days"]["groups"]
            self.assertEqual(week["gpt"]["count"], 2)
            self.assertEqual(week["lama"]["count"], 1)
            self.assertEqual(week["lama"]["prompt_tokens"], 4)
            self.assertEqual(len(summary["recent"]), 4)
            self.assertEqual(summary["recent"][0]["ts"], "2026-11-20T02:00:00Z")

    def test_33_rotation_creates_backup_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stats.jsonl")
            config = create_config(
                api_key=UPSTREAM_KEY,
                base_url=self.base_url,
                keys_file=self.keys_path,
                stats_file=path,
            )
            original = stats.MAX_BYTES
            stats.MAX_BYTES = 10
            try:
                stats.record(config, {
                    "ts": "2026-11-20T00:00:00Z",
                    "group": "gpt",
                    "model": "m",
                    "status": 200,
                })
                stats.record(config, {
                    "ts": "2026-11-20T00:00:01Z",
                    "group": "gpt",
                    "model": "m",
                    "status": 200,
                })
            finally:
                stats.MAX_BYTES = original
            self.assertTrue(os.path.exists(path))
            self.assertTrue(os.path.exists(path + ".1"))
            self.assertEqual(len(self.read_lines(path)), 1)

    def test_34_admin_stats_endpoint_and_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stats.jsonl")
            server, thread = self.start_server(
                admin_token="admin-secret-test", stats_file=path
            )
            try:
                port = server.server_address[1]
                status, _ = self.request_port(port, "GET", "/admin/api/stats")
                self.assertEqual(status, 401)
                status, body = self.request_port(
                    port, "GET", "/admin/api/stats",
                    authorization="admin-secret-test",
                )
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertTrue(payload["enabled"])
                self.assertIn("today", payload)
                self.assertIn("recent", payload)
                status, page = self.request_port(port, "GET", "/admin")
                self.assertEqual(status, 200)
                self.assertIn("Usage", page.decode("utf-8"))
            finally:
                self.stop_admin_server(server, thread)

    def test_35_stats_off_disables_recording(self):
        os.environ["GATEWAY_STATS_FILE"] = "off"
        try:
            config = create_config(
                api_key=UPSTREAM_KEY, keys_file=self.keys_path
            )
            self.assertEqual(config.stats_file, "")
        finally:
            os.environ.pop("GATEWAY_STATS_FILE", None)

        default_path = os.path.join(self.keys_dir.name, "stats.jsonl")
        before = os.path.getsize(default_path) if os.path.exists(default_path) else 0

        server, thread = self.start_server(
            stats_file="off", admin_token="admin-secret-test"
        )
        try:
            self.assertEqual(server.gateway_config.stats_file, "")
            port = server.server_address[1]
            status, _ = self.request_port(
                port, "POST", "/v1/chat/completions", CHAT_BODY,
                authorization=GATEWAY_KEY,
            )
            self.assertEqual(status, 200)
            status, body = self.request_port(
                port, "GET", "/admin/api/stats",
                authorization="admin-secret-test",
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"enabled": False})
        finally:
            self.stop_admin_server(server, thread)

        after = os.path.getsize(default_path) if os.path.exists(default_path) else 0
        self.assertEqual(after, before)
