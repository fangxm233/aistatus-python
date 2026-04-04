# input: gateway endpoint configs, mocked requests, health state, and upload config wiring
# output: regression coverage for backend ordering, config parsing, status data, and gateway usage uploader wiring
# pos: gateway backend selection, config-loading, and usage integration test suite
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for hybrid backend selection in the gateway.

Verifies that _build_backend_list produces correct backend lists for:
- hybrid mode (managed keys + passthrough)
- managed-only mode (keys only, passthrough=False)
- passthrough-only mode (no keys)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aistatus.config import AIStatusConfig
from aistatus.gateway.config import EndpointConfig, FallbackConfig, GatewayConfig
from aistatus.gateway.health import HealthTracker
from aistatus.gateway.server import GatewayServer


def _make_server(endpoint: EndpointConfig) -> GatewayServer:
    config = GatewayConfig(endpoints={endpoint.name: endpoint})
    return GatewayServer(config)


class _Headers(dict):
    """Dict subclass that allows case-insensitive .get() and .items()."""

    def get(self, key: str, default: str = "") -> str:
        return super().get(key.lower(), default)

    def items(self):
        return super().items()


def _make_request(auth_header: str = "Bearer sk-caller-key") -> MagicMock:
    """Create a mock aiohttp request with an Authorization header."""
    request = MagicMock()
    request.headers = _Headers({
        "authorization": auth_header,
        "content-type": "application/json",
    })
    return request


def _make_anthropic_request(api_key: str = "sk-ant-caller") -> MagicMock:
    """Create a mock request with Anthropic x-api-key header."""
    request = MagicMock()
    request.headers = _Headers({
        "x-api-key": api_key,
        "content-type": "application/json",
    })
    return request


# -----------------------------------------------------------------------
# Test: Hybrid mode — managed keys + passthrough in same endpoint
# -----------------------------------------------------------------------

class TestHybridBackendSelection:
    """Both passthrough and managed key paths in the same endpoint."""

    def test_hybrid_produces_managed_then_passthrough(self):
        """With keys + passthrough=True, backends should include both
        managed keys and passthrough (in that order)."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed-1", "sk-managed-2"],
            passthrough=True,
        )
        server = _make_server(ep)
        request = _make_request("Bearer sk-caller-own")

        backends = server._build_backend_list(ep, request)

        # Should have 2 managed + 1 passthrough = 3 backends
        assert len(backends) == 3

        # First two are managed keys
        assert backends[0]["id"] == "openai:key:0"
        assert backends[0]["api_key"] == "sk-managed-1"
        assert backends[1]["id"] == "openai:key:1"
        assert backends[1]["api_key"] == "sk-managed-2"

        # Third is passthrough with caller's key
        assert backends[2]["id"] == "openai:passthrough"
        assert backends[2]["api_key"] == "sk-caller-own"

    def test_hybrid_with_fallbacks(self):
        """Hybrid mode should order: managed → passthrough → fallbacks."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed-1"],
            passthrough=True,
            fallbacks=[
                FallbackConfig(
                    name="openrouter",
                    base_url="https://openrouter.ai/api/v1",
                    api_key="sk-or-key",
                ),
            ],
        )
        server = _make_server(ep)
        request = _make_request("Bearer sk-caller")

        backends = server._build_backend_list(ep, request)

        assert len(backends) == 3
        assert backends[0]["id"] == "openai:key:0"
        assert backends[1]["id"] == "openai:passthrough"
        assert backends[2]["id"] == "openai:fb:openrouter"

    def test_hybrid_skips_passthrough_when_no_incoming_key(self):
        """If caller sends no auth header, passthrough is skipped."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed-1"],
            passthrough=True,
        )
        server = _make_server(ep)
        request = _make_request("")  # empty auth header

        backends = server._build_backend_list(ep, request)

        assert len(backends) == 1
        assert backends[0]["id"] == "openai:key:0"

    def test_hybrid_anthropic_endpoint(self):
        """Hybrid mode works with Anthropic auth style (x-api-key)."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="anthropic",
            keys=["sk-ant-managed"],
            passthrough=True,
        )
        server = _make_server(ep)
        request = _make_anthropic_request("sk-ant-caller")

        backends = server._build_backend_list(ep, request)

        assert len(backends) == 2
        assert backends[0]["id"] == "anthropic:key:0"
        assert backends[0]["api_key"] == "sk-ant-managed"
        assert backends[1]["id"] == "anthropic:passthrough"
        assert backends[1]["api_key"] == "sk-ant-caller"

    def test_hybrid_unhealthy_managed_still_tries_passthrough(self):
        """If managed keys are unhealthy, passthrough should still appear."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed-1"],
            passthrough=True,
        )
        server = _make_server(ep)
        # Mark managed key as unhealthy
        server.health.record_error("openai:key:0", 429)
        server.health.record_error("openai:key:0", 429)
        server.health.record_error("openai:key:0", 429)
        server.health.record_error("openai:key:0", 429)
        server.health.record_error("openai:key:0", 429)

        request = _make_request("Bearer sk-caller")
        backends = server._build_backend_list(ep, request)

        # Managed key is unhealthy, only passthrough should remain
        assert len(backends) == 1
        assert backends[0]["id"] == "openai:passthrough"

    def test_gateway_creates_usage_uploader_from_config(self, monkeypatch):
        config = AIStatusConfig(upload_enabled=True, name="Alice", email="alice@example.com")
        uploader_instances = []

        class StubUploader:
            def __init__(self, passed_config):
                uploader_instances.append(passed_config)

        monkeypatch.setattr("aistatus.gateway.server.get_config", lambda: config)
        monkeypatch.setattr("aistatus.gateway.server.UsageUploader", StubUploader)

        server = _make_server(
            EndpointConfig(
                name="anthropic",
                base_url="https://api.anthropic.com",
                auth_style="anthropic",
            )
        )

        assert uploader_instances == [config]
        assert isinstance(server.usage.uploader, StubUploader)


# -----------------------------------------------------------------------
# Test: Managed-only mode — passthrough disabled
# -----------------------------------------------------------------------

class TestManagedOnlyMode:
    """passthrough=False with managed keys."""

    def test_managed_only_no_passthrough(self):
        """With passthrough=False, only managed keys appear."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed-1", "sk-managed-2"],
            passthrough=False,
        )
        server = _make_server(ep)
        request = _make_request("Bearer sk-caller")

        backends = server._build_backend_list(ep, request)

        assert len(backends) == 2
        assert all(":key:" in b["id"] for b in backends)
        # No passthrough backend
        assert not any(":passthrough" in b["id"] for b in backends)


