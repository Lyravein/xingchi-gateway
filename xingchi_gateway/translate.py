"""Terjemahan protokol Chat Completions <-> Responses.

Berisi fungsi murni untuk request, respons non-stream, dan satu event SSE.
Tidak melakukan I/O, tidak membaca env, dan tidak menulis ke soket.
"""

from __future__ import annotations

import json

from .config import DEFAULT_MODEL

DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


def strip_instructions(value):
    if isinstance(value, dict):
        return {
            key: strip_instructions(item)
            for key, item in value.items()
            if key != "instructions"
        }
    if isinstance(value, list):
        return [strip_instructions(item) for item in value]
    return value


def extract_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return ""


def json_string(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def text_part(text: str) -> dict:
    return {"type": "input_text", "text": text}


def _message_item(role: str, text: str) -> dict:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def _function_call_items(tool_calls: list) -> list[dict]:
    items = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        call_id = call.get("id") or call.get("call_id")
        name = function.get("name")
        items.append(
            {
                "type": "function_call",
                "call_id": call_id if isinstance(call_id, str) else "",
                "name": name if isinstance(name, str) else "",
                "arguments": json_string(function.get("arguments", "")),
            }
        )
    return items


def _translate_tools(chat_tools: list) -> list[dict]:
    tools = []
    for tool in chat_tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        responses_tool: dict = {
            "type": "function",
            "name": function.get("name"),
            "description": function.get("description"),
            "parameters": function.get("parameters"),
        }
        if "strict" in function:
            responses_tool["strict"] = function["strict"]
        elif "strict" in tool:
            responses_tool["strict"] = tool["strict"]
        tools.append(responses_tool)
    return tools


def _message_to_items(message: dict) -> tuple[list[str], list[dict]]:
    role = message.get("role")
    content = message.get("content", "")

    if role in ("system", "developer"):
        text = extract_text(content)
        return ([text], []) if text else ([], [])

    if role == "user":
        return ([], [_message_item("user", extract_text(content))])

    if role == "assistant":
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            items = []
            text = extract_text(content)
            if text:
                items.append(_message_item("assistant", text))
            items.extend(_function_call_items(tool_calls))
            return [], items
        return ([], [_message_item("assistant", extract_text(content))])

    if role == "tool":
        call_id = message.get("tool_call_id") or message.get("call_id")
        return (
            [],
            [
                {
                    "type": "function_call_output",
                    "call_id": call_id if isinstance(call_id, str) else "",
                    "output": json_string(content),
                }
            ],
        )

    return [], []


def translate_chat_request(body: dict) -> dict:
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    instruction_parts: list[str] = []
    input_items: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        instructions, items = _message_to_items(message)
        instruction_parts.extend(instructions)
        input_items.extend(items)

    responses_body: dict = {
        "model": body.get("model") or DEFAULT_MODEL,
        "input": input_items,
    }
    responses_body["instructions"] = (
        "\n\n".join(instruction_parts) if instruction_parts else DEFAULT_INSTRUCTIONS
    )

    if "max_completion_tokens" in body:
        responses_body["max_output_tokens"] = body["max_completion_tokens"]
    elif "max_tokens" in body:
        responses_body["max_output_tokens"] = body["max_tokens"]

    for key in (
        "temperature",
        "top_p",
        "tool_choice",
        "parallel_tool_calls",
        "stream",
    ):
        if key in body:
            responses_body[key] = body[key]

    chat_tools = body.get("tools")
    if isinstance(chat_tools, list):
        responses_body["tools"] = _translate_tools(chat_tools)

    return responses_body


def chat_usage_from_responses(usage) -> dict:
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def response_created_value(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def response_finish_reason(response: dict) -> str:
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                return "tool_calls"

    incomplete = response.get("incomplete_details")
    if isinstance(incomplete, dict):
        if incomplete.get("reason") == "max_output_tokens":
            return "length"
    if response.get("status") == "incomplete":
        if incomplete is not None and isinstance(incomplete, dict):
            if incomplete.get("reason") == "max_output_tokens":
                return "length"
        return "length"
    return "stop"


def _collect_message_text(item: dict) -> list[str]:
    parts: list[str] = []
    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    elif isinstance(content, str):
        parts.append(content)
    return parts


def _collect_function_call(item: dict) -> dict:
    call_id = item.get("call_id") or item.get("id")
    return {
        "id": call_id if isinstance(call_id, str) else "",
        "type": "function",
        "function": {
            "name": item.get("name", ""),
            "arguments": json_string(item.get("arguments", "")),
        },
    }


def translate_responses_to_chat(response: dict, fallback_model: str) -> dict:
    response = strip_instructions(response)
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "message":
                text_parts.extend(_collect_message_text(item))
            elif item_type == "output_text":
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif item_type == "function_call":
                tool_calls.append(_collect_function_call(item))

    if not text_parts:
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            text_parts.append(output_text)

    message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    created = response_created_value(
        response.get("created_at", response.get("created", 0))
    )
    return {
        "id": response.get("id", ""),
        "object": "chat.completion",
        "created": created,
        "model": response.get("model", fallback_model),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": response_finish_reason(response),
            }
        ],
        "usage": chat_usage_from_responses(response.get("usage")),
    }


