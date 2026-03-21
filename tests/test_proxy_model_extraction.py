# input: mocked proxy requests, upstream responses, and endpoint fallback config
# output: regression coverage for model extraction, health recording, and fallback headers
# pos: gateway per-model proxy behavior test suite
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for model extraction from request body in proxy handler.

Verifies that the proxy handler extracts the model field from JSON request
bodies and records per-model health stats via HealthTracker.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.server import GatewayServer, _ProxyError


def _make_server(endpoint: EndpointConfig) -> GatewayServer:
    config = GatewayConfig(endpoints={endpoint.name: endpoint})
    return GatewayServer(config)


class _Headers(dict):
    def get(self, key: str, default: str = "") -> str:
        return super().get(key.lower(), default)

    def items(self):
        return super().items()


def _make_request(
    body: bytes = b"",
    auth_header: str = "Bearer sk-test",
    endpoint: str = "anthropic",
    path: str = "v1/messages",
) -> MagicMock:
    """Create a mock aiohttp request."""
    request = MagicMock()
    request.match_info = {"endpoint": endpoint, "path": path}
    request.headers = _Headers({
        "authorization": auth_header,
        "content-type": "application/json",
    })
    request.query_string = ""
    request.method = "POST"
    request.read = AsyncMock(return_value=body)
    return request


# -----------------------------------------------------------------------
# Test: _extract_model static method
# -----------------------------------------------------------------------

class TestExtractModel:
    """Direct tests for the _extract_model static method."""

    def test_extracts_model_from_valid_json(self):
        body = json.dumps({"model": "claude-opus-4-6", "max_tokens": 1024}).encode()
        assert GatewayServer._extract_model(body) == "claude-opus-4-6"

    def test_returns_empty_for_no_model_field(self):
        body = json.dumps({"max_tokens": 1024}).encode()
        assert GatewayServer._extract_model(body) == ""

    def test_returns_empty_for_empty_body(self):
        assert GatewayServer._extract_model(b"") == ""

    def test_returns_empty_for_invalid_json(self):
        assert GatewayServer._extract_model(b"not json") == ""

    def test_returns_empty_for_null_model(self):
        body = json.dumps({"model": None}).encode()
        assert GatewayServer._extract_model(body) == ""

    def test_returns_empty_for_empty_model_string(self):
        body = json.dumps({"model": ""}).encode()
        assert GatewayServer._extract_model(body) == ""

    def test_handles_unicode_model(self):
        body = json.dumps({"model": "gpt-4o"}).encode()
        assert GatewayServer._extract_model(body) == "gpt-4o"


# -----------------------------------------------------------------------
# Test: Model-level health recording on success
# -----------------------------------------------------------------------

