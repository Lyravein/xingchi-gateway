"""Tes terjemahan Chat/Responses dan proxy dasar (tes 1..10)."""

import http.client
import json
import threading

from xingchi_gateway.config import DEFAULT_USER_AGENT, create_config
from xingchi_gateway.translate import DEFAULT_INSTRUCTIONS
from xingchi_gateway.server import create_server

from .helpers import (
    GATEWAY_KEY,
    UPSTREAM_KEY,
    GatewayTestCase,
)


class TranslateProxyTests(GatewayTestCase):
    def test_1_chat_request_translation(self):
        chat_request = {
            "model": "gpt-6.1-sol",
            "messages": [
                {"role": "system", "content": "Aturan satu"},
                {"role": "developer", "content": "Aturan dua"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Cuaca "},
                        {"type": "image_url", "image_url": {"url": "ignored"}},
                        {"type": "text", "text": "Tokyo"},
                    ],
                },
                {"role": "assistant", "content": "Saya cek dulu."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_weather",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Tokyo"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_weather",
                    "content": "Hujan",
                },
            ],
            "max_tokens": 321,
            "temperature": 0.25,
            "top_p": 0.75,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "stream": False,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "Cek cuaca",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                        "strict": True,
                    },
                }
            ],
        }

        self.upstream.fixture["responses_payload"] = {
            "id": "resp_fixture",
            "output": [],
            "usage": {},
        }
        status, _, _ = self.post_chat(chat_request)
        self.assertEqual(status, 200)

        upstream_request = self.upstream.requests[-1]
        encoded_body = upstream_request["body"]
        translated = json.loads(encoded_body.decode("utf-8"))

        self.assertEqual(upstream_request["method"], "POST")
        self.assertEqual(upstream_request["path"], "/v1/responses")
        self.assertEqual(
            translated["instructions"], "Aturan satu\n\nAturan dua"
        )
        self.assertEqual(
            translated["input"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Cuaca Tokyo"}
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Saya cek dulu."}
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call_weather",
                    "name": "weather",
                    "arguments": '{"city":"Tokyo"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_weather",
                    "output": "Hujan",
                },
            ],
        )
        self.assertEqual(translated["max_output_tokens"], 321)
        self.assertEqual(translated["temperature"], 0.25)
        self.assertEqual(translated["top_p"], 0.75)
        self.assertEqual(translated["tool_choice"], "auto")
        self.assertIs(translated["parallel_tool_calls"], True)
        self.assertIs(translated["stream"], False)
        self.assertEqual(
            translated["tools"],
            [
                {
                    "type": "function",
                    "name": "weather",
                    "description": "Cek cuaca",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                    "strict": True,
                }
            ],
        )

    def test_2_upstream_headers(self):
        self.upstream.fixture["responses_payload"] = {
            "id": "resp_headers",
            "output": [],
            "usage": {},
        }
        status, _, _ = self.post_chat(
            {
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "Hai"}],
            }
        )
        self.assertEqual(status, 200)

        headers = self.upstream.requests[-1]["headers"]
        self.assertEqual(
            headers.get("Authorization"), f"Bearer {UPSTREAM_KEY}"
        )
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(
            headers.get("User-Agent"), DEFAULT_USER_AGENT
        )

    def test_3_text_response_usage_and_no_instructions(self):
        self.upstream.fixture["responses_payload"] = {
            "id": "resp_text",
            "object": "response",
            "created_at": 1710000001,
            "model": "gpt-6.1-sol",
            "instructions": "JANGAN_KIRIM_INI",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "instructions": "JANGAN_KIRIM_INI_DALAM_MESSAGE",
                    "content": [
                        {"type": "output_text", "text": "Halo "},
                        {"type": "output_text", "text": "dunia"},
                        {"type": "refusal", "refusal": "abaikan"},
                    ],
                }
            ],
            "usage": {
                "input_tokens": 18307,
                "output_tokens": 25,
                "total_tokens": 18332,
            },
        }

        status, _, response_body = self.post_chat(
            {
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "Halo"}],
            }
        )
        self.assertEqual(status, 200)
        payload = self.parse_json(response_body)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["id"], "resp_text")
        self.assertEqual(payload["created"], 1710000001)
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertEqual(payload["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(payload["choices"][0]["message"]["content"], "Halo dunia")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            payload["usage"],
            {
                "prompt_tokens": 18307,
                "completion_tokens": 25,
                "total_tokens": 18332,
            },
        )
        self.assertNotIn("instructions", json.dumps(payload))
        self.assertNotIn("JANGAN_KIRIM_INI", json.dumps(payload))

    def test_4_function_call_response(self):
        self.upstream.fixture["responses_payload"] = {
            "id": "resp_tool",
            "created_at": 1710000002,
            "model": "gpt-6.1-sol",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_weather",
                    "name": "weather",
                    "arguments": '{"city":"Tokyo"}',
                }
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 12,
                "total_tokens": 112,
            },
        }

        status, _, response_body = self.post_chat(
            {
                "messages": [{"role": "user", "content": "Cuaca Tokyo?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            }
        )
        self.assertEqual(status, 200)
        payload = self.parse_json(response_body)
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(
            choice["message"]["tool_calls"],
            [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "arguments": '{"city":"Tokyo"}',
                    },
                }
            ],
        )

    def test_5_streaming_sse_chunk_order(self):
        self.upstream.fixture["responses_content_type"] = "text/event-stream"
        self.upstream.fixture["responses_status"] = 200
        self.upstream.fixture["responses_events"] = [
            {
                "type": "response.output_text.delta",
                "data": {
                    "type": "response.output_text.delta",
                    "delta": "Cek ",
                },
            },
            {
                "type": "response.output_item.added",
                "data": {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "fc_item_1",
                        "call_id": "call_weather",
                        "type": "function_call",
                        "name": "weather",
                        "arguments": "",
                    },
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "data": {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_item_1",
                    "delta": '{"city":',
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "data": {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_item_1",
                    "delta": '"Tokyo"}',
                },
            },
            {
                "type": "response.completed",
                "data": {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_stream",
                        "created_at": 1710000003,
                        "model": "gpt-6.1-sol",
                        "status": "completed",
                        "usage": {
                            "input_tokens": 31,
                            "output_tokens": 9,
                            "total_tokens": 40,
                        },
                    },
                },
            },
        ]

        status, content_type, response_body = self.post_chat(
            {
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "Cuaca Tokyo"}],
                "stream": True,
            }
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", content_type)
        chunks, done = self.parse_sse(response_body)

        self.assertTrue(done)
        self.assertEqual(
            chunks[0]["choices"][0]["delta"],
            {"role": "assistant", "content": "Cek "},
        )
        self.assertEqual(
            chunks[1]["choices"][0]["delta"]["tool_calls"],
            [
                {
                    "index": 0,
                    "id": "call_weather",
                    "type": "function",
                    "function": {"name": "weather", "arguments": ""},
                }
            ],
        )
        self.assertEqual(
            chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"][
                "arguments"
            ],
            '{"city":',
        )
        self.assertEqual(
            chunks[3]["choices"][0]["delta"]["tool_calls"][0]["function"][
                "arguments"
            ],
            '"Tokyo"}',
        )
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(
            chunks[-1]["usage"],
            {
                "prompt_tokens": 31,
                "completion_tokens": 9,
                "total_tokens": 40,
            },
        )

    def test_6_upstream_403_error_is_forwarded(self):
        self.upstream.fixture["responses_status"] = 403
        self.upstream.fixture["responses_payload"] = {
            "error": {
                "message": "This client is not allowed",
                "type": "forbidden",
                "code": "client_not_allowed",
            }
        }

        status, _, response_body = self.request(
            "POST",
            "/v1/responses",
            {"model": "gpt-6.1-sol", "input": "Hello"},
        )
        self.assertEqual(status, 403)
        payload = self.parse_json(response_body)
        self.assertEqual(
            payload["error"]["message"], "This client is not allowed"
        )
        self.assertEqual(payload["error"]["type"], "upstream_error")
        self.assertEqual(payload["error"]["code"], "client_not_allowed")

    def test_7_gateway_api_key_required(self):
        for gateway_key in (None, "wrong-key-bukan-asli"):
            with self.subTest(gateway_key=gateway_key):
                status, _, response_body = self.post_chat(
                    {
                        "messages": [{"role": "user", "content": "Hello"}],
                    },
                    gateway_key=gateway_key,
                )
                self.assertEqual(status, 401)
                payload = self.parse_json(response_body)
                self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_8_get_models_success_and_fallback(self):
        status, _, response_body = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        payload = self.parse_json(response_body)
        self.assertEqual(
            payload,
            {
                "object": "list",
                "data": [
                    {
                        "id": "gpt-6.1-sol",
                        "object": "model",
                        "owned_by": "xingchi",
                    }
                ],
            },
        )

        fallback_config = create_config(
            api_key=UPSTREAM_KEY,
            base_url="http://127.0.0.1:1",
            host="127.0.0.1",
            port=8789,
            upstream_timeout=0.2,
            gateway_api_key=GATEWAY_KEY,
        )
        fallback_server = create_server(fallback_config, port=0)
        fallback_thread = threading.Thread(
            target=fallback_server.serve_forever, daemon=True
        )
        fallback_thread.start()
        try:
            fallback_port = fallback_server.server_address[1]
            connection = http.client.HTTPConnection(
                "127.0.0.1", fallback_port, timeout=3
            )
            connection.request(
                "GET",
                "/v1/models",
                headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
            )
            response = connection.getresponse()
            fallback_status = response.status
            fallback_body = response.read()
            connection.close()
            fallback_payload = json.loads(fallback_body.decode("utf-8"))
            self.assertEqual(fallback_status, 502)
            self.assertEqual(
                fallback_payload,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "gpt-6.1-sol",
                            "object": "model",
                            "owned_by": "xingchi",
                        }
                    ],
                },
            )
        finally:
            fallback_server.shutdown()
            fallback_server.server_close()
            fallback_thread.join(timeout=2)

    def test_9_non_string_upstream_error_code_is_dropped(self):
        self.upstream.fixture["responses_status"] = 422
        self.upstream.fixture["responses_payload"] = {
            "error": {
                "message": "Kode error berbentuk objek",
                "code": {"kode": "aneh", "nomor": 7},
            }
        }

        status, _, response_body = self.request(
            "POST",
            "/v1/responses",
            {"model": "gpt-6.1-sol", "input": "Hello"},
        )
        self.assertEqual(status, 422)
        payload = self.parse_json(response_body)
        self.assertEqual(
            payload["error"]["message"], "Kode error berbentuk objek"
        )
        self.assertEqual(payload["error"]["type"], "upstream_error")
        self.assertNotIsInstance(payload["error"].get("code"), (dict, list, int))
        self.assertTrue(
            payload["error"].get("code") is None
            or isinstance(payload["error"]["code"], str)
        )

    def test_10_default_instructions_without_system_message(self):
        self.upstream.fixture["responses_payload"] = {
            "id": "resp_no_system",
            "output": [],
            "usage": {},
        }
        status, _, _ = self.post_chat(
            {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "Halo"}]}
        )
        self.assertEqual(status, 200)
        translated = json.loads(
            self.upstream.requests[-1]["body"].decode("utf-8")
        )
        self.assertEqual(translated["instructions"], DEFAULT_INSTRUCTIONS)
        self.assertEqual(translated["instructions"], "You are a helpful assistant.")

        self.post_chat(
            {
                "model": "gpt-6.1-sol",
                "messages": [
                    {"role": "system", "content": "Aturan khusus"},
                    {"role": "user", "content": "Halo"},
                ],
            }
        )
        translated = json.loads(
            self.upstream.requests[-1]["body"].decode("utf-8")
        )
        self.assertEqual(translated["instructions"], "Aturan khusus")
