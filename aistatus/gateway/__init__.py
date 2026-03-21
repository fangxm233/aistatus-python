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
    pid_file: str | None = None,
    log_file: str | None = None,
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
    import logging

    # Configure log file if requested
    if log_file:
        from pathlib import Path

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    from .config import GatewayConfig
    from .server import GatewayServer

    if auto:
        config = GatewayConfig.auto_discover(host=host, port=port)
    elif config_path:
        from pathlib import Path as _Path

        config = GatewayConfig.load(_Path(config_path))
    else:
        config = GatewayConfig.load()

    config.host = host
    config.port = port

    server = GatewayServer(config, pid_file=pid_file)
    asyncio.run(server.run())
