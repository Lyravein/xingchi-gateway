"""Server HTTP gateway: konstruksi server, auth, routing, dan logging.

Menyatukan mixin I/O dan proxy ke GatewayHandler serta memilih rute endpoint.
Tidak menulis body/SSE dan tidak memanggil upstream secara langsung.
"""

from __future__ import annotations

import hmac
import json
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .admin import handle_admin
from .config import (
    DEFAULT_MODEL,
    GatewayConfig,
    config_from_env,
    load_dotenv,
)
from .keystore import KeyStore
from .translate import strip_instructions

HEALTH_PAYLOAD = {
    "service": "xingchi-gateway",
    "status": "ok",
    "model": DEFAULT_MODEL,
}

def close_quietly(response) -> None:
    try:
        response.close()
    except Exception:
        pass


def sanitize_json_body(body: bytes, content_type: str) -> bytes:
    """Buang instructions dari body JSON upstream; biarkan selain JSON apa adanya."""
    if "application/json" not in content_type.lower():
        return body
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    return json.dumps(
        strip_instructions(parsed),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

# Impor mixin setelah helper: proxy memakai close_quietly/sanitize_json_body.
from .handler_io import HandlerIO  # noqa: E402
from .proxy import ProxyMixin  # noqa: E402


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, config: GatewayConfig):
        self.gateway_config = config
        self.keystore = KeyStore(config.keys_file)
        super().__init__(server_address, GatewayHandler)


class GatewayHandler(HandlerIO, ProxyMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: GatewayServer

    def handle_one_request(self):
        started = time.monotonic()
        self._response_status = None
        super().handle_one_request()
        if getattr(self, "_response_status", None) is not None:
            duration = time.monotonic() - started
            path = urlsplit(getattr(self, "path", "")).path
            method = getattr(self, "command", "")
            sys.stderr.write(
                f"{method} {path} {self._response_status} {duration:.3f}s\n"
            )

    def send_response(self, code, message=None):
        self._response_status = code
        super().send_response(code, message)

    def log_message(self, format, *args):
        return

    def log_error(self, format, *args):
        return

    @property
    def config(self) -> GatewayConfig:
        return self.server.gateway_config

    def request_path(self) -> str:
        return urlsplit(self.path).path

    def gateway_key_authorized(self) -> bool:
        configured = self.config.gateway_api_key
        if not configured:
            return True
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {configured}"
        try:
            return hmac.compare_digest(
                authorization.encode("utf-8"), expected.encode("utf-8")
            )
        except AttributeError:
            return False

    def require_gateway_key(self) -> bool:
        if self.gateway_key_authorized():
            return True
        self.send_unauthorized()
        return False

    def parse_group_route(self) -> tuple[str, str] | None:
        path = self.request_path()
        for suffix in (
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/models",
        ):
            marker = "/v1"
            if path.endswith(suffix) and path.startswith("/"):
                slug = path[1:-len(suffix)]
                if slug and "/" not in slug:
                    return slug, marker + suffix[len(marker):]
        return None

    def group_key_entry(self, slug: str) -> dict | None:
        key = self.server.keystore.select_key(slug)
        if not isinstance(key, dict):
            return None
        secret = key.get("secret")
        if not isinstance(secret, str) or not secret:
            return None
        return key

    def group_key(self, slug: str) -> str | None:
        entry = self.group_key_entry(slug)
        return entry["secret"] if entry else None

    def set_proxy_context(self, group: str, key_label: str, endpoint: str) -> None:
        self._proxy_context = {
            "group": group,
            "key_label": key_label,
            "endpoint": endpoint,
        }

    def send_no_group_key(self) -> None:
        self.send_error_body(
            503,
            "key_store",
            "no_upstream_key",
            "No upstream key configured for group",
        )

    def send_protocol_not_implemented(self) -> None:
        self.send_error_body(
            501,
            "protocol_not_implemented",
            "protocol_not_implemented",
            "Protocol not implemented",
        )

    def do_GET(self):
        if handle_admin(self, self.server.keystore):
            return
        if not self.require_gateway_key():
            return
        path = self.request_path()
        if path in ("/", "/healthz"):
            self.send_json(200, HEALTH_PAYLOAD)
            return
        if path == "/v1/models":
            self.proxy_models()
            return
        route = self.parse_group_route()
        if route and route[1] == "/v1/models":
            slug, _ = route
            group = self.server.keystore.get_group(slug)
            if not group:
                self.send_not_found()
                return
            key = self.group_key(slug)
            if not key:
                self.send_no_group_key()
                return
            self.proxy_models(key)
            return
        self.send_not_found()

    def do_POST(self):
        if handle_admin(self, self.server.keystore):
            return
        if not self.require_gateway_key():
            return
        path = self.request_path()
        if path == "/v1/responses":
            self.set_proxy_context("default", "env", "responses")
            self.proxy_responses()
            return
        if path == "/v1/chat/completions":
            self.set_proxy_context("default", "env", "chat")
            self.proxy_chat_completions()
            return
        route = self.parse_group_route()
        if route:
            slug, endpoint = route
            group = self.server.keystore.get_group(slug)
            if not group:
                self.send_not_found()
                return
            if group.get("protocol") != "responses":
                self.send_protocol_not_implemented()
                return
            entry = self.group_key_entry(slug)
            if not entry:
                self.send_no_group_key()
                return
            label = entry.get("label")
            kind = "responses" if endpoint == "/v1/responses" else "chat"
            self.set_proxy_context(
                slug, label if isinstance(label, str) else "", kind
            )
            if kind == "responses":
                self.proxy_responses(entry["secret"])
            else:
                self.proxy_chat_completions(entry["secret"])
            return
        self.send_not_found()

    def do_HEAD(self):
        if handle_admin(self, self.server.keystore):
            return
        if not self.require_gateway_key():
            return
        if self.request_path() in ("/", "/healthz"):
            self.send_json(200, HEALTH_PAYLOAD)
            return
        self.send_not_found()

    def do_DELETE(self):
        if handle_admin(self, self.server.keystore):
            return
        if self.require_gateway_key():
            self.send_not_found()

    def _unsupported(self):
        if self.require_gateway_key():
            self.send_not_found()

    do_PUT = do_PATCH = do_OPTIONS = _unsupported


def create_server(
    config: GatewayConfig | None = None, port: int | None = None
) -> GatewayServer:
    if config is None:
        load_dotenv()
        config = config_from_env()
    elif port is not None:
        config = replace(config, port=port)
    port_to_bind = port if port is not None else config.port
    return GatewayServer((config.host, port_to_bind), config)


def run(config: GatewayConfig | None = None) -> int:
    load_dotenv()
    if config is None:
        try:
            config = config_from_env()
        except ValueError as exc:
            sys.stderr.write(f"gateway: {exc}\n")
            return 1

    server = GatewayServer((config.host, config.port), config)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
