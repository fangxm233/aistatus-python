# input: a live fake upstream emitting SSE in three provider protocols, proxied through the gateway
# output: regression coverage that streaming responses are accounted, priced and given real latency
# pos: gateway streaming usage accounting test suite (mirrors TS aistatus tests/gateway.test.mjs)
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for streaming usage accounting.

Before this suite the gateway recorded usage only on the `anthropic-to-openai` translate path;
every ordinary streaming response — the overwhelming majority of real traffic — was proxied without
producing a usage row at all. These tests drive a real upstream through the real proxy so the whole
path (chunking, protocol parsing, pricing, persistence) is exercised, not just the parser.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.server import GatewayServer
from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage

ANTHROPIC_SSE = (
    'data: {"type":"message_start","message":{"model":"claude-opus-5",'
    '"usage":{"input_tokens":10,"cache_read_input_tokens":400,"cache_creation_input_tokens":7}}}\n\n'
    'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
    'data: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
    "data: [DONE]\n\n"
)

OPENAI_SSE = (
    'data: {"id":"c1","model":"gpt-5.6","choices":[{"delta":{"content":"hi"}}]}\n\n'
    'data: {"id":"c1","model":"gpt-5.6","usage":{"prompt_tokens":80,"completion_tokens":12}}\n\n'
    "data: [DONE]\n\n"
)

RESPONSES_SSE = (
    'data: {"type":"response.created","response":{"id":"r1","model":"gpt-5.6-sol","usage":null}}\n\n'
    'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
    'data: {"type":"response.completed","response":{"id":"r1","model":"gpt-5.6-sol",'
    '"usage":{"input_tokens":1000,"input_tokens_details":{"cached_tokens":800},'
    '"output_tokens":50}}}\n\n'
)


class _StubPricing:
    """Stands in for CostCalculator so tests never reach the pricing API.

    Records which coroutine was called, since routing accounting through the async pricing API is
    itself part of what is under test — the sync one blocks the event loop.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def calculate_cost_async(self, provider, model, input_tokens, output_tokens):
        self.calls.append("standard")
        return 0.25

    async def calculate_cost_with_cache_async(self, provider, model, *tokens):
        self.calls.append("cache")
        return 0.5


async def _upstream_serving(body: str, *, content_type: str = "text/event-stream", chunk: int = 17):
    """A fake provider that emits `body` in small chunks, so event boundaries straddle chunks."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"content-type": content_type})
        await response.prepare(request)
        raw = body.encode("utf-8")
        for start in range(0, len(raw), chunk):
            await response.write(raw[start : start + chunk])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def _proxy_stream(tmp_path, body: str, *, model: str,
                        endpoint_name: str = "anthropic", path: str = "v1/messages"):
    """Proxy one streaming request end-to-end and return (usage records, stubbed pricing)."""
    upstream = await _upstream_serving(body)
    endpoint = EndpointConfig(
        name=endpoint_name,
        base_url=str(upstream.make_url("/")).rstrip("/"),
        auth_style="bearer",
        keys=["sk-test"],
    )
    config = GatewayConfig(
        endpoints={endpoint.name: endpoint},
        endpoint_modes={"default": {endpoint.name: endpoint}},
        status_check=False,
    )
    server = GatewayServer(config)
    storage = UsageStorage(base_dir=tmp_path)
    server.usage = UsageTracker(storage=storage)
    pricing = _StubPricing()
    server.pricing = pricing

    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    try:
        import aiohttp

        server._session = aiohttp.ClientSession()
        response = await client.post(
            f"/m/default/project=demo,trigger=user/{endpoint_name}/{path}",
            data=f'{{"model":"{model}","stream":true}}',
            headers={"content-type": "application/json", "authorization": "Bearer sk-caller"},
        )
        streamed = await response.text()
        assert response.status == 200, streamed
    finally:
        if server._session:
            await server._session.close()
        await client.close()
        await upstream.close()

    return storage.read("all"), pricing, streamed


class TestStreamingUsageIsRecorded:
    @pytest.mark.asyncio
    async def test_anthropic_message_start_and_delta(self, tmp_path):
        # The old `_extract_usage_from_sse` only understood a top-level `usage` object, so Anthropic
        # streams — which report input on `message_start` and output on `message_delta` — recorded
        # nothing even on the one path that did call it.
        records, pricing, _ = await _proxy_stream(
            tmp_path, ANTHROPIC_SSE, model="claude-opus-5"
        )

        assert len(records) == 1
        record = records[0]
        assert record["model"] == "claude-opus-5"
        assert record["provider"] == "anthropic"
        assert record["in"] == 10
        assert record["cache_read_in"] == 400
        assert record["cache_creation_in"] == 7
        assert record["out"] == 25
        assert pricing.calls == ["cache"]
        assert record["cost"] == 0.5

    @pytest.mark.asyncio
    async def test_openai_chat_completions_top_level_usage(self, tmp_path):
        records, pricing, _ = await _proxy_stream(
            tmp_path, OPENAI_SSE, model="gpt-5.6", endpoint_name="openai", path="v1/chat/completions"
        )

        assert len(records) == 1
        assert records[0]["model"] == "gpt-5.6"
        assert records[0]["provider"] == "openai"
        assert records[0]["in"] == 80
        assert records[0]["out"] == 12
        # No cache tokens, so the plain pricing path is used.
        assert pricing.calls == ["standard"]

    @pytest.mark.asyncio
    async def test_openai_responses_splits_out_cached_input(self, tmp_path):
        records, _, _ = await _proxy_stream(
            tmp_path, RESPONSES_SSE, model="gpt-5.6-sol",
            endpoint_name="openai", path="codex/responses",
        )

        assert len(records) == 1
        # `input_tokens` is a TOTAL that already contains the 800 cached tokens; recording it
        # verbatim would double-count them, so the uncached remainder is 200.
        assert records[0]["in"] == 200
        assert records[0]["cache_read_in"] == 800
        assert records[0]["out"] == 50

    @pytest.mark.asyncio
    async def test_records_real_latency_and_request_metadata(self, tmp_path):
        records, _, _ = await _proxy_stream(tmp_path, ANTHROPIC_SSE, model="claude-opus-5")

        # Streaming rows used to be written with a hardcoded latency of 0.
        assert records[0]["latency_ms"] > 0
        assert records[0]["project"] == "demo"
        assert records[0]["trigger"] == "user"
        assert records[0]["billing_mode"] == "default"

    @pytest.mark.asyncio
    async def test_body_reaches_the_client_unchanged(self, tmp_path):
        _, _, streamed = await _proxy_stream(tmp_path, ANTHROPIC_SSE, model="claude-opus-5")
        assert streamed == ANTHROPIC_SSE

    @pytest.mark.asyncio
    async def test_stream_without_usage_records_nothing(self, tmp_path):
        records, pricing, _ = await _proxy_stream(
            tmp_path,
            'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\ndata: [DONE]\n\n',
            model="claude-opus-5",
        )
        assert records == []
        assert pricing.calls == []
