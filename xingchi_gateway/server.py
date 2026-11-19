"""Server HTTP gateway dan handler semua endpoint.

Menangani auth, routing, penulisan respons/SSE ke client, dan logging ringkas.
Tidak berbicara langsung ke upstream; itu tugas modul upstream.
"""

from __future__ import annotations

import hmac
import http.client
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .config import (
    DEFAULT_MODEL,
    GatewayConfig,
    config_from_env,
    load_dotenv,
)
from .translate import (
    ChatStreamState,
    strip_instructions,
    stream_closed_chunk,
    stream_done_chunk,
    translate_chat_request,
    translate_responses_to_chat,
)
from .upstream import (
    UpstreamFailure,
    parse_error_body,
    read_stream_response,
    request_upstream,
)

HEALTH_PAYLOAD = {
    "service": "xingchi-gateway",
    "status": "ok",
    "model": DEFAULT_MODEL,
}

MODELS_FALLBACK = {
    "object": "list",
    "data": [
        {
            "id": DEFAULT_MODEL,
            "object": "model",
            "owned_by": "xingchi",
        }
    ],
}

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, OSError)
_READ_ERRORS = (http.client.HTTPException, http.client.IncompleteRead, OSError)


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


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, config: GatewayConfig):
        self.gateway_config = config
        super().__init__(server_address, GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
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

    def write_bytes(self, body: bytes) -> bool:
        try:
            self.wfile.write(body)
            return True
        except _CLIENT_GONE:
            return False

    def send_json(self, status: int, payload: dict, headers=None) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for name, value in headers.items():
                self.send_header(name, str(value))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.write_bytes(body)

    def send_error_body(
        self, status: int, error_type: str, code, message: str
    ) -> None:
        error: dict = {"message": message, "type": error_type}
        if code is not None:
            error["code"] = code
        self.send_json(status, {"error": error})

    def send_unauthorized(self) -> None:
        self.send_error_body(401, "unauthorized", "unauthorized", "Unauthorized")

    def send_invalid_json(self) -> None:
        self.send_error_body(400, "invalid_json", "invalid_json", "Invalid JSON")

    def send_not_found(self) -> None:
        self.send_error_body(404, "not_found", "not_found", "Not Found")

    def send_network_error(self) -> None:
        self.send_failure(
            UpstreamFailure(502, "upstream_unreachable", "Upstream unreachable",
                            error_type="upstream_unreachable")
        )

    def send_failure(self, failure: UpstreamFailure) -> None:
        self.send_error_body(
            failure.status,
            failure.error_type,
            failure.code if failure.code not in (None, "") else None,
            failure.message,
        )

    def read_request_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except (OSError, http.client.IncompleteRead):
            return b""

    def parse_json_body(self, body: bytes) -> tuple[dict | None, bool]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, False
        return (value, True) if isinstance(value, dict) else (None, False)

    def require_gateway_key(self) -> bool:
        if self.gateway_key_authorized():
            return True
        self.send_unauthorized()
        return False

    def do_GET(self):
        if not self.require_gateway_key():
            return
        path = self.request_path()
        if path in ("/", "/healthz"):
            self.send_json(200, HEALTH_PAYLOAD)
            return
        if path == "/v1/models":
            self.proxy_models()
            return
        self.send_not_found()

    def do_POST(self):
        if not self.require_gateway_key():
            return
        path = self.request_path()
        if path == "/v1/responses":
            self.proxy_responses()
            return
        if path == "/v1/chat/completions":
            self.proxy_chat_completions()
            return
        self.send_not_found()

    def do_HEAD(self):
        if not self.require_gateway_key():
            return
        if self.request_path() in ("/", "/healthz"):
            self.send_json(200, HEALTH_PAYLOAD)
            return
        self.send_not_found()

    def _unsupported(self):
        if self.require_gateway_key():
            self.send_not_found()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _unsupported

    def proxy_models(self) -> None:
        try:
            response = request_upstream(self.config, "GET", "/v1/models")
        except UpstreamFailure as failure:
            if failure.error_type == "upstream_error":
                self.send_failure(failure)
                return
            self.send_json(502, MODELS_FALLBACK)
            return

        try:
            status = response.status
            body = response.read()
            content_type = response.headers.get(
                "Content-Type", "application/json"
            )
        except _READ_ERRORS:
            self.send_network_error()
            return
        finally:
            close_quietly(response)

        if status >= 400:
            message, code = parse_error_body(body)
            self.send_error_body(status, "upstream_error", code, message)
            return

        self.send_raw(status, content_type, body)

    def send_raw(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.write_bytes(body)

    def open_checked(self, method: str, path: str, body: bytes | None):
        """Buka upstream dan buang respons error >=400 jadi UpstreamFailure."""
        try:
            response = request_upstream(self.config, method, path, body)
            status = response.status
            content_type = response.headers.get(
                "Content-Type", "application/json"
            )
            if status < 400:
                return response, status, content_type
            try:
                error_body = response.read()
            except _READ_ERRORS:
                error_body = b""
            response.close()
        except UpstreamFailure as failure:
            self.send_failure(failure)
            return None, None, None
        except _READ_ERRORS:
            self.send_network_error()
            return None, None, None

        message, code = parse_error_body(error_body)
        self.send_error_body(status, "upstream_error", code, message)
        return None, None, None

    def proxy_responses(self) -> None:
        raw_body = self.read_request_body()
        parsed, valid = self.parse_json_body(raw_body)
        if not valid:
            self.send_invalid_json()
            return

        response, status, content_type = self.open_checked(
            "POST", "/v1/responses", raw_body
        )
        if response is None:
            return

        try:
            if "text/event-stream" in content_type.lower():
                self.stream_passthrough_responses(response)
                return
            body = response.read()
        except _READ_ERRORS:
            response.close()
            self.send_network_error()
            return
        finally:
            close_quietly(response)

        if status == 204:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_raw(status, content_type, sanitize_json_body(body, content_type))

    def start_sse(self, status: int = 200, content_type: str = "text/event-stream"):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    def stream_passthrough_responses(self, response) -> None:
        content_type = response.headers.get("Content-Type", "text/event-stream")
        self.start_sse(response.status, content_type)

        try:
            for _, data in read_stream_response(response):
                if not data:
                    continue
                if data != "[DONE]":
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        data = json.dumps(
                            strip_instructions(parsed), ensure_ascii=False,
                            separators=(",", ":"),
                        )
                if not self.write_bytes(f"data: {data}\n\n".encode("utf-8")):
                    break
                self.flush_client()
        finally:
            close_quietly(response)

    def flush_client(self) -> None:
        try:
            self.wfile.flush()
        except _CLIENT_GONE:
            pass

    def write_sse_json(self, payload: dict) -> bool:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if not self.write_bytes(f"data: {data}\n\n".encode("utf-8")):
            return False
        self.flush_client()
        return True

    def write_done(self) -> bool:
        if not self.write_bytes(b"data: [DONE]\n\n"):
            return False
        self.flush_client()
        return True

    def proxy_chat_completions(self) -> None:
        raw_body = self.read_request_body()
        body, valid = self.parse_json_body(raw_body)
        if not valid or body is None:
            self.send_invalid_json()
            return

        responses_body = translate_chat_request(body)
        upstream_body = json.dumps(
            responses_body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        response, _, _ = self.open_checked(
            "POST", "/v1/responses", upstream_body
        )
        if response is None:
            return

        if responses_body.get("stream") is True:
            self.stream_chat_response(response, body)
            return

        try:
            upstream_json_bytes = response.read()
        except _READ_ERRORS:
            self.send_network_error()
            return
        finally:
            close_quietly(response)

        try:
            upstream_json = json.loads(upstream_json_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_network_error()
            return
        if not isinstance(upstream_json, dict):
            self.send_network_error()
            return

        chat_payload = translate_responses_to_chat(
            upstream_json, body.get("model") or DEFAULT_MODEL
        )
        self.send_json(200, strip_instructions(chat_payload))

    def stream_chat_response(self, response, chat_request: dict) -> None:
        state = ChatStreamState(chat_request.get("model") or DEFAULT_MODEL)
        self.start_sse()
        sent_completed = False
        disconnected = False

        try:
            for event_name, data in read_stream_response(response):
                if not data:
                    continue
                if data == "[DONE]":
                    if not sent_completed:
                        if not self.write_sse_json(stream_done_chunk(state)):
                            disconnected = True
                            break
                        sent_completed = True
                    if not self.write_done():
                        disconnected = True
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                chunks = state.translate_event(event_name, event)
                for chunk in chunks:
                    if not self.write_sse_json(chunk):
                        disconnected = True
                        break
                if disconnected:
                    break
                if event_name == "response.completed" or event.get(
                    "type"
                ) == "response.completed":
                    sent_completed = True
                    if not self.write_done():
                        disconnected = True
                    break
        finally:
            close_quietly(response)

        if disconnected or sent_completed:
            return
        if not self.write_sse_json(stream_closed_chunk(state)):
            return
        self.write_done()


def create_server(
    config: GatewayConfig | None = None, port: int | None = None
) -> GatewayServer:
    if config is None:
        load_dotenv()
        config = config_from_env()
    elif port is not None:
        config = GatewayConfig(
            api_key=config.api_key,
            base_url=config.base_url,
            user_agent=config.user_agent,
            host=config.host,
            port=port,
            upstream_timeout=config.upstream_timeout,
            gateway_api_key=config.gateway_api_key,
        )
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
