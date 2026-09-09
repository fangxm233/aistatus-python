# input: a client WebSocket upgrade against the gateway, backed by a fake upstream WebSocket server
# output: regression coverage for tunnelling, auth rewriting, per-response accounting and refusals
# pos: gateway WebSocket proxy test suite (mirrors TS aistatus tests/websocket-proxy.test.mjs)
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for WebSocket proxying.

The OpenAI Responses API is also served over WebSocket, and a coding agent pools one socket across
many turns — so usage must be recorded per completed response, not per connection.
"""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.quota_snapshot import QuotaSnapshotStore
from aistatus.gateway.server import GatewayServer
from aistatus.gateway.websocket_proxy import to_websocket_url, upstream_handshake_headers
from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage
from tests.gateway_harness import StubPricing

WS_PATH = "/m/openai-codex/project=demo,trigger=user/openai/codex/responses"


def completed_event(usage: dict, model: str = "gpt-5.6-sol") -> str:
    return json.dumps({
        "type": "response.completed",
        "response": {"id": "r1", "model": model, "status": "completed", "usage": usage},
    })


async def fake_upstream(on_message=None, *, reject: int = 0) -> tuple[TestServer, dict]:
    """A stand-in ChatGPT Codex WebSocket endpoint.

    `reject` refuses the handshake instead, standing in for an expired OAuth token.
    """
    seen: dict = {"handshakes": [], "received": []}

    async def handler(request: web.Request):
        # Lower-cased: aiohttp preserves the sender's casing, which is not part of the contract.
        seen["handshakes"].append({k.lower(): v for k, v in request.headers.items()})
        if reject:
            return web.Response(status=reject, text="unauthorized")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            seen["received"].append(message.data)
            if on_message is not None:
                for reply in on_message(message.data):
                    await ws.send_str(reply)
        return ws

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server, seen


async def gateway_for(upstream: TestServer, tmp_path, *, websocket: bool = True):
    endpoint = EndpointConfig(
        name="openai",
        base_url=str(upstream.make_url("/")).rstrip("/"),
        auth_style="bearer",
        keys=[],
        passthrough=True,
    )
    config = GatewayConfig(
        endpoints={endpoint.name: endpoint},
        endpoint_modes={"openai-codex": {endpoint.name: endpoint}},
        mode="openai-codex",
        status_check=False,
        websocket=websocket,
    )
    server = GatewayServer(config)
    storage = UsageStorage(base_dir=tmp_path)
    server.usage = UsageTracker(storage=storage)
    server.pricing = StubPricing()
    server.quota = QuotaSnapshotStore(tmp_path / "quota.json")
    server._session = aiohttp.ClientSession()

    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    return server, client, storage


async def _teardown(server, client, upstream):
    if server._session:
        await server._session.close()
    await client.close()
    await upstream.close()


class TestTunnelling:
    @pytest.mark.asyncio
    async def test_relays_messages_both_ways_and_rewrites_auth(self, tmp_path):
        upstream, seen = await fake_upstream(lambda data: [f"echo:{data}"])
        server, client, _ = await gateway_for(upstream, tmp_path)
        try:
            ws = await client.ws_connect(
                WS_PATH,
                headers={"authorization": "Bearer sk-caller-oauth", "originator": "pi"},
            )
            await ws.send_str("hello")
            reply = await ws.receive(timeout=5)

            assert reply.data == "echo:hello"
            assert seen["received"] == ["hello"]
            # A passthrough endpoint forwards the caller's own bearer, and custom headers survive.
            assert seen["handshakes"][0]["authorization"] == "Bearer sk-caller-oauth"
            assert seen["handshakes"][0]["originator"] == "pi"
            await ws.close()
        finally:
            await _teardown(server, client, upstream)

    @pytest.mark.asyncio
    async def test_does_not_forward_permessage_deflate(self, tmp_path):
        # Compressed frames would be opaque to the usage parser.
        upstream, seen = await fake_upstream(lambda data: ["ok"])
        server, client, _ = await gateway_for(upstream, tmp_path)
        try:
            ws = await client.ws_connect(
                WS_PATH,
                headers={"authorization": "Bearer sk-caller-oauth"},
                compress=15,
            )
            await ws.send_str("hi")
            await ws.receive(timeout=5)
            assert "sec-websocket-extensions" not in seen["handshakes"][0]
            await ws.close()
        finally:
            await _teardown(server, client, upstream)


class TestAccounting:
    @pytest.mark.asyncio
    async def test_records_one_row_per_completed_response_on_a_pooled_socket(self, tmp_path):
        def reply(_data):
            return [
                json.dumps({"type": "response.created",
                            "response": {"id": "r1", "model": "gpt-5.6-sol"}}),
                json.dumps({"type": "response.output_text.delta", "delta": "hi"}),
                completed_event({"input_tokens": 1000,
                                 "input_tokens_details": {"cached_tokens": 800},
                                 "output_tokens": 50}),
            ]

        upstream, _ = await fake_upstream(reply)
        server, client, storage = await gateway_for(upstream, tmp_path)
        try:
            ws = await client.ws_connect(
                WS_PATH, headers={"authorization": "Bearer sk-caller-oauth"}
            )
            # Two turns over the SAME socket: a coding agent pools and reuses them.
            for _ in range(2):
                await ws.send_str(json.dumps({"type": "response.create"}))
                for _ in range(3):
                    await ws.receive(timeout=5)
            await ws.close()

            records = storage.read("all")
            assert len(records) == 2, "each completed response must produce its own row"
            for record in records:
                assert record["model"] == "gpt-5.6-sol"
                # `input_tokens` is a total that already includes the 800 cached tokens.
                assert record["in"] == 200
                assert record["cache_read_in"] == 800
                assert record["out"] == 50
                assert record["billing_mode"] == "openai-codex"
                assert record["project"] == "demo"
                assert record["trigger"] == "user"
        finally:
            await _teardown(server, client, upstream)

    @pytest.mark.asyncio
    async def test_records_nothing_when_no_response_completes(self, tmp_path):
        upstream, _ = await fake_upstream(lambda data: [
            json.dumps({"type": "response.output_text.delta", "delta": "hi"})
        ])
        server, client, storage = await gateway_for(upstream, tmp_path)
        try:
            ws = await client.ws_connect(
                WS_PATH, headers={"authorization": "Bearer sk-caller-oauth"}
            )
            await ws.send_str("go")
            await ws.receive(timeout=5)
            await ws.close()
            assert storage.read("all") == []
        finally:
            await _teardown(server, client, upstream)


class TestRefusals:
    @pytest.mark.asyncio
    async def test_relays_an_upstream_handshake_rejection(self, tmp_path):
        upstream, _ = await fake_upstream(reject=401)
        server, client, storage = await gateway_for(upstream, tmp_path)
        try:
            with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
                await client.ws_connect(
                    WS_PATH, headers={"authorization": "Bearer sk-expired-oauth"}
                )
            assert caught.value.status == 401
            # One 401 is the caller's problem, not the backend's — it must not trigger a cooldown.
            assert server.health.is_healthy("openai:passthrough") is True
            assert storage.read("all") == []
        finally:
            await _teardown(server, client, upstream)

    @pytest.mark.asyncio
    async def test_refuses_upgrades_when_websocket_proxying_is_disabled(self, tmp_path):
        upstream, seen = await fake_upstream(lambda data: [])
        server, client, _ = await gateway_for(upstream, tmp_path, websocket=False)
        try:
            with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
                await client.ws_connect(
                    WS_PATH, headers={"authorization": "Bearer sk-caller-oauth"}
                )
            assert caught.value.status == 501
            assert seen["handshakes"] == []
        finally:
            await _teardown(server, client, upstream)

    @pytest.mark.asyncio
    async def test_refuses_an_unknown_endpoint_without_touching_upstream(self, tmp_path):
        upstream, seen = await fake_upstream(lambda data: [])
        server, client, _ = await gateway_for(upstream, tmp_path)
        try:
            with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
                await client.ws_connect(
                    "/m/openai-codex/nope/v1/x",
                    headers={"authorization": "Bearer sk-caller-oauth"},
                )
            assert caught.value.status == 404
            assert seen["handshakes"] == []
        finally:
            await _teardown(server, client, upstream)

    @pytest.mark.asyncio
    async def test_refuses_when_no_backend_is_available(self, tmp_path):
        # A passthrough-only endpoint has nothing to use when the caller sends no credential.
        upstream, seen = await fake_upstream(lambda data: [])
        server, client, _ = await gateway_for(upstream, tmp_path)
        try:
            with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
                await client.ws_connect(WS_PATH)
            assert caught.value.status == 503
            assert seen["handshakes"] == []
        finally:
            await _teardown(server, client, upstream)


class TestUrlAndHeaderHelpers:
    @pytest.mark.parametrize("base,path,query,expected", [
        ("https://chatgpt.com/backend-api", "codex/responses", "", "wss://chatgpt.com/backend-api/codex/responses"),
        ("http://127.0.0.1:8080", "v1/rt", "", "ws://127.0.0.1:8080/v1/rt"),
        ("https://x.dev/api/", "/v1/rt", "model=gpt", "wss://x.dev/api/v1/rt?model=gpt"),
        ("https://x.dev/api", "", "", "wss://x.dev/api"),
    ])
    def test_maps_http_scheme_onto_websocket(self, base, path, query, expected):
        assert to_websocket_url(base, path, query) == expected

    def test_strips_only_the_regenerated_handshake_headers(self):
        headers = upstream_handshake_headers({
            "Host": "gateway.local",
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Key": "abc",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Extensions": "permessage-deflate",
            "Authorization": "Bearer sk-upstream",
            "Originator": "pi",
        })
        assert headers == {"Authorization": "Bearer sk-upstream", "Originator": "pi"}
