"""Translate between Anthropic Messages API and OpenAI Chat Completions API.

Covers the common text-completion case (messages, system prompt, streaming).
Tool use and multimodal content are NOT translated — they pass through as-is.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator


# ---------------------------------------------------------------------------
# Request translation: Anthropic → OpenAI
# ---------------------------------------------------------------------------

def anthropic_request_to_openai(body: bytes) -> bytes:
    """Convert an Anthropic Messages API request body to OpenAI Chat Completions."""
    data = json.loads(body)

    messages: list[dict] = []

    # Anthropic "system" → OpenAI system message
    system = data.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # Anthropic structured system: [{"type":"text","text":"..."}]
            text = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
            if text:
                messages.append({"role": "system", "content": text})

    # Messages
    for msg in data.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Text content blocks → concatenate
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                messages.append({"role": role, "content": "\n".join(texts)})

    openai_body: dict = {"model": data.get("model", ""), "messages": messages}

    # Copy compatible params
    for key in ("max_tokens", "temperature", "top_p", "stream"):
        if key in data:
            openai_body[key] = data[key]
    if "stop_sequences" in data:
        openai_body["stop"] = data["stop_sequences"]
    # stream_options for OpenAI to include usage in streaming
    if data.get("stream"):
        openai_body["stream_options"] = {"include_usage": True}

    return json.dumps(openai_body).encode()


# ---------------------------------------------------------------------------
# Response translation: OpenAI → Anthropic
# ---------------------------------------------------------------------------

def openai_response_to_anthropic(body: bytes, original_model: str = "") -> bytes:
    """Convert an OpenAI Chat Completions response to Anthropic Messages format."""
    data = json.loads(body)

    content_text = ""
    stop_reason = "end_turn"

    choices = data.get("choices", [])
    if choices:
        choice = choices[0]
        content_text = choice.get("message", {}).get("content", "") or ""
        finish = choice.get("finish_reason", "stop")
        stop_reason = {"stop": "end_turn", "length": "max_tokens"}.get(finish, "end_turn")

    usage = data.get("usage", {})

    anthropic_resp = {
        "id": f"msg_{data.get('id', 'gw')}",
        "type": "message",
        "role": "assistant",
        "model": original_model or data.get("model", ""),
        "content": [{"type": "text", "text": content_text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
    return json.dumps(anthropic_resp).encode()


# ---------------------------------------------------------------------------
# Streaming translation: OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------------------

async def openai_sse_to_anthropic_sse(
    chunks: AsyncIterator[bytes],
    original_model: str = "",
) -> AsyncIterator[bytes]:
    """Translate an OpenAI SSE stream into Anthropic SSE events."""
    msg_id = f"msg_gw_{int(time.time())}"

    # Emit: message_start
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": original_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    # Emit: content_block_start
    yield _sse("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    input_tokens = 0
    output_tokens = 0
    buffer = b""

    async for raw_chunk in chunks:
        buffer += raw_chunk
        # Split on SSE event boundaries
        while b"\n\n" in buffer:
            event_bytes, buffer = buffer.split(b"\n\n", 1)
            event_str = event_bytes.decode("utf-8", errors="replace").strip()
            if not event_str:
                continue

            # Extract the data line(s)
            for line in event_str.split("\n"):
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()

                if payload == "[DONE]":
                    # End of stream
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop", "index": 0,
                    })
                    yield _sse("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": output_tokens},
                    })
                    yield _sse("message_stop", {"type": "message_stop"})
                    return

                try:
                    oai = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                # Usage info (when stream_options.include_usage is set)
                if "usage" in oai and oai["usage"]:
                    input_tokens = oai["usage"].get("prompt_tokens", input_tokens)
                    output_tokens = oai["usage"].get("completion_tokens", output_tokens)

                choices = oai.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                text = delta.get("content")
                if text:
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    })

                # Check finish_reason
                if choices[0].get("finish_reason"):
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop", "index": 0,
                    })
                    yield _sse("message_delta", {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": output_tokens},
                    })
                    yield _sse("message_stop", {"type": "message_stop"})
                    return


def _sse(event: str, data: dict) -> bytes:
    """Format a single SSE event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
