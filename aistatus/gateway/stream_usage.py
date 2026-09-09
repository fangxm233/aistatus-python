# input:  upstream response streams and their JSON events
# output: GatewayUsage totals and an event-stream/buffer classification
# pos:    Gateway streaming protocol parsing shared by SSE and WebSocket
# >>> 一旦我被更新，务必更新我的开头注释与所属文件夹 CLAUDE.md <<<

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass
from typing import Any

# OpenAI Responses API events that close a response and carry its final usage. `response.done` is
# the name the ChatGPT Codex WebSocket transport uses for what SSE calls `response.completed`.
RESPONSES_TERMINAL_EVENTS = frozenset(
    {"response.completed", "response.incomplete", "response.done"}
)

_SSE_START = re.compile(r"^(event|data|id|retry):")


@dataclass
class GatewayUsage:
    """One response's token totals, normalized across provider protocols."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def as_int(value: Any) -> int:
    """Coerce a provider-supplied count to a non-fractional int, treating junk as zero."""
    try:
        number = float(value if value is not None else 0)
    except (TypeError, ValueError):
        return 0
    if number != number or number in (float("inf"), float("-inf")):
        return 0
    return int(number)


def parse_responses_usage(model: str, usage: dict[str, Any]) -> GatewayUsage | None:
    """Normalize an OpenAI Responses API usage block (`/v1/responses`, ChatGPT Codex backend).

    Unlike Anthropic — where ``input_tokens`` counts only the uncached prefix — the Responses API
    reports a TOTAL ``input_tokens`` that already includes cached and cache-write tokens, with the
    breakdown in ``input_tokens_details``. Recording it verbatim would double-count the cached
    prefix, so the cached parts are subtracted back out into their own fields.

    Returns None for any other usage shape: the presence of ``input_tokens_details`` is the
    discriminator, and Anthropic / chat-completions payloads never carry it.
    """
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        return None

    cache_read = as_int(details.get("cached_tokens", 0))
    cache_creation = as_int(details.get("cache_write_tokens", 0))
    total_input = as_int(usage.get("input_tokens", 0))
    return GatewayUsage(
        model=model,
        input_tokens=max(0, total_input - cache_read - cache_creation),
        output_tokens=as_int(usage.get("output_tokens", 0)),
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


class StreamUsageParser:
    """Accumulates provider usage off a response stream, incrementally.

    Understands three protocols: Anthropic Messages SSE, OpenAI chat-completions SSE, and the
    OpenAI Responses API — the last either as SSE or, via :meth:`apply_message`, as the Codex
    WebSocket transport, which frames the very same JSON events.

    Feed SSE bytes through :meth:`push`; it buffers only up to the next event boundary rather than
    retaining the whole response.
    """

    def __init__(self, model: str = "") -> None:
        self._initial_model = model
        self.usage = GatewayUsage(model=model)
        self._buffer = ""
        self._terminal_event = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    def push_bytes(self, chunk: bytes) -> None:
        """Feed raw wire bytes.

        Decoding is incremental because a TCP chunk boundary can fall inside a multi-byte
        character; decoding each chunk on its own would corrupt whichever event straddles the split.
        """
        self.push(self._decoder.decode(chunk))

    def push(self, chunk: str) -> None:
        """Feed a decoded SSE fragment. Partial events are held until their blank-line terminator."""
        self._buffer += chunk
        while "\n\n" in self._buffer:
            event, self._buffer = self._buffer.split("\n\n", 1)
            for line in event.strip().splitlines():
                self._parse_line(line)

    def apply_message(self, payload: str) -> None:
        """Feed one already-framed JSON event (WebSocket transport)."""
        try:
            self._apply_payload(json.loads(payload))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    def reset(self) -> None:
        """Drop accumulated usage so one long-lived connection can account several responses."""
        self.usage = GatewayUsage(model=self._initial_model)
        self._terminal_event = False

    def has_usage(self) -> bool:
        return self.usage.input_tokens > 0 or self.usage.output_tokens > 0

    def has_terminal_event(self) -> bool:
        """True once the stream signalled a clean end, which tells a truncation from a completion."""
        return self._terminal_event

    # --- private ---

    def _parse_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if payload == "[DONE]":
            self._terminal_event = True
            return
        if not payload:
            return
        try:
            self._apply_payload(json.loads(payload))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    def _apply_payload(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if self._apply_responses_payload(data):
            return

        event_type = data.get("type")
        if event_type == "message_stop":
            self._terminal_event = True
        if event_type == "message_start":
            message = data.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self.usage.input_tokens = as_int(usage.get("input_tokens", 0))
                self.usage.cache_creation_input_tokens = as_int(
                    usage.get("cache_creation_input_tokens", 0)
                )
                self.usage.cache_read_input_tokens = as_int(
                    usage.get("cache_read_input_tokens", 0)
                )
        if event_type == "message_delta":
            usage = data.get("usage")
            if isinstance(usage, dict):
                self.usage.output_tokens = as_int(usage.get("output_tokens", 0))

        usage = data.get("usage")
        if isinstance(usage, dict):
            self._apply_generic_usage(usage)

    def _apply_responses_payload(self, data: dict[str, Any]) -> bool:
        """Handle one Responses API event; returns True when the event belonged to that protocol.

        Responses delta events never carry a top-level ``usage``, so short-circuiting here keeps the
        generic branch from overwriting the normalized totals. The model name is taken from
        ``response.model`` when the caller had none — the case for Codex, whose request body is
        zstd-compressed and therefore unreadable to the gateway's model extraction.
        """
        event_type = data.get("type")
        if not isinstance(event_type, str) or not event_type.startswith("response."):
            return False

        response = data.get("response")
        if not isinstance(response, dict):
            return True
        if not self.usage.model and isinstance(response.get("model"), str):
            self.usage.model = response["model"]
        if event_type not in RESPONSES_TERMINAL_EVENTS:
            return True

        self._terminal_event = True
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return True
        parsed = parse_responses_usage(self.usage.model, usage)
        if parsed is None:
            return True
        self.usage.input_tokens = parsed.input_tokens
        self.usage.output_tokens = parsed.output_tokens
        self.usage.cache_creation_input_tokens = parsed.cache_creation_input_tokens
        self.usage.cache_read_input_tokens = parsed.cache_read_input_tokens
        return True

    def _apply_generic_usage(self, usage: dict[str, Any]) -> None:
        self.usage.input_tokens = as_int(
            usage.get("input_tokens", usage.get("prompt_tokens", self.usage.input_tokens))
        )
        self.usage.output_tokens = as_int(
            usage.get("output_tokens", usage.get("completion_tokens", self.usage.output_tokens))
        )
        self.usage.cache_creation_input_tokens = as_int(
            usage.get("cache_creation_input_tokens", self.usage.cache_creation_input_tokens)
        )
        self.usage.cache_read_input_tokens = as_int(
            usage.get("cache_read_input_tokens", self.usage.cache_read_input_tokens)
        )


def looks_like_event_stream(head: bytes) -> bool:
    """SSE payloads open with a field name or a comment line (WHATWG event-stream)."""
    start = head[:64].decode("utf-8", errors="ignore").lstrip("﻿ \t\r\n")
    return bool(_SSE_START.match(start)) or start.startswith(":")


def parse_usage_response(body: bytes, original_model: str) -> GatewayUsage | None:
    """Parse usage out of a complete non-streaming JSON response body."""
    try:
        payload = json.loads(body.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    model = original_model or (payload.get("model") if isinstance(payload.get("model"), str) else "") or ""
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    responses = parse_responses_usage(model, usage)
    parsed = responses or GatewayUsage(
        model=model,
        input_tokens=as_int(usage.get("input_tokens", usage.get("prompt_tokens", 0))),
        output_tokens=as_int(usage.get("output_tokens", usage.get("completion_tokens", 0))),
        cache_creation_input_tokens=as_int(usage.get("cache_creation_input_tokens", 0)),
        cache_read_input_tokens=as_int(usage.get("cache_read_input_tokens", 0)),
    )
    if not model and not parsed.input_tokens and not parsed.output_tokens:
        return None
    return parsed
