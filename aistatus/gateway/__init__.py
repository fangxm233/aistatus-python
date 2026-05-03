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
    watch_config: bool = True,
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

    from pathlib import Path as _Path

    from .config import CONFIG_FILE, GatewayConfig
    from .server import GatewayServer

    resolved_config_path: _Path | None
    if auto:
        config = GatewayConfig.auto_discover(host=host, port=port)
        resolved_config_path = None
    elif config_path:
        resolved_config_path = _Path(config_path)
        config = GatewayConfig.load(resolved_config_path)
    else:
        resolved_config_path = CONFIG_FILE
        config = GatewayConfig.load()

    config.host = host
    config.port = port

    server = GatewayServer(
        config,
        pid_file=pid_file,
        config_path=resolved_config_path,
        watch_config=watch_config,
    )
    asyncio.run(server.run())
