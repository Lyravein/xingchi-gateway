"""Mixin proxy dan streaming untuk GatewayHandler.

Meneruskan request ke upstream, menerjemahkan respons, dan mengalirkan SSE,
serta mencatat dump debug dan statistik usage bila diaktifkan.
Tidak memilih rute atau memeriksa auth gateway.
"""

from __future__ import annotations

import http.client
import json
import time

from . import debugdump, stats
from .config import DEFAULT_MODEL
from .server import close_quietly, sanitize_json_body
from .translate import (
    ChatStreamState,
    strip_instructions,
    stream_closed_chunk,
    stream_done_chunk,
    translate_chat_request,
    translate_responses_to_chat,
)
from .upstream import (
    UpstreamFailure,
    parse_error_body,
    read_stream_response,
    request_upstream,
)

MODELS_FALLBACK = {
    "object": "list",
    "data": [
        {
            "id": DEFAULT_MODEL,
            "object": "model",
            "owned_by": "xingchi",
        }
    ],
}

_READ_ERRORS = (http.client.HTTPException, http.client.IncompleteRead, OSError)

def _tokens(usage, prompt_key: str, completion_key: str):
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get(prompt_key)
    completion = usage.get(completion_key)
    return (
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
    )

class RequestTrace:
    """State observasi satu request model: konteks rute, id dump, waktu, token."""

    def __init__(self, config, context: dict):
        self.config = config
        self.context = context
        self.dump_id = (
            debugdump.new_dump_id() if debugdump.dump_enabled(config) else ""
        )
        self.max_body = debugdump.max_body(config)
        self.sse = (
            debugdump.StreamAccumulator(self.max_body) if self.dump_id else None
        )
        self.started = time.monotonic()
        self.model = ""
        self.stream = False
        self.prompt_tokens = None
        self.completion_tokens = None

