"""Rute GUI dan API administrasi tanpa dependency eksternal.

Memuat halaman dari admin.html, membaca/mengubah keystore, dan memanggil
upstream hanya untuk validasi key. Tidak menulis secret ke log.
"""

from __future__ import annotations

import hmac
import json
from http.client import HTTPException
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import stats
from .config import DEFAULT_MODEL
from .keystore import (
    RESERVED_SLUGS,
    KeyStore,
    VALID_PROTOCOLS,
    validate_slug,
)
from .upstream import (
    UpstreamFailure,
    request_upstream,
)

_ADMIN_PAGE_PATH = Path(__file__).with_name("admin.html")


def _load_admin_page() -> bytes | None:
    try:
        return _ADMIN_PAGE_PATH.read_bytes()
    except OSError:
        return None


ADMIN_PAGE = _load_admin_page()


def authorized(handler) -> bool:
    configured = handler.config.admin_token
    if not configured:
        return False
    authorization = handler.headers.get("Authorization", "")
    expected = f"Bearer {configured}"
    try:
        return hmac.compare_digest(
            authorization.encode("utf-8"), expected.encode("utf-8")
        )
    except AttributeError:
        return False


def handle_admin(handler, keystore: KeyStore) -> bool:
    path = urlsplit(handler.path).path
    if not path.startswith("/admin"):
        return False
    if not handler.config.admin_token:
        handler.send_error_body(404, "not_found", "not_found", "Not Found")
        return True
    if handler.command == "GET" and path == "/admin":
        if ADMIN_PAGE is None:
            handler.send_error_body(
                500,
                "server_error",
                "admin_page_missing",
                "Admin page file is missing",
            )
            return True
        body = ADMIN_PAGE
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.write_bytes(body)
        return True
    if not authorized(handler):
        handler.send_error_body(
            401, "unauthorized", "unauthorized", "Unauthorized"
        )
        return True
    if handler.command == "GET" and path == "/admin/api/groups":
        handler.send_json(200, {"groups": keystore.list_groups()})
        return True
    if handler.command == "GET" and path == "/admin/api/stats":
        if not handler.config.stats_file:
            handler.send_json(200, {"enabled": False})
            return True
        handler.send_json(200, stats.summarize(handler.config))
        return True
    if handler.command == "POST" and path == "/admin/api/groups":
        create_group(handler, keystore)
        return True

    parts = path.split("/")
    if len(parts) >= 5 and parts[:4] == ["", "admin", "api", "groups"]:
        slug = unquote(parts[4])
        if len(parts) == 5 and handler.command == "DELETE":
            delete_group(handler, keystore, slug)
            return True
        if len(parts) == 7 and parts[5] == "keys" and handler.command == "DELETE":
            delete_key(handler, keystore, slug, unquote(parts[6]))
            return True
        if len(parts) == 6 and parts[5] == "keys" and handler.command == "POST":
            add_key(handler, keystore, slug)
            return True

    handler.send_not_found()
    return True


def parse_admin_body(handler) -> dict | None:
    body = handler.read_request_body()
    try:
        value = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def create_group(handler, keystore: KeyStore) -> None:
    value = parse_admin_body(handler)
    if value is None:
        handler.send_invalid_json()
        return
    slug = value.get("slug")
    name = value.get("name")
    protocol = value.get("protocol")
    slug_error = validate_slug(slug) if isinstance(slug, str) else "Slug is required"
    if slug_error:
        handler.send_error_body(400, "invalid_request", "invalid_slug", slug_error)
        return
    if protocol not in VALID_PROTOCOLS:
        handler.send_error_body(
            400, "invalid_request", "invalid_protocol",
            "Protocol must be responses, messages, or chat",
        )
        return
    if not isinstance(name, str) or not name.strip():
        handler.send_error_body(
            400, "invalid_request", "invalid_name", "Name is required"
        )
        return
    try:
        group = keystore.add_group(slug, name, protocol)
    except ValueError as exc:
        handler.send_error_body(400, "invalid_request", "invalid_group", str(exc))
        return
    handler.send_json(200, {"slug": slug, "group": {
        "slug": group["slug"], "name": group["name"],
        "protocol": group["protocol"], "keys": [],
    }})


