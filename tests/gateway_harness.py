# input: an SSE or JSON body, a content-type, and a request body to proxy
# output: a live fake upstream behind a live gateway, plus the usage rows the round trip produced
# pos: shared end-to-end fixture for the gateway proxy test suites
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Shared end-to-end harness for gateway proxy tests.

Drives a real upstream through the real proxy rather than mocking `_stream` internals, so chunk
framing, protocol parsing, pricing and persistence are all exercised on the path under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.quota_snapshot import QuotaSnapshotStore
from aistatus.gateway.server import GatewayServer
from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage


class StubPricing:
    """Stands in for CostCalculator so tests never reach the pricing API.

    Records which coroutine was called, because routing accounting through the *async* pricing API
    is itself under test — the sync one blocks the event loop.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def calculate_cost_async(self, provider, model, input_tokens, output_tokens):
        self.calls.append("standard")
        return 0.25

    async def calculate_cost_with_cache_async(self, provider, model, *tokens):
        self.calls.append("cache")
        return 0.5


@dataclass
class ProxyResult:
    """Everything one proxied round trip produced."""

    records: list[dict[str, Any]]
    pricing: StubPricing
    body: str
    headers: dict[str, str] = field(default_factory=dict)


async def serve_upstream(body: bytes, *, content_type: str, chunk: int = 17) -> TestServer:
    """A fake provider that emits `body` in small chunks, so events straddle chunk boundaries."""

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"content-type": content_type})
        await response.prepare(request)
        for start in range(0, len(body), chunk):
            await response.write(body[start : start + chunk])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def proxy_once(
    tmp_path,
    upstream_body: str | bytes,
    *,
    content_type: str = "text/event-stream",
    request_body: bytes = b'{"model":"claude-opus-5","stream":true}',
    endpoint_name: str = "anthropic",
    path: str = "v1/messages",
    mode: str = "default",
    metadata: str = "project=demo,trigger=user",
) -> ProxyResult:
    """Proxy one request end-to-end against a fake upstream and collect what it recorded."""
    raw = upstream_body.encode("utf-8") if isinstance(upstream_body, str) else upstream_body
    upstream = await serve_upstream(raw, content_type=content_type)

    endpoint = EndpointConfig(
        name=endpoint_name,
        base_url=str(upstream.make_url("/")).rstrip("/"),
        auth_style="bearer",
        keys=["sk-test"],
    )
    config = GatewayConfig(
        endpoints={endpoint.name: endpoint},
        endpoint_modes={mode: {endpoint.name: endpoint}},
        mode=mode,
        status_check=False,
    )
    server = GatewayServer(config)
    storage = UsageStorage(base_dir=tmp_path)
    server.usage = UsageTracker(storage=storage)
    pricing = StubPricing()
    server.pricing = pricing
    # Keep the suite off the real ~/.aistatus/quota.json.
    server.quota = QuotaSnapshotStore(tmp_path / "quota.json")

    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    try:
        server._session = aiohttp.ClientSession()
        response = await client.post(
            f"/m/{mode}/{metadata}/{endpoint_name}/{path}",
            data=request_body,
            headers={"content-type": "application/json", "authorization": "Bearer sk-caller"},
        )
        body = await response.text()
        assert response.status == 200, body
        headers = dict(response.headers)
    finally:
        if server._session:
            await server._session.close()
        await client.close()
        await upstream.close()

    return ProxyResult(records=storage.read("all"), pricing=pricing, body=body, headers=headers)
