"""aistatus gateway - local transparent proxy for automatic AI API failover.

Usage:
    python -m aistatus.gateway start [--auto] [--port 9880]
    python -m aistatus.gateway init
"""

from __future__ import annotations


def start(
    config_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 9880,
    auto: bool = False,
):
    """Start the gateway server."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        raise ImportError(
            "Gateway requires extra dependencies.\n"
            "Install with: pip install aistatus[gateway]"
        ) from None

    import asyncio

    from .config import GatewayConfig
    from .server import GatewayServer

    if auto:
        config = GatewayConfig.auto_discover(host=host, port=port)
    elif config_path:
        from pathlib import Path

        config = GatewayConfig.load(Path(config_path))
    else:
        config = GatewayConfig.load()

    config.host = host
    config.port = port

    server = GatewayServer(config)
    asyncio.run(server.run())