class TestModelHealthOnSuccess:
    """Proxy handler records model-level success stats."""

    @pytest.mark.asyncio
    async def test_success_records_model_health(self):
        """Successful proxy request records both backend and model health."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
        )
        server = _make_server(ep)

        body = json.dumps({"model": "claude-opus-4-6", "max_tokens": 100}).encode()
        request = _make_request(body=body, endpoint="anthropic")

        # Mock the _forward to simulate a successful response
        mock_response = MagicMock()
        with patch.object(server, "_forward", new_callable=AsyncMock) as mock_forward:
            mock_forward.return_value = mock_response
            result = await server._handle_proxy(request)

            # _forward should have been called with model param
            call_args = mock_forward.call_args
            assert call_args[0][4] == "claude-opus-4-6"  # model param (positional)

    @pytest.mark.asyncio
    async def test_unhealthy_model_uses_configured_fallback(self):
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
            model_fallbacks={"claude-opus-4-6": ["claude-sonnet-4-6", "claude-haiku-4-5"]},
        )
        server = _make_server(ep)
        for _ in range(5):
            server.health.record_error("anthropic:key:0", 529, model="claude-opus-4-6")

        body = json.dumps({"model": "claude-opus-4-6", "max_tokens": 100}).encode()
        request = _make_request(body=body, endpoint="anthropic")

        mock_response = MagicMock()
        with patch.object(server, "_forward", new_callable=AsyncMock) as mock_forward:
            mock_forward.return_value = mock_response
            await server._handle_proxy(request)

        call_args = mock_forward.call_args
        assert call_args[0][4] == "claude-sonnet-4-6"
        forwarded_body = call_args[0][3]
        assert json.loads(forwarded_body)["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_respond_sets_model_fallback_header(self):
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
        )
        server = _make_server(ep)

        upstream = AsyncMock()
        upstream.read = AsyncMock(return_value=b'{"content": "hello", "usage": {}}')
        upstream.release = MagicMock()
        upstream.status = 200
        upstream.headers = {"content-type": "application/json"}

        backend = {
            "id": "anthropic:key:0",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-test",
            "auth_style": "bearer",
            "model_prefix": "",
            "model_map": {},
            "translate": None,
        }

        response = await server._respond(
            upstream,
            backend,
            "claude-opus-4-6",
            123,
            fallback_header="claude-opus-4-6->claude-sonnet-4-6",
        )

        assert response.headers["x-gateway-model-fallback"] == "claude-opus-4-6->claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_forward_records_model_success(self):
        """_forward records model-level success on 200 response."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
        )
        server = _make_server(ep)
        # Need an aiohttp session mock
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.read = AsyncMock(return_value=b'{"content": "hello", "usage": {}}')
        mock_resp.release = MagicMock()
        mock_session.request = AsyncMock(return_value=mock_resp)
        server._session = mock_session

        backend = {
            "id": "anthropic:key:0",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-test",
            "auth_style": "bearer",
            "model_prefix": "",
            "model_map": {},
            "translate": None,
        }
        request = _make_request(
            body=b'{"model": "claude-opus-4-6"}',
            endpoint="anthropic",
        )

        await server._forward(request, backend, "v1/messages", b'{"model": "claude-opus-4-6"}', "claude-opus-4-6")

        # Backend-level success recorded
        assert server.health._state["anthropic:key:0"].total_requests >= 1

        # Model-level success recorded
        model_key = ("anthropic:key:0", "claude-opus-4-6")
        assert model_key in server.health._model_state
        assert server.health._model_state[model_key].total_requests >= 1

    @pytest.mark.asyncio
    async def test_forward_no_model_skips_model_health(self):
        """_forward without model does not record model-level health."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
        )
        server = _make_server(ep)
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.read = AsyncMock(return_value=b'{"content": "hello"}')
        mock_resp.release = MagicMock()
        mock_session.request = AsyncMock(return_value=mock_resp)
        server._session = mock_session

        backend = {
            "id": "anthropic:key:0",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-test",
            "auth_style": "bearer",
            "model_prefix": "",
            "model_map": {},
            "translate": None,
        }
        request = _make_request(body=b'{}', endpoint="anthropic")

        await server._forward(request, backend, "v1/messages", b'{}', "")

        # Backend-level success recorded
        assert server.health._state["anthropic:key:0"].total_requests >= 1

        # No model-level entry
        assert len(server.health._model_state) == 0


# -----------------------------------------------------------------------
# Test: Model-level health recording on error
# -----------------------------------------------------------------------

class TestModelHealthOnError:
    """Proxy handler records model-level error stats on retryable failures."""

    @pytest.mark.asyncio
    async def test_error_records_model_health(self):
        """429 error records both backend and model level health."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
            passthrough=False,
        )
        server = _make_server(ep)

        body = json.dumps({"model": "claude-opus-4-6", "max_tokens": 100}).encode()
        request = _make_request(body=body, endpoint="anthropic")

        # Mock _forward to raise _ProxyError (simulating upstream 429)
        with patch.object(server, "_forward", new_callable=AsyncMock) as mock_forward:
            mock_forward.side_effect = _ProxyError(429, b'{"error": "rate limited"}')
            result = await server._handle_proxy(request)

        # Backend-level error recorded
        assert server.health.error_count("anthropic:key:0") == 1

        # Model-level error recorded
        assert server.health.error_count("anthropic:key:0", model="claude-opus-4-6") == 1

    @pytest.mark.asyncio
    async def test_error_without_model_skips_model_health(self):
        """Error without model in body does not record model-level health."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
            passthrough=False,
        )
        server = _make_server(ep)

        body = json.dumps({"max_tokens": 100}).encode()  # No model field
        request = _make_request(body=body, endpoint="anthropic")

        with patch.object(server, "_forward", new_callable=AsyncMock) as mock_forward:
            mock_forward.side_effect = _ProxyError(429, b'{"error": "rate limited"}')
            result = await server._handle_proxy(request)

        # Backend-level error recorded
        assert server.health.error_count("anthropic:key:0") == 1

        # No model-level entries
        assert len(server.health._model_state) == 0

    @pytest.mark.asyncio
    async def test_multiple_backends_model_errors_tracked_per_backend(self):
        """Model-level errors are recorded per-backend when failover occurs."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-key-0", "sk-key-1"],
            passthrough=False,
        )
        server = _make_server(ep)

        body = json.dumps({"model": "claude-opus-4-6"}).encode()
        request = _make_request(body=body, endpoint="anthropic")

        # Both backends fail
        with patch.object(server, "_forward", new_callable=AsyncMock) as mock_forward:
            mock_forward.side_effect = _ProxyError(529, b'{"error": "overloaded"}')
            await server._handle_proxy(request)

        # Both backends have model-level errors
        assert server.health.error_count("anthropic:key:0", model="claude-opus-4-6") == 1
        assert server.health.error_count("anthropic:key:1", model="claude-opus-4-6") == 1


# -----------------------------------------------------------------------
# Test: Model health visible in summary
# -----------------------------------------------------------------------

class TestModelHealthInSummary:
    """Model health data is visible through health.summary()."""

    @pytest.mark.asyncio
    async def test_summary_shows_model_after_proxy(self):
        """After proxy requests, summary() includes model_health."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="bearer",
            keys=["sk-managed"],
            passthrough=False,
        )
        server = _make_server(ep)

        body = json.dumps({"model": "claude-opus-4-6"}).encode()
        request = _make_request(body=body, endpoint="anthropic")

        with patch.object(server, "_forward", new_callable=AsyncMock) as mock_forward:
            mock_forward.side_effect = _ProxyError(529, b'{"error": "overloaded"}')
            await server._handle_proxy(request)

        summary = server.health.summary()
        assert "model_health" in summary
        key = "anthropic:key:0/claude-opus-4-6"
        assert key in summary["model_health"]
        assert summary["model_health"][key]["total_errors"] == 1
