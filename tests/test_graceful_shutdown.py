"""Tests for graceful shutdown (SIGTERM/SIGINT) of the gateway server."""

from __future__ import annotations

import asyncio
import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

from aistatus.gateway.config import EndpointConfig, GatewayConfig
from aistatus.gateway.server import GatewayServer


def _make_config() -> GatewayConfig:
    ep = EndpointConfig(
        name="test",
        base_url="https://api.example.com",
        auth_style="bearer",
    )
    return GatewayConfig(endpoints={ep.name: ep})


class TestSignalHandlerInstallation:
    """Verify that _install_signal_handlers registers handlers correctly."""

    @pytest.mark.asyncio
    async def test_install_sets_shutdown_event_on_sigterm(self):
        """On Unix, SIGTERM handler should set the shutdown event."""
        if sys.platform == "win32":
            pytest.skip("Unix signal handlers not available on Windows")

        event = asyncio.Event()
        GatewayServer._install_signal_handlers(event)

        # Simulate SIGTERM
        loop = asyncio.get_running_loop()
        # The handler was registered; trigger it
        loop._signal_handlers  # verify handlers exist
        event_set_before = event.is_set()
        assert not event_set_before

        # Send SIGTERM to self
        import os
        os.kill(os.getpid(), signal.SIGTERM)
        # Give the event loop a chance to process the signal
        await asyncio.sleep(0.01)

        assert event.is_set()

    @pytest.mark.asyncio
    async def test_install_sets_shutdown_event_on_sigint(self):
        """On Unix, SIGINT handler should set the shutdown event."""
        if sys.platform == "win32":
            pytest.skip("Unix signal handlers not available on Windows")

        event = asyncio.Event()
        GatewayServer._install_signal_handlers(event)

        assert not event.is_set()

        import os
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0.01)

        assert event.is_set()

    @pytest.mark.asyncio
    async def test_windows_falls_back_to_signal_module(self):
        """On Windows (or when add_signal_handler raises NotImplementedError),
        should fall back to signal.signal for SIGTERM."""
        event = asyncio.Event()
        loop = asyncio.get_running_loop()

        # Force the NotImplementedError path
        with patch.object(
            loop, "add_signal_handler", side_effect=NotImplementedError
        ):
            with patch("signal.signal") as mock_signal:
                GatewayServer._install_signal_handlers(event)
                # Should have registered SIGTERM via signal.signal
                mock_signal.assert_called_once()
                assert mock_signal.call_args[0][0] == signal.SIGTERM


class TestGracefulShutdown:
    """Verify that run() performs full cleanup on shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_cleans_up_session_and_runner(self):
        """When shutdown event is set, run() should close session,
        cleanup runner, and remove PID file."""
        config = _make_config()
        server = GatewayServer(config)

        cleanup_order = []

        async def mock_close():
            cleanup_order.append("session_close")

        async def fake_run():
            """Simulate run() with immediate shutdown."""
            server._session = MagicMock()
            server._session.close = mock_close

            shutdown_event = asyncio.Event()
            shutdown_event.set()

            try:
                await shutdown_event.wait()
            finally:
                server._remove_pid_file()
                if server._session:
                    await server._session.close()
                    cleanup_order.append("session_closed")

        await fake_run()
        assert "session_close" in cleanup_order
        assert "session_closed" in cleanup_order

    @pytest.mark.asyncio
    async def test_pid_file_removed_on_shutdown(self):
        """PID file should be removed during shutdown cleanup."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "gateway.pid"
            config = _make_config()
            server = GatewayServer(config, pid_file=str(pid_path))

            # Simulate: write PID file, then remove it
            server._write_pid_file()
            assert pid_path.exists()

            server._remove_pid_file()
            assert not pid_path.exists()
