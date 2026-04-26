# input: mocked aiohttp upstream responses, GATEWAY_DUMP_DIR env, and per-request mode metadata in URL
# output: regression coverage for URL metadata parsing, UsageTracker metadata pass-through, reserved-key filtering, and request+response dumps
# pos: gateway metadata + dump-dir regression test suite (mirrors TS aistatus tests/gateway-mode.test.mjs and tests/usage.test.mjs)
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for per-request URL metadata, UsageTracker metadata, and GATEWAY_DUMP_DIR."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.server import GatewayServer
from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage


# -----------------------------------------------------------------------
# Test: _parse_url_metadata
# -----------------------------------------------------------------------

class TestParseUrlMetadata:
    def test_parses_simple_pairs(self):
        result = GatewayServer._parse_url_metadata("project=dex-hand,trigger=dispatch")
        assert result == {"project": "dex-hand", "trigger": "dispatch"}

    def test_url_decodes_keys_and_values(self):
        result = GatewayServer._parse_url_metadata("project=my%20project,trigger=user%2Fweb")
        assert result == {"project": "my project", "trigger": "user/web"}

    def test_skips_pairs_without_equals(self):
        result = GatewayServer._parse_url_metadata("malformed,key=value")
        assert result == {"key": "value"}

    def test_returns_empty_dict_for_empty_input(self):
        assert GatewayServer._parse_url_metadata("") == {}


# -----------------------------------------------------------------------
# Test: UsageTracker.record_usage metadata pass-through
# -----------------------------------------------------------------------

class TestUsageTrackerMetadata:
    def test_records_optional_metadata_fields(self, tmp_path: Path):
        storage = UsageStorage(base_dir=tmp_path, cwd="/test/project1")
        tracker = UsageTracker(storage=storage)

        record = tracker.record_usage(
            provider="anthropic",
            model="claude-opus-4-6",
            input_tokens=10,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            latency_ms=123,
            fallback=False,
            billing_mode="plan",
            metadata={"project": "dex-hand", "trigger": "dispatch"},
        )

        assert record["project"] == "dex-hand"
        assert record["trigger"] == "dispatch"
        assert record["billing_mode"] == "plan"

        records = storage.read("all")
        assert len(records) == 1
        assert records[0]["project"] == "dex-hand"
        assert records[0]["trigger"] == "dispatch"

    def test_metadata_does_not_overwrite_reserved_fields(self, tmp_path: Path):
        storage = UsageStorage(base_dir=tmp_path, cwd="/test/project2")
        tracker = UsageTracker(storage=storage)

        record = tracker.record_usage(
            provider="anthropic",
            model="claude-opus-4-6",
            input_tokens=10,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            latency_ms=123,
            fallback=False,
            metadata={"model": "evil-override", "project": "legit"},
        )

        assert record["model"] == "claude-opus-4-6"
        assert record["project"] == "legit"


# -----------------------------------------------------------------------
# Test: _handle_mode_dispatch URL parsing
# -----------------------------------------------------------------------

def _make_server_with_modes(tmp_dir: Path) -> GatewayServer:
    ep = EndpointConfig(
        name="anthropic",
        base_url="https://api.anthropic.com",
        auth_style="anthropic",
        keys=["sk-ant-test"],
        passthrough=False,
    )
    plan_ep = EndpointConfig(
        name="anthropic",
        base_url="https://api.anthropic.com",
        auth_style="anthropic",
        keys=["sk-plan-key"],
        passthrough=False,
    )
    config = GatewayConfig(
        endpoints={ep.name: ep},
        endpoint_modes={
            "api": {ep.name: ep},
            "plan": {plan_ep.name: plan_ep},
        },
    )
    server = GatewayServer(config)
    storage = UsageStorage(base_dir=tmp_dir)
    server.usage = UsageTracker(storage=storage)
    return server


def _make_mode_request(tail: str) -> MagicMock:
    """Create an aiohttp-style mock request for the /m/{tail:.*} route."""
    request = MagicMock()
    request.match_info = {"tail": tail}
    request.headers = {"authorization": "Bearer sk-test", "content-type": "application/json"}
    request.method = "POST"
    request.read = AsyncMock(return_value=b'{"model":"claude-sonnet-4-6","messages":[]}')
    request.query_string = ""
    return request


