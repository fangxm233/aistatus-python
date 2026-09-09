# input: Anthropic unified rate-limit response headers and persisted snapshot files
# output: regression coverage for quota header parsing, persistence validation, and /quota
# pos: gateway quota snapshot test suite (mirrors TS aistatus quota-snapshot coverage)
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for the provider quota snapshot store.

Anthropic reports subscription quota on `anthropic-ratelimit-unified-*` response headers. The
gateway is the only component that sees them, so it keeps the latest reading per provider on disk
for other tools to consult.
"""

from __future__ import annotations

import json

import pytest

from aistatus.gateway.quota_snapshot import QuotaSnapshotStore


def _store(tmp_path) -> QuotaSnapshotStore:
    return QuotaSnapshotStore(tmp_path / "quota.json")


def _headers(**kwargs) -> dict[str, str]:
    return {key.replace("_", "-"): value for key, value in kwargs.items()}


FIVE_HOUR = _headers(**{
    "anthropic_ratelimit_unified_5h_utilization": "0.42",
    "anthropic_ratelimit_unified_5h_reset": "1799999999",
})


class TestHeaderParsing:
    def test_reads_both_windows(self, tmp_path):
        store = _store(tmp_path)
        headers = {
            "anthropic-ratelimit-unified-5h-utilization": "0.42",
            "anthropic-ratelimit-unified-5h-reset": "1799999999",
            "anthropic-ratelimit-unified-7d-utilization": "0.13",
            "anthropic-ratelimit-unified-7d-reset": "1800000000",
        }
        assert store.observe(headers, provider="anthropic", mode="plan", status=200) is True

        [snapshot] = store.list()
        assert snapshot["provider"] == "anthropic"
        assert snapshot["mode"] == "plan"
        assert [w["type"] for w in snapshot["windows"]] == ["five_hour", "seven_day"]
        assert snapshot["windows"][0]["utilization"] == 0.42
        assert snapshot["windows"][1]["resets_at"] == 1800000000

    def test_a_429_records_the_rejected_window_as_full(self, tmp_path):
        store = _store(tmp_path)
        headers = {
            "anthropic-ratelimit-unified-status": "rejected",
            "anthropic-ratelimit-unified-representative-claim": "7d",
            "anthropic-ratelimit-unified-reset": "1800000123",
        }
        assert store.observe(headers, provider="anthropic", mode="plan", status=429) is True

        [snapshot] = store.list()
        assert snapshot["windows"] == [
            {"type": "seven_day", "utilization": 1.0, "resets_at": 1800000123}
        ]

    @pytest.mark.parametrize("status", [400, 401, 500, 503])
    def test_ignores_ordinary_error_responses(self, tmp_path, status):
        # Their headers need not describe real quota state; only 2xx and 429 are trusted.
        assert _store(tmp_path).observe(
            FIVE_HOUR, provider="anthropic", mode="plan", status=status
        ) is False

    def test_ignores_responses_without_quota_headers(self, tmp_path):
        assert _store(tmp_path).observe(
            {"content-type": "application/json"}, provider="anthropic", mode="plan", status=200
        ) is False

    @pytest.mark.parametrize("value", ["", "abc", "-1", "1.5", "NaN"])
    def test_rejects_out_of_range_utilization(self, tmp_path, value):
        headers = {
            "anthropic-ratelimit-unified-5h-utilization": value,
            "anthropic-ratelimit-unified-5h-reset": "1799999999",
        }
        assert _store(tmp_path).observe(
            headers, provider="anthropic", mode="plan", status=200
        ) is False

    def test_latest_reading_replaces_the_previous_one(self, tmp_path):
        store = _store(tmp_path)
        store.observe(FIVE_HOUR, provider="anthropic", mode="plan", status=200)
        store.observe({
            "anthropic-ratelimit-unified-5h-utilization": "0.91",
            "anthropic-ratelimit-unified-5h-reset": "1799999999",
        }, provider="anthropic", mode="plan", status=200)

        assert len(store.list()) == 1
        assert store.list()[0]["windows"][0]["utilization"] == 0.91


class TestPersistence:
    def test_survives_a_restart(self, tmp_path):
        store = _store(tmp_path)
        store.observe(FIVE_HOUR, provider="anthropic", mode="plan", status=200, now=1700000000)

        assert _store(tmp_path).list() == store.list()

    def test_written_file_is_owner_only(self, tmp_path):
        store = _store(tmp_path)
        store.observe(FIVE_HOUR, provider="anthropic", mode="plan", status=200)
        assert (tmp_path / "quota.json").stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("payload", [
        "not json",
        '{"version": 2, "providers": []}',
        '{"version": 1, "providers": "nope"}',
        '{"version": 1, "providers": [{"provider": "anthropic"}]}',
        '{"version": 1, "providers": [{"provider": "anthropic", "mode": "plan",'
        ' "observed_at": 1, "windows": [{"type": "decade", "utilization": 0.1, "resets_at": 2}]}]}',
        '{"version": 1, "providers": [{"provider": "anthropic", "mode": "plan",'
        ' "observed_at": 1, "windows": [{"type": "five_hour", "utilization": 9, "resets_at": 2}]}]}',
    ])
    def test_drops_malformed_entries_instead_of_serving_them(self, tmp_path, payload):
        # The file is user-writable state; a bad entry must not be handed back as a real reading.
        (tmp_path / "quota.json").write_text(payload, encoding="utf-8")
        assert _store(tmp_path).list() == []

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert _store(tmp_path).list() == []

    def test_filters_by_provider(self, tmp_path):
        store = _store(tmp_path)
        store.observe(FIVE_HOUR, provider="anthropic", mode="plan", status=200)
        assert store.list("anthropic") != []
        assert store.list("openai") == []


class TestQuotaEndpoint:
    @pytest.mark.asyncio
    async def test_serves_stored_snapshots(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        from aistatus.gateway.config import EndpointConfig, GatewayConfig
        from aistatus.gateway.server import GatewayServer

        endpoint = EndpointConfig(
            name="anthropic", base_url="http://127.0.0.1:1", auth_style="bearer", keys=[]
        )
        server = GatewayServer(
            GatewayConfig(endpoints={endpoint.name: endpoint}, status_check=False, mode="plan")
        )
        server.quota = _store(tmp_path)
        server.quota.observe(FIVE_HOUR, provider="anthropic", mode="plan", status=200)

        client = TestClient(TestServer(server.create_app()))
        await client.start_server()
        try:
            payload = await (await client.get("/quota")).json()
            assert payload["providers"][0]["provider"] == "anthropic"
            assert payload["providers"][0]["windows"][0]["utilization"] == 0.42

            empty = await (await client.get("/quota?provider=openai")).json()
            assert empty == {"providers": []}

            # /health now names the active mode, matching the TypeScript SDK.
            health = await (await client.get("/health")).json()
            assert health["mode"] == "plan"
        finally:
            await client.close()


class TestObserveQuotaGating:
    """Only OAuth passthrough traffic carries subscription quota headers worth recording."""

    @pytest.mark.parametrize(
        "backend_id,auth_style,expected",
        [
            ("anthropic:passthrough", "bearer", True),
            ("anthropic:key:0", "bearer", False),
            ("anthropic:passthrough", "anthropic", False),
            ("openai:passthrough", "bearer", False),
            ("anthropic:fb:openrouter", "bearer", False),
        ],
    )
    def test_gating(self, tmp_path, backend_id, auth_style, expected):
        from aistatus.gateway.config import EndpointConfig, GatewayConfig
        from aistatus.gateway.server import GatewayServer

        endpoint = EndpointConfig(
            name="anthropic", base_url="http://127.0.0.1:1", auth_style="bearer", keys=[]
        )
        server = GatewayServer(
            GatewayConfig(endpoints={endpoint.name: endpoint}, status_check=False)
        )
        server.quota = _store(tmp_path)

        upstream = type("Upstream", (), {"headers": FIVE_HOUR, "status": 200})()
        server._observe_quota(upstream, {"id": backend_id, "auth_style": auth_style}, "plan")

        assert (server.quota.list() != []) is expected


def test_snapshot_file_shape(tmp_path):
    """The on-disk format is consumed by other tools, so its shape is part of the contract."""
    store = _store(tmp_path)
    store.observe(FIVE_HOUR, provider="anthropic", mode="plan", status=200, now=1700000000)

    payload = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert payload == {
        "version": 1,
        "providers": [{
            "provider": "anthropic",
            "mode": "plan",
            "windows": [{"type": "five_hour", "utilization": 0.42, "resets_at": 1799999999}],
            "observed_at": 1700000000,
        }],
    }
