# input: an upstream that dies partway through a streaming response
# output: regression coverage that truncated streams fail loudly and are not billed
# pos: gateway interrupted-stream test suite
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for interrupted upstream streams.

If the provider connection dies mid-response, ending the proxied stream normally hands the client
a truncated answer that looks complete — a silent wrong answer, which is worse than a visible
failure. It would also bill whatever partial token counts had arrived for a response that was
never delivered.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.quota_snapshot import QuotaSnapshotStore
from aistatus.gateway.server import GatewayServer
from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage
from tests.gateway_harness import StubPricing

USAGE_EVENT = (
    'data: {"type":"message_start","message":{"model":"claude-opus-5",'
    '"usage":{"input_tokens":10}}}\n\n'
    'data: {"type":"message_delta","usage":{"output_tokens":25}}\n\n'
)


async def _dying_upstream(prefix: str) -> TestServer:
    """Emit `prefix`, then kill the connection without terminating the chunked body."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        await response.write(prefix.encode("utf-8"))
        request.transport.abort()
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def _proxy_interrupted(tmp_path, prefix: str):
    """Proxy a request whose upstream dies mid-stream. Returns (error, usage records)."""
    upstream = await _dying_upstream(prefix)
    endpoint = EndpointConfig(
        name="anthropic",
        base_url=str(upstream.make_url("/")).rstrip("/"),
        auth_style="bearer",
        keys=["sk-test"],
    )
    server = GatewayServer(
        GatewayConfig(
            endpoints={endpoint.name: endpoint},
            endpoint_modes={"default": {endpoint.name: endpoint}},
            status_check=False,
        )
    )
    storage = UsageStorage(base_dir=tmp_path)
    server.usage = UsageTracker(storage=storage)
    server.pricing = StubPricing()
    server.quota = QuotaSnapshotStore(tmp_path / "quota.json")

    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    error: Exception | None = None
    try:
        server._session = aiohttp.ClientSession()
        try:
            response = await client.post(
                "/anthropic/v1/messages",
                data=b'{"model":"claude-opus-5","stream":true}',
                headers={"content-type": "application/json", "authorization": "Bearer sk-caller"},
            )
            await response.text()
        except Exception as caught:  # noqa: BLE001 — the failure mode is what is under test
            error = caught
    finally:
        if server._session:
            await server._session.close()
        await client.close()
        await upstream.close()

    return error, storage.read("all")


class TestInterruptedStream:
    @pytest.mark.asyncio
    async def test_truncated_stream_fails_instead_of_looking_complete(self, tmp_path):
        error, records = await _proxy_interrupted(tmp_path, USAGE_EVENT)

        # The client must see a transport failure, not a short but well-formed response.
        assert error is not None, "client received a clean response for a truncated stream"
        assert isinstance(error, (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError))
        # Partial counts from an undelivered response must not be billed.
        assert records == []

    @pytest.mark.asyncio
    async def test_interruption_after_the_terminal_event_is_a_complete_response(self, tmp_path):
        # `[DONE]` means the provider already said everything it was going to say; a connection
        # that drops afterwards has still delivered the whole response.
        error, records = await _proxy_interrupted(tmp_path, USAGE_EVENT + "data: [DONE]\n\n")

        assert error is None, f"a completed stream must not fail: {error!r}"
        assert len(records) == 1
        assert records[0]["in"] == 10
        assert records[0]["out"] == 25