class TestHandleModeDispatch:
    def test_4_segment_url_parses_metadata(self, tmp_path: Path):
        server = _make_server_with_modes(tmp_path)
        captured: dict[str, object] = {}

        async def fake_proxy(self, request, endpoint, path, billing_mode=None, metadata=None):
            captured["billing_mode"] = billing_mode
            captured["metadata"] = metadata
            captured["path"] = path
            return MagicMock()

        with patch.object(GatewayServer, "_proxy_request", fake_proxy), \
             patch.object(GatewayServer, "_check_auth", lambda self, req: True):
            request = _make_mode_request("plan/project=dex-hand,trigger=dispatch/anthropic/v1/messages")
            asyncio.run(server._handle_mode_dispatch(request))

        assert captured["billing_mode"] == "plan"
        assert captured["metadata"] == {"project": "dex-hand", "trigger": "dispatch"}
        assert captured["path"] == "v1/messages"

    def test_3_segment_url_falls_through_to_no_metadata(self, tmp_path: Path):
        """A 3-segment URL where parts[2] is NOT a valid endpoint must route via the 3-segment branch."""
        server = _make_server_with_modes(tmp_path)
        captured: dict[str, object] = {}

        async def fake_proxy(self, request, endpoint, path, billing_mode=None, metadata=None):
            captured["billing_mode"] = billing_mode
            captured["metadata"] = metadata
            captured["path"] = path
            return MagicMock()

        with patch.object(GatewayServer, "_proxy_request", fake_proxy), \
             patch.object(GatewayServer, "_check_auth", lambda self, req: True):
            # plan/anthropic/v1/messages — parts[2]="v1" is not an endpoint, so falls through
            request = _make_mode_request("plan/anthropic/v1/messages")
            asyncio.run(server._handle_mode_dispatch(request))

        assert captured["billing_mode"] == "plan"
        assert captured["metadata"] is None
        assert captured["path"] == "v1/messages"

    def test_unknown_mode_returns_400(self, tmp_path: Path):
        server = _make_server_with_modes(tmp_path)

        with patch.object(GatewayServer, "_check_auth", lambda self, req: True):
            request = _make_mode_request("nonexistent/anthropic/v1/messages")
            response = asyncio.run(server._handle_mode_dispatch(request))

        assert response.status == 400


# -----------------------------------------------------------------------
# Test: _dump_api_call (GATEWAY_DUMP_DIR)
# -----------------------------------------------------------------------

class TestDumpApiCall:
    def test_dump_dir_disabled_when_env_unset(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("GATEWAY_DUMP_DIR", raising=False)
        ep = EndpointConfig(name="anthropic", base_url="https://x", auth_style="anthropic", keys=["k"], passthrough=True)
        server = GatewayServer(GatewayConfig(endpoints={ep.name: ep}))
        assert server._dump_dir is None

    def test_dump_dir_created_when_env_set(self, tmp_path: Path, monkeypatch):
        dump_dir = tmp_path / "dumps"
        monkeypatch.setenv("GATEWAY_DUMP_DIR", str(dump_dir))
        ep = EndpointConfig(name="anthropic", base_url="https://x", auth_style="anthropic", keys=["k"], passthrough=True)
        server = GatewayServer(GatewayConfig(endpoints={ep.name: ep}))
        assert server._dump_dir == dump_dir
        assert dump_dir.is_dir()

    def test_dumps_request_and_response_to_json_file(self, tmp_path: Path, monkeypatch):
        dump_dir = tmp_path / "dumps"
        monkeypatch.setenv("GATEWAY_DUMP_DIR", str(dump_dir))
        ep = EndpointConfig(name="anthropic", base_url="https://x", auth_style="anthropic", keys=["k"], passthrough=True)
        server = GatewayServer(GatewayConfig(endpoints={ep.name: ep}))

        request_body = json.dumps({"model": "claude-sonnet-4-6", "messages": []}).encode()
        response_body = json.dumps({"id": "msg_abc", "usage": {"input_tokens": 10}}).encode()

        server._dump_api_call(request_body, response_body, "claude-sonnet-4-6", "anthropic:key:0", 123)

        files = list(dump_dir.glob("*.json"))
        assert len(files) == 1
        dump = json.loads(files[0].read_text(encoding="utf-8"))
        assert dump["model"] == "claude-sonnet-4-6"
        assert dump["backend"] == "anthropic:key:0"
        assert dump["latency_ms"] == 123
        assert dump["request"] == {"model": "claude-sonnet-4-6", "messages": []}
        assert dump["response"] == {"id": "msg_abc", "usage": {"input_tokens": 10}}

    def test_dump_skipped_when_request_body_empty(self, tmp_path: Path, monkeypatch):
        dump_dir = tmp_path / "dumps"
        monkeypatch.setenv("GATEWAY_DUMP_DIR", str(dump_dir))
        ep = EndpointConfig(name="anthropic", base_url="https://x", auth_style="anthropic", keys=["k"], passthrough=True)
        server = GatewayServer(GatewayConfig(endpoints={ep.name: ep}))

        server._dump_api_call(b"", b'{"ok":true}', "m", "b", 0)
        server._dump_api_call(None, b'{"ok":true}', "m", "b", 0)

        assert list(dump_dir.glob("*.json")) == []

    def test_dump_falls_back_to_text_for_non_json_response(self, tmp_path: Path, monkeypatch):
        dump_dir = tmp_path / "dumps"
        monkeypatch.setenv("GATEWAY_DUMP_DIR", str(dump_dir))
        ep = EndpointConfig(name="anthropic", base_url="https://x", auth_style="anthropic", keys=["k"], passthrough=True)
        server = GatewayServer(GatewayConfig(endpoints={ep.name: ep}))

        # SSE-style response — not parsable as JSON
        sse_body = b"event: message\ndata: {\"x\":1}\n\n"
        server._dump_api_call(b'{"model":"x"}', sse_body, "m", "b", 0)

        files = list(dump_dir.glob("*.json"))
        assert len(files) == 1
        dump = json.loads(files[0].read_text(encoding="utf-8"))
        assert dump["response"] == sse_body.decode("utf-8")
