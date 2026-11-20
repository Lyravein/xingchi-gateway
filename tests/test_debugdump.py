"""Tes debug body dump (tes 26..30)."""

import glob
import json
import os
import tempfile

from .helpers import GATEWAY_KEY, UPSTREAM_KEY, GatewayTestCase

CHAT_BODY = {
    "model": "gpt-6.1-sol",
    "messages": [{"role": "user", "content": "Halo dump"}],
}

class DebugDumpTests(GatewayTestCase):
    def dump_files(self, directory, suffix):
        return sorted(glob.glob(os.path.join(directory, f"*-{suffix}.json")))

    def read_dump(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_26_debug_off_writes_no_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            server, thread = self.start_server(
                debug_dump=False, debug_dir=directory
            )
            try:
                status, _ = self.request_port(
                    server.server_address[1],
                    "POST",
                    "/v1/chat/completions",
                    CHAT_BODY,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
                self.assertEqual(os.listdir(directory), [])
            finally:
                self.stop_admin_server(server, thread)

    def test_27_debug_on_writes_three_files(self):
        self.upstream.fixture["responses_payload"] = {
            "id": "resp_dump",
            "model": "gpt-6.1-sol",
            "output": [],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        }
        with tempfile.TemporaryDirectory() as directory:
            server, thread = self.start_server(
                debug_dump=True, debug_dir=directory
            )
            try:
                status, _ = self.request_port(
                    server.server_address[1],
                    "POST",
                    "/v1/chat/completions",
                    CHAT_BODY,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
                self.assertEqual(len(self.dump_files(directory, "inbound")), 1)
                self.assertEqual(len(self.dump_files(directory, "upstream")), 1)
                self.assertEqual(len(self.dump_files(directory, "response")), 1)

                inbound = self.read_dump(self.dump_files(directory, "inbound")[0])
                upstream = self.read_dump(
                    self.dump_files(directory, "upstream")[0]
                )
                self.assertIn("messages", inbound["body"])
                self.assertEqual(inbound["endpoint"], "chat")
                self.assertEqual(inbound["route"], "/v1/chat/completions")
                self.assertIn("instructions", upstream["body"])
                self.assertEqual(upstream["path"], "/v1/responses")
                self.assertEqual(upstream["key_label"], "env")
                self.assertEqual(upstream["key_last4"], UPSTREAM_KEY[-4:])
            finally:
                self.stop_admin_server(server, thread)

    def test_28_dump_never_contains_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            server, thread = self.start_server(
                debug_dump=True, debug_dir=directory
            )
            try:
                status, _ = self.request_port(
                    server.server_address[1],
                    "POST",
                    "/v1/chat/completions",
                    CHAT_BODY,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
                files = glob.glob(os.path.join(directory, "*.json"))
                self.assertEqual(len(files), 3)
                for path in files:
                    with open(path, "r", encoding="utf-8") as handle:
                        text = handle.read()
                    self.assertNotIn(UPSTREAM_KEY, text)
                    self.assertNotIn(GATEWAY_KEY, text)
            finally:
                self.stop_admin_server(server, thread)

    def test_29_large_body_is_clamped(self):
        big_body = {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "x" * 4000}],
        }
        with tempfile.TemporaryDirectory() as directory:
            server, thread = self.start_server(
                debug_dump=True, debug_dir=directory, debug_max_body=64
            )
            try:
                status, _ = self.request_port(
                    server.server_address[1],
                    "POST",
                    "/v1/chat/completions",
                    big_body,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
                inbound = self.read_dump(self.dump_files(directory, "inbound")[0])
                self.assertTrue(inbound["truncated"])
                self.assertIsInstance(inbound["body"], str)
                self.assertLessEqual(len(inbound["body"].encode("utf-8")), 64)
            finally:
                self.stop_admin_server(server, thread)

    def test_30_stream_dump_keeps_client_bytes(self):
        self.upstream.fixture["responses_content_type"] = "text/event-stream"
        self.upstream.fixture["responses_events"] = [
            {
                "type": "response.output_text.delta",
                "data": {
                    "type": "response.output_text.delta",
                    "delta": "Halo ",
                },
            },
            {
                "type": "response.completed",
                "data": {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_stream_dump",
                        "created_at": 1710000009,
                        "model": "gpt-6.1-sol",
                        "status": "completed",
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 4,
                            "total_tokens": 15,
                        },
                    },
                },
            },
        ]
        stream_body = {
            "model": "gpt-6.1-sol",
            "input": "Halo",
            "stream": True,
        }

        _, _, expected = self.request("POST", "/v1/responses", stream_body)

        with tempfile.TemporaryDirectory() as directory:
            server, thread = self.start_server(
                debug_dump=True, debug_dir=directory
            )
            try:
                status, actual = self.request_port(
                    server.server_address[1],
                    "POST",
                    "/v1/responses",
                    stream_body,
                    authorization=GATEWAY_KEY,
                )
                self.assertEqual(status, 200)
                self.assertEqual(actual, expected)

                response = self.read_dump(
                    self.dump_files(directory, "response")[0]
                )
                self.assertTrue(response["stream"])
                self.assertEqual(response["status"], 200)
                self.assertIn("response.output_text.delta", response["body"])
                self.assertIn("Halo ", response["body"])
            finally:
                self.stop_admin_server(server, thread)