# -----------------------------------------------------------------------
# Test: Passthrough-only mode (backward compat)
# -----------------------------------------------------------------------

class TestPassthroughOnlyMode:
    """No keys configured — pure passthrough like the old behavior."""

    def test_passthrough_only_forwards_caller_key(self):
        """Without managed keys, only passthrough backend is used."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=[],
            passthrough=True,
        )
        server = _make_server(ep)
        request = _make_request("Bearer sk-caller")

        backends = server._build_backend_list(ep, request)

        assert len(backends) == 1
        assert backends[0]["id"] == "openai:passthrough"
        assert backends[0]["api_key"] == "sk-caller"

    def test_passthrough_only_default_passthrough_true(self):
        """Default EndpointConfig has passthrough=True."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
        )
        assert ep.passthrough is True


# -----------------------------------------------------------------------
# Test: Status endpoint reflects mode correctly
# -----------------------------------------------------------------------

class TestStatusEndpoint:
    """_handle_status should report the mode (hybrid/managed/passthrough)."""

    @pytest.mark.asyncio
    async def test_status_reports_hybrid_mode(self):
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed"],
            passthrough=True,
        )
        server = _make_server(ep)
        request = MagicMock()

        resp = await server._handle_status(request)

        import json
        body = json.loads(resp.body)
        ep_info = body["endpoints"]["openai"]

        assert ep_info["mode"] == "hybrid"
        backend_ids = [b["id"] for b in ep_info["backends"]]
        assert "openai:key:0" in backend_ids
        assert "openai:passthrough" in backend_ids

    @pytest.mark.asyncio
    async def test_status_reports_managed_mode(self):
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-managed"],
            passthrough=False,
        )
        server = _make_server(ep)
        request = MagicMock()

        resp = await server._handle_status(request)

        import json
        body = json.loads(resp.body)
        assert body["endpoints"]["openai"]["mode"] == "managed"

    @pytest.mark.asyncio
    async def test_status_reports_passthrough_mode(self):
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=[],
            passthrough=True,
        )
        server = _make_server(ep)
        request = MagicMock()

        resp = await server._handle_status(request)

        import json
        body = json.loads(resp.body)
        assert body["endpoints"]["openai"]["mode"] == "passthrough"

    @pytest.mark.asyncio
    async def test_status_includes_model_health_field(self):
        """model_health should be a top-level field in /status response."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="anthropic",
            keys=["sk-ant-managed"],
            passthrough=True,
        )
        server = _make_server(ep)
        request = MagicMock()

        resp = await server._handle_status(request)

        import json
        body = json.loads(resp.body)
        assert "model_health" in body
        # Empty when no model-level data recorded
        assert body["model_health"] == {}

    @pytest.mark.asyncio
    async def test_status_model_health_with_errors(self):
        """model_health shows per-model healthy/unhealthy + recent_errors."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="anthropic",
            keys=["sk-ant-managed"],
            passthrough=True,
        )
        server = _make_server(ep)

        # Simulate model-level errors: opus rate-limited, sonnet ok
        bid = "anthropic:key:0"
        for _ in range(5):
            server.health.record_error(bid, 529, model="claude-opus-4-6")
        server.health.record_success(bid, model="claude-sonnet-4-6")

        request = MagicMock()
        resp = await server._handle_status(request)

        import json
        body = json.loads(resp.body)

        mh = body["model_health"]
        assert "anthropic:key:0/claude-opus-4-6" in mh
        assert mh["anthropic:key:0/claude-opus-4-6"]["healthy"] is False
        assert mh["anthropic:key:0/claude-opus-4-6"]["recent_errors"] >= 1

        assert "anthropic:key:0/claude-sonnet-4-6" in mh
        assert mh["anthropic:key:0/claude-sonnet-4-6"]["healthy"] is True
        assert mh["anthropic:key:0/claude-sonnet-4-6"]["total_errors"] == 0

    @pytest.mark.asyncio
    async def test_status_model_health_not_in_health_detail(self):
        """model_health should NOT appear nested inside health_detail."""
        ep = EndpointConfig(
            name="anthropic",
            base_url="https://api.anthropic.com",
            auth_style="anthropic",
            keys=["sk-ant-managed"],
        )
        server = _make_server(ep)

        # Record model-level data
        server.health.record_error("anthropic:key:0", 529, model="claude-opus-4-6")

        request = MagicMock()
        resp = await server._handle_status(request)

        import json
        body = json.loads(resp.body)

        # model_health should be top-level, not nested in health_detail
        assert "model_health" not in body["health_detail"]
        assert "model_health" in body


