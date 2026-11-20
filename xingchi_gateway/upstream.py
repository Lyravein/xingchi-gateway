"""Satu-satunya modul yang menyentuh jaringan upstream.

Membangun request berheader dan menerjemahkan kegagalan HTTP/jaringan menjadi
UpstreamFailure terstruktur. Tidak menulis ke client dan tidak memetakan protokol.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import GatewayConfig

_NETWORK_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    ConnectionError,
    http.client.HTTPException,
    OSError,
)


@dataclass
class UpstreamFailure(Exception):
    status: int
    code: object
    message: str
    error_type: str = "upstream_error"


def upstream_headers(config: GatewayConfig, api_key: str | None = None) -> dict:
    secret = config.api_key if api_key is None else api_key
    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "User-Agent": config.user_agent,
    }


def upstream_url(config: GatewayConfig, path: str) -> str:
    return f"{config.base_url}{path}"


def open_upstream(
    config: GatewayConfig,
    method: str,
    path: str,
    body: bytes | None = None,
    api_key: str | None = None,
):
    request = urllib.request.Request(
        upstream_url(config, path),
        data=body,
        headers=upstream_headers(config, api_key),
        method=method,
    )
    return urllib.request.urlopen(request, timeout=config.upstream_timeout)


def _read_http_error(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read(2_000_000)
    except (OSError, http.client.IncompleteRead):
        return b""
    finally:
        exc.close()


def parse_error_body(body: bytes) -> tuple[str, str | None]:
    raw = body.decode("utf-8", "replace")
    message = ""
    code = None

    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None

    if isinstance(parsed, dict):
        nested_error = parsed.get("error")
        if isinstance(nested_error, dict):
            message_value = nested_error.get("message")
            code_value = nested_error.get("code")
            if isinstance(message_value, str):
                message = message_value
            if isinstance(code_value, str):
                code = code_value
        if not message:
            for key in ("message", "detail"):
                value = parsed.get(key)
                if isinstance(value, str):
                    message = value
                    break
                if isinstance(value, dict):
                    nested_message = value.get("message")
                    if isinstance(nested_message, str):
                        message = nested_message
                        break
        if code is None:
            code_value = parsed.get("code")
            if isinstance(code_value, str):
                code = code_value

    if not message:
        message = raw.strip() if raw.strip() else "Upstream request failed"
    message = message[:500]

    return message, code


def request_upstream(
    config: GatewayConfig,
    method: str,
    path: str,
    body: bytes | None = None,
    api_key: str | None = None,
):
    """Buka respons upstream; HTTPError/putus jaringan jadi UpstreamFailure."""
    try:
        return open_upstream(config, method, path, body, api_key)
    except urllib.error.HTTPError as exc:
        message, code = parse_error_body(_read_http_error(exc))
        raise UpstreamFailure(exc.code, code, message) from None
    except _NETWORK_ERRORS as exc:
        raise UpstreamFailure(
            502,
            "upstream_unreachable",
            "Upstream unreachable",
            error_type="upstream_unreachable",
        ) from exc


def read_stream_response(response):
    """Iterasi (event_name, data) dari respons SSE upstream."""
    event_name = ""
    data_lines: list[bytes] = []

    while True:
        try:
            line = response.readline()
        except (http.client.IncompleteRead, http.client.HTTPException, OSError):
            break
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line:
            if data_lines:
                try:
                    data = b"\n".join(data_lines).decode("utf-8", "replace")
                except Exception:
                    data = ""
                yield event_name, data
            event_name = ""
            data_lines = []
            continue
        if line.startswith(b":"):
            continue
        if line.startswith(b"event:"):
            event_name = line[len(b"event:"):].strip().decode(
                "utf-8", "replace"
            )
        elif line.startswith(b"data:"):
            data_lines.append(line[len(b"data:"):].strip())

    if data_lines:
        data = b"\n".join(data_lines).decode("utf-8", "replace")
        yield event_name, data
