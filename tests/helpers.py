"""Fixture bersama tes: upstream palsu dan base test case.

Menyediakan FakeUpstreamHandler, server gateway sementara, dan helper request.
Tidak berisi satu pun method test.
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from xingchi_gateway.config import DEFAULT_USER_AGENT, create_config
from xingchi_gateway.server import create_server

ADMIN_HTML_PATH = Path(__file__).parent.parent / "xingchi_gateway" / "admin.html"

UPSTREAM_KEY = "test-key-bukan-asli"
GATEWAY_KEY = "gateway-test-key-bukan-asli"


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            return self.rfile.read(length)
        return b""

    def send_body(self, status, body, content_type="application/json"):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        body = self.read_body()
        self.server.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        if self.path == "/v1/models":
            status = self.server.fixture["models_status"]
            payload = self.server.fixture["models_payload"]
            encoded = json.dumps(payload).encode("utf-8")
            self.send_body(status, encoded)
            return
        self.send_body(404, b'{"error":{"message":"Not Found"}}')

    def do_POST(self):
        body = self.read_body()
        self.server.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        if self.path != "/v1/responses":
            self.send_body(
                404,
                b'{"error":{"message":"Not Found","code":"not_found"}}',
            )
            return

        status = self.server.fixture["responses_status"]
        content_type = self.server.fixture["responses_content_type"]
        payload = self.server.fixture["responses_payload"]
        if content_type == "text/event-stream":
            event_lines = []
            for event in self.server.fixture["responses_events"]:
                event_lines.append("event: " + event["type"])
                event_lines.append(
                    "data: "
                    + json.dumps(
                        event["data"], ensure_ascii=False, separators=(",", ":")
                    )
                )
                event_lines.append("")
            event_lines.append("data: [DONE]")
            event_lines.append("")
            encoded = ("\n".join(event_lines)).encode("utf-8")
            self.send_body(status, encoded, content_type)
            return

        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_body(status, encoded, content_type)


class GatewayTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeUpstreamHandler
        )
        cls.upstream.daemon_threads = True
        cls.upstream.requests = []
        cls.upstream.fixture = {
            "models_status": 200,
            "models_payload": {
                "object": "list",
                "data": [
                    {
                        "id": "gpt-6.1-sol",
                        "object": "model",
                        "owned_by": "xingchi",
                    }
                ],
            },
            "responses_status": 200,
            "responses_content_type": "application/json",
            "responses_payload": {},
            "responses_events": [],
        }
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True
        )
        cls.upstream_thread.start()

        upstream_host, upstream_port = cls.upstream.server_address
        cls.base_url = f"http://{upstream_host}:{upstream_port}"
        cls.keys_dir = tempfile.TemporaryDirectory(prefix="xingchi-keys-")
        cls.keys_path = os.path.join(cls.keys_dir.name, "keys.json")
        cls.config = create_config(
            api_key=UPSTREAM_KEY,
            base_url=cls.base_url,
            user_agent=DEFAULT_USER_AGENT,
            host="127.0.0.1",
            port=8789,
            upstream_timeout=2.0,
            gateway_api_key=GATEWAY_KEY,
            keys_file=cls.keys_path,
            admin_token="",
        )
        cls.gateway_server = create_server(cls.config, port=0)
        cls.gateway_thread = threading.Thread(
            target=cls.gateway_server.serve_forever, daemon=True
        )
        cls.gateway_thread.start()
        cls.gateway_port = cls.gateway_server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.gateway_server.shutdown()
        cls.gateway_server.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.gateway_thread.join(timeout=2)
        cls.upstream_thread.join(timeout=2)
        cls.keys_dir.cleanup()

    def setUp(self):
        self.gateway_server.keystore.save({"groups": []})
        self.upstream.requests.clear()
        self.upstream.fixture.update(
            {
                "models_status": 200,
                "models_payload": {
                    "object": "list",
                    "data": [
                        {
                            "id": "gpt-6.1-sol",
                            "object": "model",
                            "owned_by": "xingchi",
                        }
                    ],
                },
                "responses_status": 200,
                "responses_content_type": "application/json",
                "responses_payload": {},
                "responses_events": [],
            }
        )

    def request(
        self,
        method,
        path,
        body=None,
        gateway_key=GATEWAY_KEY,
    ):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.gateway_port, timeout=3
        )
        headers = {}
        if gateway_key is not None:
            headers["Authorization"] = f"Bearer {gateway_key}"
        if body is not None:
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        content_type = response.getheader("Content-Type", "")
        response_body = response.read()
        connection.close()
        return status, content_type, response_body

    def post_chat(self, payload, gateway_key=GATEWAY_KEY):
        return self.request(
            "POST", "/v1/chat/completions", payload, gateway_key=gateway_key
        )

    def request_port(
        self, port, method, path, body=None, authorization=None
    ):
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=3
        )
        headers = {}
        if authorization:
            headers["Authorization"] = f"Bearer {authorization}"
        if body is not None:
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_body = response.read()
        connection.close()
        return status, response_body

    def start_admin_server(self, token):
        return self.start_server(admin_token=token)

    def start_server(self, **overrides):
        options = {
            "api_key": UPSTREAM_KEY,
            "base_url": self.base_url,
            "user_agent": DEFAULT_USER_AGENT,
            "host": "127.0.0.1",
            "port": 8789,
            "upstream_timeout": 2.0,
            "gateway_api_key": GATEWAY_KEY,
            "keys_file": self.keys_path,
            "admin_token": "",
        }
        options.update(overrides)
        config = create_config(**options)
        server = create_server(config, port=0)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        thread.start()
        return server, thread

    def stop_admin_server(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def parse_json(self, body):
        return json.loads(body.decode("utf-8"))

    def parse_sse(self, body):
        chunks = []
        done = False
        for block in body.decode("utf-8").split("\n\n"):
            if not block:
                continue
            data_lines = [
                line[len("data: "):]
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if data == "[DONE]":
                done = True
            else:
                chunks.append(json.loads(data))
        return chunks, done
