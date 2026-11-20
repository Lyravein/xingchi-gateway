"""Konfigurasi gateway dari environment.

Memuat nilai env, memberi default, dan memvalidasi batasannya.
Tidak membuka koneksi jaringan atau menjalankan server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.xingchiapi.com"
DEFAULT_USER_AGENT = "codex_cli_rs/0.77.0"
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_DEBUG_MAX_BODY = 262144
_TRUE_VALUES = {"1", "true", "yes"}

@dataclass(frozen=True)
class GatewayConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    user_agent: str = DEFAULT_USER_AGENT
    host: str = "127.0.0.1"
    port: int = 8789
    upstream_timeout: float = 300.0
    gateway_api_key: str = ""
    keys_file: str = "keys.json"
    admin_token: str = ""
    debug_dump: bool = False
    debug_dir: str = ""
    debug_max_body: int = DEFAULT_DEBUG_MAX_BODY
    stats_file: str = ""


def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _validate_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise ValueError("GATEWAY_PORT must be between 1 and 65535")
    return port


def _validate_timeout(timeout: float) -> float:
    if timeout <= 0:
        raise ValueError("GATEWAY_UPSTREAM_TIMEOUT must be greater than zero")
    return timeout


def _normalize_base_url(value: str) -> str:
    return value.rstrip("/") or DEFAULT_BASE_URL


def _normalize_user_agent(value: str) -> str:
    return value or DEFAULT_USER_AGENT

def _sibling_path(keys_file: str, name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(keys_file)), name)

def _resolve_debug_dir(value: str | None, keys_file: str) -> str:
    if value is None:
        value = os.environ.get("GATEWAY_DEBUG_DIR", "")
    return value.strip() or _sibling_path(keys_file, "debug")

def _resolve_stats_file(value: str | None, keys_file: str) -> str:
    if value is None:
        value = os.environ.get("GATEWAY_STATS_FILE", "")
    value = value.strip()
    if not value:
        return _sibling_path(keys_file, "stats.jsonl")
    return "" if value.lower() == "off" else value

def _env_flag(explicit: bool | None, name: str) -> bool:
    if explicit is not None:
        return bool(explicit)
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES

def _env_positive_int(explicit, name: str, default: int) -> int:
    raw = explicit if explicit is not None else os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def config_from_env() -> GatewayConfig:
    api_key = os.environ.get("XINGCHI_API_KEY", "")
    if not api_key.strip():
        raise ValueError("XINGCHI_API_KEY is required")

    base_url = os.environ.get("XINGCHI_BASE_URL", DEFAULT_BASE_URL).strip()
    if not base_url:
        base_url = DEFAULT_BASE_URL
    base_url = base_url.rstrip("/")

    user_agent = os.environ.get("XINGCHI_USER_AGENT", DEFAULT_USER_AGENT).strip()
    if not user_agent:
        user_agent = DEFAULT_USER_AGENT

    host = os.environ.get("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"

    try:
        port = int(os.environ.get("GATEWAY_PORT", "8789"))
    except ValueError as exc:
        raise ValueError("GATEWAY_PORT must be an integer") from exc
    _validate_port(port)

    try:
        upstream_timeout = float(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT", "300"))
    except ValueError as exc:
        raise ValueError("GATEWAY_UPSTREAM_TIMEOUT must be a number") from exc
    _validate_timeout(upstream_timeout)

    gateway_api_key = os.environ.get("GATEWAY_API_KEY", "").strip()
    keys_file = os.environ.get("GATEWAY_KEYS_FILE", "keys.json").strip() or "keys.json"
    admin_token = os.environ.get("GATEWAY_ADMIN_TOKEN", "").strip()

    return GatewayConfig(
        api_key=api_key,
        base_url=base_url,
        user_agent=user_agent,
        host=host,
        port=port,
        upstream_timeout=upstream_timeout,
        gateway_api_key=gateway_api_key,
        keys_file=keys_file,
        admin_token=admin_token,
        debug_dump=_env_flag(None, "GATEWAY_DEBUG_DUMP"),
        debug_dir=_resolve_debug_dir(None, keys_file),
        debug_max_body=_env_positive_int(
            None, "GATEWAY_DEBUG_MAX_BODY", DEFAULT_DEBUG_MAX_BODY
        ),
        stats_file=_resolve_stats_file(None, keys_file),
    )


def _env_port(explicit: int | None) -> int:
    if explicit is None:
        try:
            port = int(os.environ.get("GATEWAY_PORT", "8789"))
        except ValueError as exc:
            raise ValueError("GATEWAY_PORT must be an integer") from exc
    else:
        port = int(explicit)
    return _validate_port(port)


def _env_timeout(explicit: float | None) -> float:
    if explicit is None:
        try:
            timeout = float(os.environ.get("GATEWAY_UPSTREAM_TIMEOUT", "300"))
        except ValueError as exc:
            raise ValueError("GATEWAY_UPSTREAM_TIMEOUT must be a number") from exc
    else:
        timeout = float(explicit)
    return _validate_timeout(timeout)


def _env_value(explicit: str | None, name: str, default: str) -> str:
    if explicit is None:
        return os.environ.get(name, default)
    return explicit


def create_config(
    api_key: str | None = None,
    base_url: str | None = None,
    user_agent: str | None = None,
    host: str | None = None,
    port: int | None = None,
    upstream_timeout: float | None = None,
    gateway_api_key: str | None = None,
    keys_file: str | None = None,
    admin_token: str | None = None,
    debug_dump: bool | None = None,
    debug_dir: str | None = None,
    debug_max_body: int | None = None,
    stats_file: str | None = None,
) -> GatewayConfig:
    env_api_key = os.environ.get("XINGCHI_API_KEY", "")
    resolved_api_key = env_api_key if api_key is None else api_key
    if not resolved_api_key or not resolved_api_key.strip():
        raise ValueError("XINGCHI_API_KEY is required")

    resolved_keys_file = (
        os.environ.get("GATEWAY_KEYS_FILE", "keys.json").strip() or "keys.json"
        if keys_file is None
        else keys_file.strip() or "keys.json"
    )

    return GatewayConfig(
        api_key=resolved_api_key,
        base_url=_normalize_base_url(
            _env_value(base_url, "XINGCHI_BASE_URL", DEFAULT_BASE_URL).strip()
        ),
        user_agent=_normalize_user_agent(
            _env_value(
                user_agent, "XINGCHI_USER_AGENT", DEFAULT_USER_AGENT
            ).strip()
        ),
        host=_env_value(host, "GATEWAY_HOST", "127.0.0.1").strip()
        or "127.0.0.1",
        port=_env_port(port),
        upstream_timeout=_env_timeout(upstream_timeout),
        gateway_api_key=(
            os.environ.get("GATEWAY_API_KEY", "").strip()
            if gateway_api_key is None
            else gateway_api_key.strip()
        ),
        keys_file=resolved_keys_file,
        admin_token=(
            os.environ.get("GATEWAY_ADMIN_TOKEN", "").strip()
            if admin_token is None
            else admin_token.strip()
        ),
        debug_dump=_env_flag(debug_dump, "GATEWAY_DEBUG_DUMP"),
        debug_dir=_resolve_debug_dir(debug_dir, resolved_keys_file),
        debug_max_body=_env_positive_int(
            debug_max_body, "GATEWAY_DEBUG_MAX_BODY", DEFAULT_DEBUG_MAX_BODY
        ),
        stats_file=_resolve_stats_file(stats_file, resolved_keys_file),
    )
