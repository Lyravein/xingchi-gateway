"""Mixin I/O respons untuk GatewayHandler.

Menulis body/JSON/SSE ke client dan membaca request body.
Tidak memilih rute, tidak memanggil upstream, tidak menangani auth.
"""

from __future__ import annotations

import http.client
import json

from .upstream import UpstreamFailure

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, OSError)


class HandlerIO:
    """Potongan handler yang hanya berbicara dengan socket client."""

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

    def send_raw(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.write_bytes(body)

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

    def start_sse(self, status: int = 200, content_type: str = "text/event-stream"):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
