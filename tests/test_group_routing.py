"""Tes routing per grup (tes 14, 15, 19)."""

import json

from .helpers import (
    GATEWAY_KEY,
    UPSTREAM_KEY,
    GatewayTestCase,
)


class GroupRoutingTests(GatewayTestCase):
    def test_14_group_routing_uses_group_key_not_env_key(self):
        store = self.gateway_server.keystore
        store.add_group("gpt", "GPT Publik", "responses")
        store.add_key("gpt", "test-group-key-bukan-asli", "utama")
        self.upstream.fixture["responses_payload"] = {
            "id": "resp_group",
            "output": [],
            "usage": {},
        }
        status, _, _ = self.request(
            "POST",
            "/gpt/v1/chat/completions",
            {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Halo"}]},
        )
        upstream_request = self.upstream.requests[-1]
        self.assertEqual(status, 200)
        self.assertEqual(
            upstream_request["headers"]["Authorization"],
            "Bearer test-group-key-bukan-asli",
        )

    def test_15_default_routing_still_uses_env_key(self):
        status, _, _ = self.post_chat(
            {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Halo"}]}
        )
        self.assertEqual(status, 200)
        upstream_request = self.upstream.requests[-1]
        self.assertEqual(
            upstream_request["headers"]["Authorization"],
            "Bearer " + UPSTREAM_KEY,
        )

    def test_19_messages_group_models_200_chat_501(self):
        store = self.gateway_server.keystore
        store.add_group("messages", "Pesan", "messages")
        store.add_key("messages", "test-messages-key-bukan-asli", "utama")
        status, _, _ = self.request(
            "GET", "/messages/v1/models", gateway_key=GATEWAY_KEY
        )
        self.assertEqual(status, 200)
        status, _, body = self.request(
            "POST",
            "/messages/v1/chat/completions",
            {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Halo"}]},
            gateway_key=GATEWAY_KEY,
        )
        self.assertEqual(status, 501)
        payload = json.loads(body)
        self.assertEqual(
            payload["error"]["code"], "protocol_not_implemented"
        )