# -----------------------------------------------------------------------
# Test: Config loading parses passthrough field
# -----------------------------------------------------------------------

class TestConfigLoading:
    def test_from_dict_passthrough_true(self):
        raw = {
            "openai": {
                "keys": ["sk-test"],
                "passthrough": True,
            },
        }
        config = GatewayConfig._from_dict(raw)
        assert config.endpoints["openai"].passthrough is True

    def test_from_dict_passthrough_false(self):
        raw = {
            "openai": {
                "keys": ["sk-test"],
                "passthrough": False,
            },
        }
        config = GatewayConfig._from_dict(raw)
        assert config.endpoints["openai"].passthrough is False

    def test_from_dict_passthrough_default_true(self):
        raw = {
            "openai": {
                "keys": ["sk-test"],
            },
        }
        config = GatewayConfig._from_dict(raw)
        assert config.endpoints["openai"].passthrough is True

    def test_from_dict_parses_model_fallbacks(self):
        raw = {
            "anthropic": {
                "keys": ["sk-test"],
                "model_fallbacks": {
                    "claude-opus-4-6": ["claude-sonnet-4-6", "claude-haiku-4-5"],
                },
            },
        }
        config = GatewayConfig._from_dict(raw)
        assert config.endpoints["anthropic"].model_fallbacks == {
            "claude-opus-4-6": ["claude-sonnet-4-6", "claude-haiku-4-5"],
        }

    def test_from_dict_model_fallbacks_default_empty(self):
        raw = {
            "anthropic": {
                "keys": ["sk-test"],
            },
        }
        config = GatewayConfig._from_dict(raw)
        assert config.endpoints["anthropic"].model_fallbacks == {}

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            (["claude-opus-4-6"], "model_fallbacks must be a mapping"),
            ({"claude-opus-4-6": "claude-sonnet-4-6"}, "fallback list must be a non-empty list"),
            ({"claude-opus-4-6": []}, "fallback list must be a non-empty list"),
            ({"claude-opus-4-6": [""]}, "fallback target must be a non-empty string"),
            ({"": ["claude-sonnet-4-6"]}, "source model must be a non-empty string"),
        ],
    )
    def test_from_dict_rejects_invalid_model_fallbacks(self, value, message):
        raw = {
            "anthropic": {
                "keys": ["sk-test"],
                "model_fallbacks": value,
            },
        }
        with pytest.raises(ValueError, match=message):
            GatewayConfig._from_dict(raw)

    def test_auto_discover_sets_passthrough_true(self):
        """Auto-discovered endpoints should have passthrough=True by default."""
        import os
        old = os.environ.get("OPENAI_API_KEY")
        try:
            os.environ["OPENAI_API_KEY"] = "sk-test-auto"
            config = GatewayConfig.auto_discover()
            if "openai" in config.endpoints:
                assert config.endpoints["openai"].passthrough is True
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old
            elif "OPENAI_API_KEY" in os.environ:
                del os.environ["OPENAI_API_KEY"]



# -----------------------------------------------------------------------
# Test: Key rotation still works in hybrid mode
# -----------------------------------------------------------------------

class TestKeyRotation:
    def test_key_rotation_round_robin(self):
        """Managed keys rotate even in hybrid mode."""
        ep = EndpointConfig(
            name="openai",
            base_url="https://api.openai.com",
            auth_style="bearer",
            keys=["sk-a", "sk-b"],
            passthrough=True,
        )
        server = _make_server(ep)
        request = _make_request("Bearer sk-caller")

        # First call: rotation starts at 0 → [key:0, key:1, passthrough]
        b1 = server._build_backend_list(ep, request)
        assert b1[0]["api_key"] == "sk-a"
        assert b1[1]["api_key"] == "sk-b"

        # Second call: rotation advances → [key:1, key:0, passthrough]
        b2 = server._build_backend_list(ep, request)
        assert b2[0]["api_key"] == "sk-b"
        assert b2[1]["api_key"] == "sk-a"
        # Passthrough always last among primary backends
        assert b2[2]["id"] == "openai:passthrough"
