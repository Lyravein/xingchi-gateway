"""Statistik usage per request dalam file JSONL plus agregat untuk admin.

Mencatat satu baris per request model selesai dan membaca ringkasannya.
Tidak menyentuh jaringan, tidak mencatat secret, dan kegagalan tulis hanya
dilaporkan ke stderr.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import date, datetime, timedelta, timezone

_LOCK = threading.Lock()
MAX_BYTES = 5_000_000
RECENT_LIMIT = 20
_ZERO = {"count": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0}

def _path(config) -> str:
    value = getattr(config, "stats_file", "")
    return value if isinstance(value, str) else ""

def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            return
    except OSError:
        return
    os.replace(path, path + ".1")

def record(config, entry: dict) -> None:
    path = _path(config)
    if not path:
        return
    try:
        line = json.dumps(
            entry, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        with _LOCK:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            _rotate(path)
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
    except Exception as exc:
        sys.stderr.write(f"stats: {exc}\n")

def _entries(path: str) -> list[dict]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries

def _entry_date(entry: dict) -> date | None:
    stamp = entry.get("ts")
    if not isinstance(stamp, str):
        return None
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None

def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0

def _add(bucket: dict, entry: dict) -> None:
    group = entry.get("group")
    group = group if isinstance(group, str) and group else "default"
    model = entry.get("model")
    model = model if isinstance(model, str) else ""
    for target, name in ((bucket["groups"], group), (bucket["models"], model)):
        row = target.setdefault(name, dict(_ZERO))
        row["count"] += 1
        status = entry.get("status")
        if isinstance(status, int) and status >= 400:
            row["errors"] += 1
        row["prompt_tokens"] += _int(entry.get("prompt_tokens"))
        row["completion_tokens"] += _int(entry.get("completion_tokens"))

def summarize(config, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    today = current.date()
    week_start = today - timedelta(days=6)
    buckets = {
        "today": {"groups": {}, "models": {}},
        "last_7_days": {"groups": {}, "models": {}},
    }
    recent = []
    for entry in _entries(_path(config)):
        day = _entry_date(entry)
        if day is None or day > today:
            continue
        if day == today:
            _add(buckets["today"], entry)
        if day >= week_start:
            _add(buckets["last_7_days"], entry)
        recent.append(entry)
    recent.sort(key=lambda item: item.get("ts") or "", reverse=True)
    return {
        "enabled": bool(_path(config)),
        "today": buckets["today"],
        "last_7_days": buckets["last_7_days"],
        "recent": recent[:RECENT_LIMIT],
    }