class ProxyMixin:
    """Potongan handler yang berbicara ke upstream."""

    def proxy_models(self, api_key: str | None = None) -> None:
        try:
            response = request_upstream(
                self.config, "GET", "/v1/models", api_key=api_key
            )
        except UpstreamFailure as failure:
            if failure.error_type == "upstream_error":
                self.send_failure(failure)
                return
            self.send_json(502, MODELS_FALLBACK)
            return

        try:
            status = response.status
            body = response.read()
            content_type = response.headers.get(
                "Content-Type", "application/json"
            )
        except _READ_ERRORS:
            self.send_network_error()
            return
        finally:
            close_quietly(response)

        if status >= 400:
            message, code = parse_error_body(body)
            self.send_error_body(status, "upstream_error", code, message)
            return

        self.send_raw(status, content_type, body)

    def request_context(self, endpoint: str) -> dict:
        context = getattr(self, "_proxy_context", None)
        if not isinstance(context, dict):
            context = {}
        label = context.get("key_label")
        return {
            "group": context.get("group") or "default",
            "key_label": label if isinstance(label, str) else "env",
            "endpoint": context.get("endpoint") or endpoint,
            "route": self.request_path(),
        }

    def start_trace(self, endpoint: str) -> RequestTrace:
        return RequestTrace(self.config, self.request_context(endpoint))

    def finish_trace(self, trace: RequestTrace) -> None:
        duration = time.monotonic() - trace.started
        stats.record(trace.config, {
            "ts": debugdump.utc_now(),
            "route": trace.context["route"],
            "group": trace.context["group"],
            "endpoint": trace.context["endpoint"],
            "model": trace.model,
            "status": getattr(self, "_response_status", None),
            "stream": trace.stream,
            "duration_ms": int(duration * 1000),
            "prompt_tokens": trace.prompt_tokens,
            "completion_tokens": trace.completion_tokens,
            "key_label": trace.context["key_label"],
        })

    def dump_inbound(self, trace: RequestTrace, raw_body: bytes) -> None:
        if not trace.dump_id:
            return
        body, truncated = debugdump.body_field(raw_body, trace.max_body)
        payload = {
            "ts": debugdump.utc_now(),
            "route": trace.context["route"],
            "endpoint": trace.context["endpoint"],
            "model": trace.model,
            "body": body,
        }
        if truncated:
            payload["truncated"] = True
        debugdump.write_dump(trace.config, trace.dump_id, "inbound", payload)

    def dump_upstream(
        self, trace: RequestTrace, api_key: str | None, path: str, raw: bytes
    ) -> None:
        if not trace.dump_id:
            return
        secret = trace.config.api_key if api_key is None else api_key
        body, truncated = debugdump.body_field(raw, trace.max_body)
        payload = {
            "ts": debugdump.utc_now(),
            "route": trace.context["route"],
            "endpoint": trace.context["endpoint"],
            "model": trace.model,
            "key_label": trace.context["key_label"],
            "key_last4": secret[-4:] if isinstance(secret, str) else "",
            "path": path,
            "body": body,
        }
        if truncated:
            payload["truncated"] = True
        debugdump.write_dump(trace.config, trace.dump_id, "upstream", payload)

    def dump_response(
        self, trace: RequestTrace, body=None, stream: bool = False,
        truncated: bool = False,
    ) -> None:
        if not trace.dump_id:
            return
        if stream and trace.sse is not None:
            body = trace.sse.text()
            truncated = trace.sse.truncated
        payload = {
            "ts": debugdump.utc_now(),
            "route": trace.context["route"],
            "endpoint": trace.context["endpoint"],
            "status": getattr(self, "_response_status", None),
            "stream": stream,
            "body": body,
        }
        if truncated:
            payload["truncated"] = True
        debugdump.write_dump(trace.config, trace.dump_id, "response", payload)

    def dump_body(self, trace: RequestTrace, raw: bytes):
        if not trace.dump_id:
            return None, False
        return debugdump.body_field(raw, trace.max_body)

    def accumulate_sse(self, trace: RequestTrace, data: str) -> None:
        if trace.sse is not None:
            trace.sse.add(data)

    def capture_stream_usage(self, trace: RequestTrace, event: dict) -> None:
        nested = event.get("response")
        if not isinstance(nested, dict):
            nested = event
        usage = nested.get("usage")
        if isinstance(usage, dict):
            trace.prompt_tokens, trace.completion_tokens = _tokens(
                usage, "input_tokens", "output_tokens"
            )

    def open_checked(
        self,
        method: str,
        path: str,
        body: bytes | None,
        api_key: str | None = None,
    ):
        """Buka upstream dan buang respons error >=400 jadi UpstreamFailure."""
        try:
            response = request_upstream(
                self.config, method, path, body, api_key=api_key
            )
            status = response.status
            content_type = response.headers.get(
                "Content-Type", "application/json"
            )
            if status < 400:
                return response, status, content_type
            try:
                error_body = response.read()
            except _READ_ERRORS:
                error_body = b""
            response.close()
        except UpstreamFailure as failure:
            self.send_failure(failure)
            return None, None, None
        except _READ_ERRORS:
            self.send_network_error()
            return None, None, None

        message, code = parse_error_body(error_body)
        self.send_error_body(status, "upstream_error", code, message)
        return None, None, None

    def proxy_responses(self, api_key: str | None = None) -> None:
        trace = self.start_trace("responses")
        try:
            self.forward_responses(trace, api_key)
        finally:
            self.finish_trace(trace)

    def forward_responses(
        self, trace: RequestTrace, api_key: str | None = None
    ) -> None:
        raw_body = self.read_request_body()
        parsed, valid = self.parse_json_body(raw_body)
        if not valid:
            self.send_invalid_json()
            self.dump_response(trace)
            return

        model = parsed.get("model")
        trace.model = model if isinstance(model, str) else ""
        self.dump_inbound(trace, raw_body)

        trace.started = time.monotonic()
        response, status, content_type = self.open_checked(
            "POST", "/v1/responses", raw_body, api_key=api_key
        )
        if response is None:
            self.dump_response(trace)
            return

        self.dump_upstream(trace, api_key, "/v1/responses", raw_body)

        try:
            if "text/event-stream" in content_type.lower():
                trace.stream = True
                self.stream_passthrough_responses(response, trace)
                return
            body = response.read()
        except _READ_ERRORS:
            response.close()
            self.send_network_error()
            return
        finally:
            close_quietly(response)

        if status == 204:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.dump_response(trace)
            return

        try:
            upstream_json = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            upstream_json = None
        if isinstance(upstream_json, dict):
            trace.prompt_tokens, trace.completion_tokens = _tokens(
                upstream_json.get("usage"), "input_tokens", "output_tokens"
            )

        value, truncated = self.dump_body(trace, body)
        self.dump_response(trace, body=value, truncated=truncated)
        self.send_raw(status, content_type, sanitize_json_body(body, content_type))

    def stream_passthrough_responses(
        self, response, trace: RequestTrace | None = None
    ) -> None:
        content_type = response.headers.get("Content-Type", "text/event-stream")
        self.start_sse(response.status, content_type)

        try:
            for _, data in read_stream_response(response):
                if not data:
                    continue
                if trace is not None:
                    self.accumulate_sse(trace, data)
                if data != "[DONE]":
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        parsed = None
                    if parsed is not None:
                        if trace is not None:
                            self.capture_stream_usage(trace, parsed)
                        data = json.dumps(
                            strip_instructions(parsed), ensure_ascii=False,
                            separators=(",", ":"),
                        )
                if not self.write_bytes(f"data: {data}\n\n".encode("utf-8")):
                    break
                self.flush_client()
        finally:
            close_quietly(response)
            if trace is not None:
                self.dump_response(trace, stream=True)

    def proxy_chat_completions(self, api_key: str | None = None) -> None:
        trace = self.start_trace("chat")
        try:
            self.forward_chat_completions(trace, api_key)
        finally:
            self.finish_trace(trace)

    def forward_chat_completions(
        self, trace: RequestTrace, api_key: str | None = None
    ) -> None:
        raw_body = self.read_request_body()
        body, valid = self.parse_json_body(raw_body)
        if not valid or body is None:
            self.send_invalid_json()
            self.dump_response(trace)
            return

        model = body.get("model")
        trace.model = model if isinstance(model, str) else ""
        self.dump_inbound(trace, raw_body)

        responses_body = translate_chat_request(body)
        upstream_body = json.dumps(
            responses_body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        trace.started = time.monotonic()
        response, _, _ = self.open_checked(
            "POST", "/v1/responses", upstream_body, api_key=api_key
        )
        if response is None:
            self.dump_response(trace)
            return

        self.dump_upstream(trace, api_key, "/v1/responses", upstream_body)

        if responses_body.get("stream") is True:
            trace.stream = True
            self.stream_chat_response(response, body, trace)
            return

        try:
            upstream_json_bytes = response.read()
        except _READ_ERRORS:
            self.send_network_error()
            return
        finally:
            close_quietly(response)

        try:
            upstream_json = json.loads(upstream_json_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_network_error()
            return
        if not isinstance(upstream_json, dict):
            self.send_network_error()
            return

        chat_payload = translate_responses_to_chat(
            upstream_json, body.get("model") or DEFAULT_MODEL
        )
        trace.prompt_tokens, trace.completion_tokens = _tokens(
            chat_payload.get("usage"), "prompt_tokens", "completion_tokens"
        )
        value, truncated = self.dump_body(trace, upstream_json_bytes)
        self.dump_response(trace, body=value, truncated=truncated)
        self.send_json(200, strip_instructions(chat_payload))

    def stream_chat_response(
        self, response, chat_request: dict, trace: RequestTrace | None = None
    ) -> None:
        state = ChatStreamState(chat_request.get("model") or DEFAULT_MODEL)
        self.start_sse()
        sent_completed = False
        disconnected = False

        try:
            for event_name, data in read_stream_response(response):
                if not data:
                    continue
                if trace is not None:
                    self.accumulate_sse(trace, data)
                if data == "[DONE]":
                    if not sent_completed:
                        if not self.write_sse_json(stream_done_chunk(state)):
                            disconnected = True
                            break
                        sent_completed = True
                    if not self.write_done():
                        disconnected = True
                    break

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                chunks = state.translate_event(event_name, event)
                for chunk in chunks:
                    if not self.write_sse_json(chunk):
                        disconnected = True
                        break
                if disconnected:
                    break
                if event_name == "response.completed" or event.get(
                    "type"
                ) == "response.completed":
                    sent_completed = True
                    if trace is not None:
                        self.capture_stream_usage(trace, event)
                    if not self.write_done():
                        disconnected = True
                    break
        finally:
            close_quietly(response)
            if trace is not None:
                self.dump_response(trace, stream=True)

        if disconnected or sent_completed:
            return
        if not self.write_sse_json(stream_closed_chunk(state)):
            return
        self.write_done()
