"""Tests for hot-reload of gateway config via reload_config() and the watcher loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.server import GatewayServer


def _make_server(mode: str = "default") -> GatewayServer:
    ep = EndpointConfig(
        name="openai",
        base_url="https://api.openai.com",
        auth_style="bearer",
        keys=["k1"],
    )
    cfg = GatewayConfig(
        host="127.0.0.1",
        port=9999,
        status_check=False,
        mode=mode,
        endpoints={"openai": ep},
        endpoint_modes={mode: {"openai": ep}},
    )
    return GatewayServer(cfg)


def test_reload_config_swaps_endpoints_and_pins_host_port():
    s = _make_server()
    new_ep = EndpointConfig(
        name="anthropic",
        base_url="https://api.anthropic.com",
        auth_style="anthropic",
        keys=["k2"],
    )
    new_cfg = GatewayConfig(
        host="0.0.0.0",  # should be ignored — already bound
        port=1234,       # should be ignored
        status_check=False,
        mode="default",
        endpoints={"anthropic": new_ep},
        endpoint_modes={"default": {"anthropic": new_ep}},
    )
    s.reload_config(new_cfg)
    assert s.config.host == "127.0.0.1"
    assert s.config.port == 9999
    assert list(s.config.endpoints.keys()) == ["anthropic"]


def test_reload_config_falls_back_when_active_mode_disappears():
    s = _make_server(mode="prod")
    assert s.config.mode == "prod"
    ep = EndpointConfig(name="openai", base_url="u", auth_style="bearer", keys=["x"])
    new_cfg = GatewayConfig(
        host="127.0.0.1",
        port=9999,
        status_check=False,
        mode="prod",
        endpoints={},
        endpoint_modes={"staging": {"openai": ep}},
    )
    s.reload_config(new_cfg)
    assert s.config.mode == "staging"
    assert s.config.endpoints["openai"].keys == ["x"]


@pytest.mark.asyncio
async def test_config_watcher_loop_reloads_on_file_change(tmp_path: Path):
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text("port: 9880\nopenai:\n  keys:\n    - k1\n")

    ep = EndpointConfig(name="openai", base_url="u", auth_style="bearer", keys=["k1"])
    cfg = GatewayConfig(
        host="127.0.0.1",
        port=9999,
        status_check=False,
        mode="default",
        endpoints={"openai": ep},
        endpoint_modes={"default": {"openai": ep}},
    )
    server = GatewayServer(cfg, config_path=cfg_path)

    task = asyncio.create_task(server._config_watcher_loop(interval=0.05))
    try:
        await asyncio.sleep(0.2)
        cfg_path.write_text("port: 9880\nopenai:\n  keys:\n    - k1\n    - k2\n")

        for _ in range(60):
            await asyncio.sleep(0.05)
            keys = server.config.endpoints.get("openai")
            if keys is not None and keys.keys == ["k1", "k2"]:
                break

        assert server.config.endpoints["openai"].keys == ["k1", "k2"]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_config_watcher_loop_survives_invalid_yaml(tmp_path: Path):
    """A malformed config update must not crash the watcher; previous config stays."""
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text("port: 9880\nopenai:\n  keys:\n    - k1\n")

    ep = EndpointConfig(name="openai", base_url="u", auth_style="bearer", keys=["k1"])
    cfg = GatewayConfig(
        host="127.0.0.1",
        port=9999,
        status_check=False,
        mode="default",
        endpoints={"openai": ep},
        endpoint_modes={"default": {"openai": ep}},
    )
    server = GatewayServer(cfg, config_path=cfg_path)

    task = asyncio.create_task(server._config_watcher_loop(interval=0.05))
    try:
        await asyncio.sleep(0.2)
        cfg_path.write_text("not: [valid yaml")  # broken
        await asyncio.sleep(0.3)
        # Server kept its previous config and is still alive
        assert server.config.endpoints["openai"].keys == ["k1"]
        assert not task.done()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
