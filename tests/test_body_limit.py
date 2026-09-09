# input: GatewayConfig max_body_size_mb values and oversized POST bodies against a live app
# output: regression coverage that large coding-agent payloads are not rejected with 413
# pos: gateway request body size limit test suite (mirrors TS aistatus body-limit coverage)
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for the configurable request body limit.

aiohttp defaults `client_max_size` to 1 MiB. The gateway fronts coding agents whose ordinary
requests (system prompt plus accumulated context) routinely exceed that, so a bare
`web.Application()` made the gateway unusable for its actual traffic.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from aistatus.gateway.config import DEFAULT_MAX_BODY_SIZE_MB, EndpointConfig, GatewayConfig


def _config(**overrides) -> GatewayConfig:
    endpoint = EndpointConfig(
        name="anthropic",
        base_url="http://127.0.0.1:1/v1",
        auth_style="x-api-key",
        keys=["sk-test"],
    )
    return GatewayConfig(endpoints={endpoint.name: endpoint}, status_check=False, **overrides)


async def _post_body(config: GatewayConfig, size_bytes: int) -> int:
    """POST `size_bytes` of padding at the proxy route and return the status code.

    The upstream base_url points at a closed port, so a body that clears the size check fails
    later in the proxy. Any status other than 413 therefore means the limit let the body through.
    """
    from aistatus.gateway.server import GatewayServer

    server = GatewayServer(config)
    client = TestClient(TestServer(server.create_app()))
    await client.start_server()
    try:
        response = await client.post(
            "/anthropic/v1/messages",
            data=b"x" * size_bytes,
            headers={"content-type": "application/json"},
        )
        return response.status
    finally:
        await client.close()


class TestBodyLimit:
    def test_default_is_100_mb(self):
        assert DEFAULT_MAX_BODY_SIZE_MB == 100
        assert _config().max_body_size_mb == 100

    @pytest.mark.asyncio
    async def test_accepts_body_above_the_aiohttp_default(self):
        # 2 MiB — over aiohttp's 1 MiB default, well under the gateway's 100 MB.
        status = await _post_body(_config(), 2 * 1024 * 1024)
        assert status != 413

    @pytest.mark.asyncio
    async def test_rejects_body_above_the_configured_limit(self):
        status = await _post_body(_config(max_body_size_mb=1), 2 * 1024 * 1024)
        assert status == 413

    @pytest.mark.asyncio
    async def test_configured_limit_raises_the_ceiling(self):
        status = await _post_body(_config(max_body_size_mb=8), 4 * 1024 * 1024)
        assert status != 413


class TestBodyLimitConfig:
    def test_parses_from_yaml_dict(self):
        config = GatewayConfig._from_dict({
            "max_body_size_mb": 5,
            "anthropic": {"base_url": "https://api.anthropic.com/v1"},
        })
        assert config.max_body_size_mb == 5.0

    @pytest.mark.parametrize("value", [0, -1, "abc", True, float("inf"), float("nan")])
    def test_rejects_non_positive_and_non_finite(self, value):
        with pytest.raises(ValueError, match="max_body_size_mb"):
            GatewayConfig._from_dict({
                "max_body_size_mb": value,
                "anthropic": {"base_url": "https://api.anthropic.com/v1"},
            })

    def test_is_reserved_and_not_treated_as_an_endpoint(self):
        config = GatewayConfig._from_dict({
            "max_body_size_mb": 5,
            "anthropic": {"base_url": "https://api.anthropic.com/v1"},
        })
        assert list(config.endpoints) == ["anthropic"]

    def test_omitted_falls_back_to_the_default(self):
        config = GatewayConfig._from_dict({
            "anthropic": {"base_url": "https://api.anthropic.com/v1"},
        })
        assert config.max_body_size_mb == DEFAULT_MAX_BODY_SIZE_MB