def validation_rejected(failure: UpstreamFailure) -> bool:
    message = (failure.message or "").lower()
    code = str(failure.code or "").lower()
    if failure.status == 400 and code == "chat_completions_disabled":
        return True
    if failure.status == 403:
        if "does not allow" in message or "dispatch" in message or "dispatch" in code:
            return True
    return False


def validate_models(handler, secret: str) -> tuple[int, dict | None]:
    try:
        response = request_upstream(
            handler.config, "GET", "/v1/models", api_key=secret
        )
        body = response.read()
        status = response.status
        response.close()
    except UpstreamFailure as failure:
        handler.send_failure(failure)
        return 0, None
    except (HTTPException, OSError, TimeoutError):
        handler.send_network_error()
        return 0, None
    if status != 200:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        message = "Upstream validation failed"
        code = "upstream_error"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message", message)
                code = error.get("code", code)
        handler.send_error_body(status, "upstream_error", code, str(message)[:500])
        return 0, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0, {}
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return len(data) if isinstance(data, list) else 0, payload


def detect_protocol(handler, secret: str) -> tuple[str, bool]:
    probes = (
        ("responses", "/v1/responses", {"model": DEFAULT_MODEL, "input": "hi", "max_output_tokens": 1}),
        ("messages", "/v1/messages", {"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}),
        ("chat", "/v1/chat/completions", {"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}),
    )
    all_rejected = True
    for protocol, path, payload in probes:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            response = request_upstream(
                handler.config, "POST", path, body, api_key=secret
            )
            response.close()
            all_rejected = False
            return protocol, True
        except UpstreamFailure as failure:
            if not validation_rejected(failure):
                all_rejected = False
                return protocol, True
    return "responses", False


def add_key(handler, keystore: KeyStore, slug: str) -> None:
    group = keystore.get_group(slug)
    if not group:
        handler.send_error_body(404, "not_found", "not_found", "Group not found")
        return
    value = parse_admin_body(handler)
    if value is None:
        handler.send_invalid_json()
        return
    secret = value.get("secret")
    label = value.get("label", "")
    if not isinstance(secret, str) or not secret:
        handler.send_error_body(
            400, "invalid_request", "invalid_key", "Secret is required"
        )
        return
    override = value.get("protocol")
    if override not in (None, "", *VALID_PROTOCOLS):
        handler.send_error_body(
            400, "invalid_request", "invalid_protocol",
            "Protocol must be responses, messages, or chat",
        )
        return

    model_count, _ = validate_models(handler, secret)
    if model_count == 0:
        response_started = getattr(handler, "_response_status", None)
        if response_started is not None:
            return
    else:
        pass

    if override:
        protocol, detected = override, False
    else:
        protocol, detected = detect_protocol(handler, secret)

    try:
        key = keystore.add_key(
            slug, secret, label,
            protocol if not group.get("keys") else None,
        )
    except (KeyError, ValueError) as exc:
        handler.send_error_body(400, "invalid_request", "invalid_key", str(exc))
        return
    last4 = secret[-4:] if len(secret) >= 4 else secret
    handler.send_json(200, {
        "slug": slug,
        "key": {
            "id": key["id"], "label": key["label"],
            "last4": last4, "added_at": key["added_at"],
        },
        "model_count": model_count,
        "protocol": protocol,
        "detected": detected,
    })


def delete_key(handler, keystore: KeyStore, slug: str, key_id: str) -> None:
    if keystore.remove_key(slug, key_id):
        handler.send_json(200, {"deleted": True})
    else:
        handler.send_error_body(404, "not_found", "not_found", "Key or group not found")


def delete_group(handler, keystore: KeyStore, slug: str) -> None:
    if keystore.remove_group(slug):
        handler.send_json(200, {"deleted": True})
    else:
        handler.send_error_body(404, "not_found", "not_found", "Group not found")
