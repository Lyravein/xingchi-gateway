"""Tes penyimpanan key (tes 11..13)."""

import os
import stat

from xingchi_gateway.keystore import KeyStore

from .helpers import GatewayTestCase


class KeystoreTests(GatewayTestCase):
    def test_11_keystore_save_load_and_permission(self):
        path = os.path.join(self.keys_dir.name, "keystore-test.json")
        store = KeyStore(path)
        data = {
            "groups": [{
                "slug": "gpt",
                "name": "GPT",
                "protocol": "responses",
                "keys": [{
                    "id": "k1",
                    "secret": "test-key-bukan-asli",
                    "label": "utama",
                    "added_at": "2026-11-20T04:00:00Z",
                }],
            }]
        }
        store.save(data)
        self.assertEqual(store.load(), data)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_12_keystore_hot_reload_from_disk(self):
        path = os.path.join(self.keys_dir.name, "hot-reload.json")
        store = KeyStore(path)
        store.save({
            "groups": [{
                "slug": "lama",
                "name": "Lama",
                "protocol": "responses",
                "keys": [],
            }]
        })
        writer = KeyStore(path)
        writer.save({
            "groups": [{
                "slug": "baru",
                "name": "Baru",
                "protocol": "chat",
                "keys": [],
            }]
        })
        loaded = store.load()
        self.assertEqual(loaded["groups"][0]["slug"], "baru")

    def test_13_keystore_round_robin_alternates_keys(self):
        store = KeyStore(os.path.join(self.keys_dir.name, "round-robin.json"))
        store.add_group("gpt", "GPT", "responses")
        first = store.add_key("gpt", "test-key-bukan-asli", "satu")
        second = store.add_key("gpt", "test-key-bukan-asli-2", "dua")
        picked = [
            store.select_key("gpt")["id"],
            store.select_key("gpt")["id"],
        ]
        self.assertEqual(picked, [first["id"], second["id"]])
