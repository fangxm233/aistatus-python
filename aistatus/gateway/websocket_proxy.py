# input:  a client WebSocket upgrade, a resolved backend, and an aiohttp client session
# output: a bidirectional WebSocket tunnel with one usage record per completed response
# pos:    Gateway WebSocket proxying and per-response accounting
# >>> 一旦我被更新，务必更新我的开头注释与所属文件夹 CLAUDE.md <<<

"""WebSocket proxying for the gateway.

The OpenAI Responses API is also served over WebSocket, framing the very same JSON events an SSE
stream carries. A coding agent pools one socket and runs many turns over it, so accounting is per
terminal event rather than per connection.

Unlike the TypeScript SDK — which tunnels raw bytes and sniffs frames, to avoid taking on a
WebSocket dependency — this uses aiohttp's own WebSocket implementation on both sides, since
aiohttp is already a hard dependency here. aiohttp therefore decodes and re-encodes each frame
rather than passing bytes through untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from aiohttp import web

from .stream_usage import StreamUsageParser
from .usage_accounting import record_gateway_usage

logger = logging.getLogger("aistatus.gateway")

#: Headers aiohttp regenerates for the upstream handshake; forwarding ours would conflict.
_DROPPED_HANDSHAKE_HEADERS = frozenset({
    "host",
    "connection",
    "upgrade",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "content-length",
})


def to_websocket_url(base_url: str, path: str, query_string: str = "") -> str:
    """Build the upstream WebSocket URL, mapping the http(s) scheme onto ws(s)."""
    base = base_url.rstrip("/")
    for http_scheme, ws_scheme in (("https://", "wss://"), ("http://", "ws://")):
        if base.startswith(http_scheme):
            base = ws_scheme + base[len(http_scheme) :]
            break
    url = f"{base}/{path.lstrip('/')}" if path else base
    return f"{url}?{query_string}" if query_string else url


def upstream_handshake_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip the handshake headers aiohttp regenerates, keeping everything else the caller sent.

    `sec-websocket-extensions` is dropped deliberately, not just because aiohttp negotiates it:
    letting `permessage-deflate` through would compress frames the gateway needs to read to account
    for usage.
    """
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _DROPPED_HANDSHAKE_HEADERS
    }


class WebSocketProxy:
    """Tunnels one client WebSocket to an upstream one, accounting each response that completes."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        backend: dict[str, Any],
        pricing: Any,
        tracker: Any,
        default_billing_mode: str | None = None,
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
        original_model: str = "",
    ) -> None:
        self._session = session
        self._backend = backend
        self._pricing = pricing
        self._tracker = tracker
        self._default_billing_mode = default_billing_mode
        self._billing_mode = billing_mode
        self._metadata = metadata
        self._parser = StreamUsageParser(original_model)
        self._turn_started = time.monotonic()

    async def run(
        self, request: web.Request, url: str, headers: dict[str, str]
    ) -> web.WebSocketResponse:
        """Connect upstream, accept the client, and pump until either side closes."""
        upstream = await self._session.ws_connect(url, headers=headers, autoping=True)
        client = web.WebSocketResponse()
        await client.prepare(request)
        try:
            await asyncio.gather(
                self._pump_to_upstream(client, upstream),
                self._pump_to_client(upstream, client),
            )
        finally:
            await upstream.close()
            if not client.closed:
                await client.close()
        return client

    # --- private ---

    async def _pump_to_upstream(
        self, client: web.WebSocketResponse, upstream: aiohttp.ClientWebSocketResponse
    ) -> None:
        async for message in client:
            if message.type == aiohttp.WSMsgType.TEXT:
                # A new request begins a new turn, so latency is measured from here.
                self._turn_started = time.monotonic()
                await upstream.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                self._turn_started = time.monotonic()
                await upstream.send_bytes(message.data)
            else:
                break

    async def _pump_to_client(
        self, upstream: aiohttp.ClientWebSocketResponse, client: web.WebSocketResponse
    ) -> None:
        async for message in upstream:
            if message.type == aiohttp.WSMsgType.TEXT:
                await client.send_str(message.data)
                await self._observe(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                await client.send_bytes(message.data)
                await self._observe(message.data.decode("utf-8", errors="ignore"))
            else:
                break

    async def _observe(self, payload: str) -> None:
        """Feed one upstream event to the parser, recording a row when a response completes."""
        self._parser.apply_message(payload)
        if not self._parser.has_terminal_event():
            return
        if self._parser.has_usage():
            await record_gateway_usage(
                backend=self._backend,
                usage=self._parser.usage,
                elapsed_ms=int((time.monotonic() - self._turn_started) * 1000),
                pricing=self._pricing,
                tracker=self._tracker,
                billing_mode=self._billing_mode,
                default_billing_mode=self._default_billing_mode,
                metadata=self._metadata,
            )
        # One pooled socket carries many turns, so the parser is cleared for the next response
        # rather than accumulating the previous one's totals into it.
        self._parser.reset()
