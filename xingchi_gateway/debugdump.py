"""Dump body request/response ke disk saat debug nyala.

Menulis file JSON 0600 per request untuk investigasi manual, dijaga lock.
Tidak menyentuh jaringan, tidak mencatat secret, dan tidak boleh mengubah
respons ke client; kegagalan apa pun hanya ditulis ke stderr.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_DEFAULT_MAX_BODY = 262144

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )

def dump_enabled(config) -> bool:
    return bool(getattr(config, "debug_dump", False)) and bool(
        getattr(config, "debug_dir", "")
    )

def max_body(config) -> int:
    value = getattr(config, "debug_max_body", _DEFAULT_MAX_BODY)
    return value if isinstance(value, int) and value > 0 else _DEFAULT_MAX_BODY

def new_dump_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"

def clamp_text(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", "ignore"), True

def body_field(raw: bytes, max_bytes: int) -> tuple[object, bool]:
    """Body sebagai objek JSON bila valid; selain itu string yang di-clamp."""
    text, truncated = clamp_text(raw.decode("utf-8", "replace"), max_bytes)
    if not truncated:
        try:
            return json.loads(text), False
        except json.JSONDecodeError:
            pass
    return text, truncated

class StreamAccumulator:
    """Kumpulkan payload SSE sampai batas byte tanpa menahan stream."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.truncated = False
        self._parts: list[str] = []
        self._size = 0

    def add(self, payload: str) -> None:
        if self.truncated:
            return
        size = len(payload.encode("utf-8"))
        if self._size + size > self.max_bytes:
            self.truncated = True
            return
        self._parts.append(payload)
        self._size += size

    def text(self) -> str:
        return "\n".join(self._parts)

def write_dump(config, dump_id: str, kind: str, payload: dict) -> None:
    try:
        directory = getattr(config, "debug_dir", "") or ""
        if not directory:
            return
        with _LOCK:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            path = os.path.join(directory, f"{dump_id}-{kind}.json")
            fd, temporary = tempfile.mkstemp(prefix=".dump-", dir=directory)
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
    except Exception as exc:
        sys.stderr.write(f"debugdump: {exc}\n")
