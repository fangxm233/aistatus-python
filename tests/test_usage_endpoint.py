"""Tests for the /usage HTTP endpoint on GatewayServer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.server import GatewayServer
from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage


def _make_server(tmp_dir: Path) -> GatewayServer:
    ep = EndpointConfig(
        name="anthropic",
        base_url="https://api.anthropic.com",
        auth_style="anthropic",
        keys=["sk-ant-test"],
        passthrough=True,
    )
    config = GatewayConfig(endpoints={ep.name: ep})
    server = GatewayServer(config)
    # Replace usage tracker with one backed by isolated temp storage
    storage = UsageStorage(base_dir=tmp_dir)
    server.usage = UsageTracker(storage=storage)
    return server


def _make_request(query: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.query = query or {}
    return request


def _seed_usage(server: GatewayServer) -> None:
    """Seed some usage records into the server's tracker."""
    server.usage.record_usage(
        provider="anthropic",
        model="claude-opus-4-6",
        input_tokens=1000,
        output_tokens=500,
        latency_ms=200,
        fallback=False,
    )
    server.usage.record_usage(
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=800,
        output_tokens=300,
        latency_ms=150,
        fallback=False,
    )
    server.usage.record_usage(
        provider="openai",
        model="gpt-4o",
        input_tokens=600,
        output_tokens=200,
        latency_ms=100,
        fallback=True,
    )


# -----------------------------------------------------------------------
# Basic /usage endpoint
# -----------------------------------------------------------------------

class TestUsageEndpoint:

    @pytest.mark.asyncio
    async def test_usage_default_returns_summary(self, tmp_path):
        """GET /usage with no params returns summary for today."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request())
        body = json.loads(resp.body)

        assert "summary" in body
        assert body["summary"]["period"] == "today"
        assert body["summary"]["requests"] == 3
        assert body["summary"]["input_tokens"] == 2400
        assert body["summary"]["output_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_usage_group_by_model(self, tmp_path):
        """GET /usage?period=today&group_by=model returns per-model breakdown."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request({"period": "today", "group_by": "model"}))
        body = json.loads(resp.body)

        assert "summary" in body
        assert "models" in body
        assert body["summary"]["period"] == "today"

        models = {m["model"]: m for m in body["models"]}
        assert "claude-opus-4-6" in models
        assert "claude-sonnet-4-6" in models
        assert "gpt-4o" in models
        assert models["claude-opus-4-6"]["requests"] == 1
        assert models["claude-opus-4-6"]["input_tokens"] == 1000
        assert models["gpt-4o"]["fallback_requests"] == 1

    @pytest.mark.asyncio
    async def test_usage_group_by_provider(self, tmp_path):
        """GET /usage?group_by=provider returns per-provider breakdown."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request({"group_by": "provider"}))
        body = json.loads(resp.body)

        assert "summary" in body
        assert "providers" in body

        providers = {p["provider"]: p for p in body["providers"]}
        assert "anthropic" in providers
        assert "openai" in providers
        assert providers["anthropic"]["requests"] == 2
        assert providers["openai"]["requests"] == 1

    @pytest.mark.asyncio
    async def test_usage_period_week(self, tmp_path):
        """GET /usage?period=week works."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request({"period": "week"}))
        body = json.loads(resp.body)

        assert body["summary"]["period"] == "week"
        assert body["summary"]["requests"] == 3

    @pytest.mark.asyncio
    async def test_usage_period_month(self, tmp_path):
        """GET /usage?period=month works."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request({"period": "month"}))
        body = json.loads(resp.body)

        assert body["summary"]["period"] == "month"
        assert body["summary"]["requests"] == 3

    @pytest.mark.asyncio
    async def test_usage_period_all(self, tmp_path):
        """GET /usage?period=all works."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request({"period": "all"}))
        body = json.loads(resp.body)

        assert body["summary"]["period"] == "all"
        assert body["summary"]["requests"] == 3

    @pytest.mark.asyncio
    async def test_usage_empty(self, tmp_path):
        """GET /usage returns zero counts when no records exist."""
        server = _make_server(tmp_path)

        resp = await server._handle_usage(_make_request())
        body = json.loads(resp.body)

        assert body["summary"]["requests"] == 0
        assert body["summary"]["input_tokens"] == 0
        assert body["summary"]["cost_usd"] == 0

    @pytest.mark.asyncio
    async def test_usage_no_models_key_without_group_by(self, tmp_path):
        """Without group_by, response should not contain 'models' or 'providers'."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request())
        body = json.loads(resp.body)

        assert "models" not in body
        assert "providers" not in body

    @pytest.mark.asyncio
    async def test_usage_cost_is_calculated(self, tmp_path):
        """Usage records should have cost_usd > 0 for known models."""
        server = _make_server(tmp_path)
        _seed_usage(server)

        resp = await server._handle_usage(_make_request({"period": "today", "group_by": "model"}))
        body = json.loads(resp.body)

        assert body["summary"]["cost_usd"] >= 0


# -----------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------

class TestUsageValidation:

    @pytest.mark.asyncio
    async def test_invalid_period_returns_400(self, tmp_path):
        server = _make_server(tmp_path)
        resp = await server._handle_usage(_make_request({"period": "yesterday"}))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert "error" in body

    @pytest.mark.asyncio
    async def test_invalid_group_by_returns_400(self, tmp_path):
        server = _make_server(tmp_path)
        resp = await server._handle_usage(_make_request({"group_by": "endpoint"}))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert "error" in body


# -----------------------------------------------------------------------
# UsageStorage: "today" period
# -----------------------------------------------------------------------

class TestTodayPeriod:

    def test_period_since_today(self):
        """_period_since('today') returns start of current UTC day."""
        from datetime import datetime, timezone

        since = UsageStorage._period_since("today")
        assert since is not None
        now = datetime.now(timezone.utc)
        assert since.year == now.year
        assert since.month == now.month
        assert since.day == now.day
        assert since.hour == 0
        assert since.minute == 0
        assert since.second == 0