class ChatStreamState:
    """State murni untuk memetakan event SSE Responses menjadi chunk Chat."""

    def __init__(self, fallback_model: str):
        self.upstream_id = ""
        self.created = 0
        self.model = fallback_model
        self.usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        self.first_chunk = True
        self.function_called = False
        self.tool_index_by_item: dict[str, int] = {}
        self.next_tool_index = 0

    def update_metadata(self, data: dict) -> None:
        nested = data.get("response")
        if not isinstance(nested, dict):
            nested = data
        upstream_id = nested.get("id")
        if isinstance(upstream_id, str) and upstream_id:
            self.upstream_id = upstream_id
        created = nested.get("created_at", nested.get("created", self.created))
        self.created = response_created_value(created)
        model = nested.get("model")
        if isinstance(model, str) and model:
            self.model = model
        usage = nested.get("usage")
        if isinstance(usage, dict):
            self.usage = chat_usage_from_responses(usage)

    def build_chunk(self, delta: dict, finish_reason=None) -> dict:
        return {
            "id": self.upstream_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

    def _delta(self) -> dict:
        delta: dict = {}
        if self.first_chunk:
            delta["role"] = "assistant"
            self.first_chunk = False
        return delta

    def _finish_reason(self, data: dict) -> str:
        nested = data.get("response")
        if not isinstance(nested, dict):
            nested = data
        if self.function_called:
            return "tool_calls"
        incomplete = nested.get("incomplete_details")
        if isinstance(incomplete, dict):
            if incomplete.get("reason") == "max_output_tokens":
                return "length"
        if nested.get("status") == "incomplete":
            return "length"
        return "stop"

    def _completed_chunk(self, finish_reason: str) -> dict:
        chunk = self.build_chunk({}, finish_reason)
        chunk["usage"] = self.usage
        return chunk

    def _output_text_chunk(self, event: dict) -> list[dict]:
        text = event.get("delta")
        if not isinstance(text, str):
            return []
        delta = self._delta()
        delta["content"] = text
        return [self.build_chunk(delta)]

    def _output_item_chunk(self, event: dict) -> list[dict]:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return []
        self.function_called = True
        call_id = item.get("call_id") or item.get("id")
        call_id = call_id if isinstance(call_id, str) else ""
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            self.tool_index_by_item[item_id] = self.next_tool_index
        self.tool_index_by_item[call_id] = self.next_tool_index
        tool_index = self.next_tool_index
        self.next_tool_index += 1

        name = item.get("name")
        delta = self._delta()
        delta["tool_calls"] = [
            {
                "index": tool_index,
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name if isinstance(name, str) else "",
                    "arguments": "",
                },
            }
        ]
        return [self.build_chunk(delta)]

    def _arguments_chunk(self, event: dict) -> list[dict]:
        arguments = event.get("delta")
        if not isinstance(arguments, str):
            return []
        item_id = event.get("item_id") or event.get("call_id")
        tool_index = self.tool_index_by_item.get(
            item_id if isinstance(item_id, str) else "", self.next_tool_index
        )
        delta = self._delta()
        delta["tool_calls"] = [
            {
                "index": tool_index,
                "function": {"arguments": arguments},
            }
        ]
        return [self.build_chunk(delta)]

    def translate_event(self, event_name: str, event: dict) -> list[dict]:
        """Petakan satu event SSE; kembalikan chunk Chat (bisa kosong)."""
        event_type = event_name or event.get("type", "")
        self.update_metadata(event)
        if event_type == "response.output_text.delta":
            return self._output_text_chunk(event)
        if event_type == "response.output_item.added":
            return self._output_item_chunk(event)
        if event_type == "response.function_call_arguments.delta":
            return self._arguments_chunk(event)
        if event_type == "response.completed":
            self.update_metadata(event)
            return [self._completed_chunk(self._finish_reason(event))]
        return []


def stream_closed_chunk(state: ChatStreamState) -> dict:
    """Chunk akhir saat upstream menutup stream tanpa response.completed."""
    delta: dict = {}
    if state.first_chunk:
        delta["role"] = "assistant"
    chunk = state.build_chunk(delta, "stop")
    chunk["usage"] = state.usage
    return chunk


def stream_done_chunk(state: ChatStreamState) -> dict:
    """Chunk akhir saat upstream mengirim [DONE] tanpa response.completed."""
    chunk = state.build_chunk({}, "stop")
    chunk["usage"] = state.usage
    return chunk
