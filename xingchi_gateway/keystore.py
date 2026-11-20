"""Penyimpanan dan pemilihan key upstream dengan hot reload.

Memiliki lock untuk file dan counter round-robin; tidak melakukan I/O jaringan.
Secret penuh hanya dikembalikan ke pemanggil upstream, bukan data tampilan.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

RESERVED_SLUGS = {"admin", "v1", "health"}
VALID_SLUG = re.compile(r"^[a-z0-9-]{1,32}$")
VALID_PROTOCOLS = {"responses", "messages", "chat"}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def validate_slug(slug: str) -> str | None:
    if not isinstance(slug, str) or not VALID_SLUG.fullmatch(slug):
        return "Slug must match [a-z0-9-]{1,32}"
    if slug in RESERVED_SLUGS:
        return "Slug is reserved"
    return None


def mask_secret(secret: str) -> str:
    return secret[-4:] if isinstance(secret, str) else ""


def _masked_key(key: dict) -> dict:
    secret = key.get("secret")
    return {
        "id": key.get("id", ""),
        "label": key.get("label", ""),
        "last4": mask_secret(secret) if isinstance(secret, str) else "",
        "added_at": key.get("added_at", ""),
    }


class KeyStore:
    def __init__(self, path: str = "keys.json"):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._data: dict = {"groups": []}
        self._signature: tuple[int, int] | None = None
        self._round_robin: dict[str, int] = {}

    def _file_signature(self) -> tuple[int, int] | None:
        try:
            stat = os.stat(self.path)
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _read_disk(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {"groups": []}
        if not isinstance(value, dict) or not isinstance(value.get("groups"), list):
            return {"groups": []}
        return value

    def reload_if_changed(self) -> dict:
        with self._lock:
            signature = self._file_signature()
            if signature == self._signature:
                return copy.deepcopy(self._data)
            self._data = self._read_disk()
            self._signature = signature
            return copy.deepcopy(self._data)

    def load(self) -> dict:
        return self.reload_if_changed()

    def save(self, data: dict) -> None:
        normalized = data if isinstance(data, dict) else {"groups": []}
        groups = normalized.get("groups")
        if not isinstance(groups, list):
            groups = []
        normalized = {"groups": groups}

        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".keys-", dir=directory)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
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

        with self._lock:
            self._data = copy.deepcopy(normalized)
            self._signature = self._file_signature()

    def list_groups(self) -> list[dict]:
        data = self.load()
        result = []
        for group in data["groups"]:
            if not isinstance(group, dict):
                continue
            result.append({
                "slug": group.get("slug", ""),
                "name": group.get("name", ""),
                "protocol": group.get("protocol", "responses"),
                "keys": [
                    _masked_key(key)
                    for key in group.get("keys", [])
                    if isinstance(key, dict)
                ],
            })
        return result

    def get_group(self, slug: str) -> dict | None:
        data = self.load()
        for group in data["groups"]:
            if isinstance(group, dict) and group.get("slug") == slug:
                return copy.deepcopy(group)
        return None

    def add_group(self, slug: str, name: str, protocol: str) -> dict:
        error = validate_slug(slug)
        if error:
            raise ValueError(error)
        if protocol not in VALID_PROTOCOLS:
            raise ValueError("Protocol must be responses, messages, or chat")
        with self._lock:
            data = self.load()
            for group in data["groups"]:
                if isinstance(group, dict) and group.get("slug") == slug:
                    raise ValueError("Group slug already exists")
            group = {
                "slug": slug,
                "name": name if isinstance(name, str) else str(name),
                "protocol": protocol,
                "keys": [],
            }
            data["groups"].append(group)
            self.save(data)
            return copy.deepcopy(group)

    def add_key(
        self,
        slug: str,
        secret: str,
        label: str = "",
        protocol: str | None = None,
    ) -> dict:
        if not isinstance(secret, str) or not secret:
            raise ValueError("Secret is required")
        with self._lock:
            data = self.load()
            for group in data["groups"]:
                if not isinstance(group, dict) or group.get("slug") != slug:
                    continue
                key = {
                    "id": "k" + uuid.uuid4().hex,
                    "secret": secret,
                    "label": label if isinstance(label, str) else "",
                    "added_at": utc_timestamp(),
                }
                keys = group.setdefault("keys", [])
                if not isinstance(keys, list):
                    keys = []
                    group["keys"] = keys
                if not keys and protocol in VALID_PROTOCOLS:
                    group["protocol"] = protocol
                keys.append(key)
                self.save(data)
                return copy.deepcopy(key)
            raise KeyError("Group not found")

    def remove_key(self, slug: str, key_id: str) -> bool:
        with self._lock:
            data = self.load()
            for group in data["groups"]:
                if not isinstance(group, dict) or group.get("slug") != slug:
                    continue
                keys = group.get("keys", [])
                if not isinstance(keys, list):
                    return False
                filtered = [
                    key for key in keys
                    if not (isinstance(key, dict) and key.get("id") == key_id)
                ]
                if len(filtered) == len(keys):
                    return False
                group["keys"] = filtered
                self.save(data)
                return True
            return False

    def remove_group(self, slug: str) -> bool:
        with self._lock:
            data = self.load()
            groups = data["groups"]
            filtered = [
                group for group in groups
                if not (isinstance(group, dict) and group.get("slug") == slug)
            ]
            if len(filtered) == len(groups):
                return False
            data["groups"] = filtered
            self.save(data)
            self._round_robin.pop(slug, None)
            return True

    def select_key(self, slug: str) -> dict | None:
        with self._lock:
            group = self.get_group(slug)
            if not group:
                return None
            keys = group.get("keys")
            if not isinstance(keys, list) or not keys:
                return None
            usable = [key for key in keys if isinstance(key, dict)]
            if not usable:
                return None
            index = self._round_robin.get(slug, 0) % len(usable)
            self._round_robin[slug] = index + 1
            return copy.deepcopy(usable[index])


def load_keys(path: str = "keys.json") -> dict:
    return KeyStore(path).load()


def save_keys(path: str, data: dict) -> None:
    KeyStore(path).save(data)
