# input: gateway config, auth rules, aiohttp upstream requests, pricing, upload config/uploader helpers, and optional GATEWAY_DUMP_DIR env
# output: local gateway HTTP endpoints, proxied upstream responses, usage accounting, optional usage upload, and optional request+response JSON dumps
# pos: SDK gateway runtime that fronts upstream providers with health/fallback, usage collection, hot config reload, and optional dump of full API call payloads to GATEWAY_DUMP_DIR
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

"""Gateway HTTP server — transparent proxy with failover and key rotation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from urllib.parse import unquote
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from ..api import StatusAPI
from ..models import Status
from ..pricing import CostCalculator
from ..uploader import UsageUploader
from ..usage import UsageTracker
from ..config import get_config
from .auth import check_gateway_auth
from .config import AUTH_STYLES, EndpointConfig, GatewayConfig
from .health import HealthTracker
from .quota_snapshot import QuotaSnapshotStore
from .stream_usage import StreamUsageParser, parse_usage_response, probe_upstream_body
from .usage_accounting import record_gateway_usage

logger = logging.getLogger("aistatus.gateway")


# Headers that must NOT be forwarded from upstream to the client:
#   - hop-by-hop (RFC 7230 §6.1)
#   - body-framing headers that are invalidated when we decode/re-encode the body
#   - headers the gateway sets itself (overridden after this helper runs)
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
    "content-type",
})


def _forward_upstream_headers(upstream_headers: Any, target: Any) -> None:
    """Copy all upstream response headers into target, skipping hop-by-hop and gateway-managed names."""
    for key, value in upstream_headers.items():
        lower = key.lower()
        if lower in _HOP_BY_HOP_HEADERS:
            continue
        if lower.startswith("x-gateway-"):
            continue
        target[key] = value


def _query_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _filter_usage_since(
    records: list[dict[str, Any]], since: str | None
) -> list[dict[str, Any]]:
    """Keep records strictly newer than `since`. An unparseable bound is ignored, not an error."""
    if not since:
        return records
    try:
        bound = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return records
    if bound.tzinfo is None:
        bound = bound.replace(tzinfo=timezone.utc)

    kept = []
    for record in records:
        try:
            ts = datetime.fromisoformat(str(record.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts > bound:
            kept.append(record)
    return kept


class GatewayServer:
    def __init__(
        self,
        config: GatewayConfig,
        pid_file: str | None = None,
        config_path: str | Path | None = None,
        watch_config: bool = True,
    ):
        self.config = config
        self.health = HealthTracker()
        self.usage = UsageTracker(uploader=UsageUploader(get_config()))
        self.pricing = CostCalculator()
        self.quota = QuotaSnapshotStore()
        self._session: aiohttp.ClientSession | None = None
        self._key_idx: dict[str, int] = {}  # round-robin counters
        self._pid_file: Path | None = Path(pid_file) if pid_file else None
        self._config_path: Path | None = Path(config_path) if config_path else None
        self._watch_config: bool = watch_config and self._config_path is not None
        self._watcher_task: asyncio.Task[None] | None = None
        dump_dir_env = os.environ.get("GATEWAY_DUMP_DIR") or None
        self._dump_dir: Path | None = Path(dump_dir_env) if dump_dir_env else None
        if self._dump_dir is not None:
            self._dump_dir.mkdir(parents=True, exist_ok=True)

    def reload_config(self, new_config: GatewayConfig) -> None:
        """Hot-swap gateway configuration in place.

        Preserves bound host/port and the health/usage trackers; resets the
        round-robin key index. Falls back to a still-available mode if the
        active mode disappears.
        """
        if new_config.host != self.config.host or new_config.port != self.config.port:
            logger.warning(
                "host/port change ignored on reload (already bound to %s:%s)",
                self.config.host, self.config.port,
            )
        new_config.host = self.config.host
        new_config.port = self.config.port

        if not new_config.endpoint_modes:
            new_config.endpoint_modes = {new_config.mode or "default": new_config.endpoints or {}}

        available_modes = list(new_config.endpoint_modes.keys())
        desired_mode = self.config.mode
        if desired_mode in new_config.endpoint_modes:
            active_mode = desired_mode
        else:
            active_mode = available_modes[0] if available_modes else "default"
        new_config.mode = active_mode
        new_config.endpoints = new_config.endpoint_modes.get(active_mode, {})

        self.config = new_config
        self._key_idx = {}
        logger.info("Config reloaded")
        # Schedule a follow-up health pre-check so newly listed models get probed.
        try:
            asyncio.get_running_loop().create_task(self._apply_global_model_health_precheck())
        except RuntimeError:
            # No running loop (e.g. called from sync context) — skip silently.
            pass

    async def _config_watcher_loop(self, interval: float = 1.0) -> None:
        """Poll the config file's mtime and trigger reload_config() on change."""
        if self._config_path is None:
            return
        path = self._config_path
        try:
            last_mtime = path.stat().st_mtime
        except FileNotFoundError:
            last_mtime = None
        logger.info("Watching config file for changes: %s", path)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    mtime = path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if last_mtime is not None and mtime == last_mtime:
                    continue
                last_mtime = mtime
                try:
                    new_config = GatewayConfig.load(path)
                    self.reload_config(new_config)
                except Exception as e:  # noqa: BLE001 — bad config must never crash gateway
                    logger.error("Config reload failed for %s: %s", path, e)
        except asyncio.CancelledError:
            return

    def create_app(self) -> web.Application:
        """Build the routed aiohttp application. Split out of :meth:`run` so tests can drive it."""
        # aiohttp defaults client_max_size to 1 MiB, which 413s ordinary coding-agent payloads.
        max_body_bytes = int(self.config.max_body_size_mb * 1024 * 1024)
        app = web.Application(client_max_size=max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/status", self._handle_status)
        app.router.add_get("/usage", self._handle_usage)
        app.router.add_get("/quota", self._handle_quota)
        app.router.add_post("/mode", self._handle_mode_switch)
        # Per-request mode routing: /m/{mode}/{metadata?}/{endpoint}/{path}
        app.router.add_route("*", "/m/{tail:.*}", self._handle_mode_dispatch)
        # Catch-all proxy: /{endpoint}/...
        app.router.add_route("*", "/{endpoint}/{path:.*}", self._handle_proxy)
        return app

    async def run(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, connect=10),
            connector=aiohttp.TCPConnector(limit=100),
        )

        await self._apply_global_model_health_precheck()

        app = self.create_app()

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()

        self._write_pid_file()
        self._print_banner()

        if self._watch_config and self._config_path is not None:
            self._watcher_task = asyncio.create_task(self._config_watcher_loop())

        shutdown_event = asyncio.Event()
        self._install_signal_handlers(shutdown_event)

        try:
            await shutdown_event.wait()
            logger.info("Shutdown signal received, stopping gracefully...")
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            if self._watcher_task is not None:
                self._watcher_task.cancel()
                try:
                    await self._watcher_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._watcher_task = None
            self._remove_pid_file()
            if self._session:
                await self._session.close()
            await runner.cleanup()
            logger.info("Gateway stopped")

    # ------------------------------------------------------------------
    # Auth middleware
    # ------------------------------------------------------------------

    def _check_auth(self, request: web.Request) -> bool:
        """Check request authorization against gateway auth config."""
        if not self.config.auth:
            return True
        headers = {k.lower(): v for k, v in request.headers.items()}
        return check_gateway_auth(self.config.auth, request.path, headers)

    # ------------------------------------------------------------------
    # Mode proxy handler
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_url_metadata(raw: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for pair in raw.split(","):
            eq_idx = pair.find("=")
            if eq_idx > 0:
                result[unquote(pair[:eq_idx])] = unquote(pair[eq_idx + 1:])
        return result

    async def _handle_mode_dispatch(self, request: web.Request) -> web.StreamResponse:
        """Handle per-request mode routing with optional metadata: /m/{mode}/{metadata?}/{endpoint}/{path}."""
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )

        tail = request.match_info["tail"]
        parts = tail.split("/", 3)

        if len(parts) < 3:
            return web.json_response(
                {"error": {"message": f"Invalid mode path: /m/{tail}", "type": "gateway_error"}},
                status=404,
            )

        mode = parts[0]
        mode_endpoints = self.config.endpoint_modes.get(mode)
        if not mode_endpoints:
            return web.json_response(
                {"error": {"message": f"Unknown mode: {mode}", "type": "gateway_error"}},
                status=400,
            )

        metadata: dict[str, str] | None = None

        # Try 4-segment: mode/metadata/endpoint/path
        if len(parts) >= 4:
            ep_candidate = parts[2]
            if ep_candidate in mode_endpoints:
                metadata = self._parse_url_metadata(parts[1])
                ep_name = ep_candidate
                path = parts[3] if len(parts) > 3 else ""
                endpoint = mode_endpoints[ep_name]
                return await self._proxy_request(request, endpoint, path, billing_mode=mode, metadata=metadata)

        # 3-segment: mode/endpoint/path
        ep_name = parts[1]
        path = "/".join(parts[2:])
        endpoint = mode_endpoints.get(ep_name)
        if not endpoint:
            return web.json_response(
                {"error": {"message": f"Unknown endpoint '{ep_name}' in mode '{mode}'", "type": "gateway_error"}},
                status=404,
            )

        return await self._proxy_request(request, endpoint, path, billing_mode=mode)

    # ------------------------------------------------------------------
    # Proxy handler
    # ------------------------------------------------------------------

    async def _handle_proxy(self, request: web.Request) -> web.StreamResponse:
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )

        ep_name = request.match_info["endpoint"]
        path = request.match_info["path"]

        endpoint = self.config.endpoints.get(ep_name)
        billing_mode: str | None = None

        if not endpoint:
            # Auto-discover: search all other modes for the requested endpoint
            for mode_name, mode_endpoints in self.config.endpoint_modes.items():
                if mode_name == self.config.mode:
                    continue
                found = mode_endpoints.get(ep_name)
                if found:
                    endpoint = found
                    billing_mode = mode_name
                    break

        if not endpoint:
            return web.json_response(
                {"error": {"message": f"Unknown endpoint: {ep_name}", "type": "gateway_error"}},
                status=404,
            )

        return await self._proxy_request(request, endpoint, path, billing_mode=billing_mode)

    async def _proxy_request(
        self,
        request: web.Request,
        endpoint: EndpointConfig,
        path: str,
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> web.StreamResponse:
        """Core proxy logic shared by both standard and mode-aware handlers."""
        body = await request.read()
        original_model = self._extract_model(body)
        backends = self._build_backend_list(endpoint, request)

        # When all backends are in cooldown, pick the one whose cooldown expires
        # soonest and try it anyway. This prevents a single transient 5xx from
        # blackholing all traffic for the full cooldown window — the retry may
        # succeed if the upstream recovered.
        if not backends:
            fallback_backend = self._pick_soonest_cooldown_backend(endpoint, request)
            if fallback_backend:
                backends = [fallback_backend]
            else:
                return web.json_response(
                    {"error": {"message": "All backends unavailable", "type": "gateway_error"}},
                    status=503,
                )

        last_err: _ProxyError | None = None
        for backend in backends:
            model, effective_body, fallback_header = self._apply_model_fallback(
                endpoint, backend["id"], body, original_model
            )
            try:
                return await self._forward(request, backend, path, effective_body, model, fallback_header, billing_mode, metadata)
            except _ProxyError as e:
                last_err = e
                self.health.record_error(backend["id"], e.status)
                if model:
                    self.health.record_error(backend["id"], e.status, model=model)
                logger.warning(
                    "%s → %d, trying next backend", backend["id"], e.status
                )

        # All failed — return last error
        if last_err:
            return web.Response(body=last_err.body, status=last_err.status,
                                content_type="application/json")
        return web.json_response(
            {"error": {"message": "All backends failed", "type": "gateway_error"}},
            status=503,
        )

    # ------------------------------------------------------------------
    # Mode switch handler
    # ------------------------------------------------------------------

    async def _handle_mode_switch(self, request: web.Request) -> web.Response:
        """Switch the active endpoint mode. POST /mode with {"mode": "prod"}."""
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "Invalid JSON body", "type": "gateway_error"}},
                status=400,
            )

        new_mode = data.get("mode")
        if not new_mode or new_mode not in self.config.endpoint_modes:
            available = list(self.config.endpoint_modes.keys())
            return web.json_response(
                {"error": {"message": f"Unknown mode: {new_mode}. Available: {available}", "type": "gateway_error"}},
                status=400,
            )

        self.config.mode = new_mode
        self.config.endpoints = self.config.endpoint_modes[new_mode]
        logger.info("Switched to mode: %s", new_mode)

        return web.json_response({
            "mode": new_mode,
            "endpoints": list(self.config.endpoints.keys()),
        })

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    def _build_backend_list(
        self, endpoint: EndpointConfig, request: web.Request
    ) -> list[dict[str, Any]]:
        backends: list[dict[str, Any]] = []
        ep = endpoint.name

        # 1. Managed keys (if any)
        if endpoint.keys:
            idx = self._key_idx.get(ep, 0)
            n = len(endpoint.keys)
            for i in range(n):
                ki = (idx + i) % n
                bid = f"{ep}:key:{ki}"
                if self.health.is_healthy(bid):
                    backends.append(self._primary_backend(bid, endpoint, endpoint.keys[ki]))
            self._key_idx[ep] = (idx + 1) % n

        # 2. Passthrough
        if not endpoint.keys or endpoint.passthrough:
            bid = f"{ep}:passthrough"
            if self.health.is_healthy(bid):
                incoming_key = self._extract_incoming_key(request, endpoint.auth_style)
                if incoming_key:
                    backends.append(self._primary_backend(bid, endpoint, incoming_key))

        # 3. Fallbacks
        for fb in endpoint.fallbacks:
            bid = f"{ep}:fb:{fb.name}"
            if not self.health.is_healthy(bid) or not fb.api_key:
                continue
            backends.append({
                "id": bid,
                "base_url": fb.base_url,
                "api_key": fb.api_key,
                "auth_style": fb.auth_style,
                "model_prefix": fb.model_prefix,
                "model_map": fb.model_map,
                "translate": fb.translate,
            })

        return backends

    def _pick_soonest_cooldown_backend(
        self, endpoint: EndpointConfig, request: web.Request
    ) -> dict[str, Any] | None:
        """Last-resort fallback when all backends are in cooldown.

        Enumerates every possible backend for the endpoint and returns the one
        whose cooldown expires soonest. This avoids an instant 503 when a single
        transient error marked the only backend unhealthy — the retry often
        succeeds because the upstream has already recovered.
        """
        ep = endpoint.name
        candidates: list[tuple[str, dict[str, Any]]] = []

        # Managed keys
        for i, key in enumerate(endpoint.keys):
            bid = f"{ep}:key:{i}"
            candidates.append((bid, self._primary_backend(bid, endpoint, key)))

        # Passthrough
        if not endpoint.keys or endpoint.passthrough:
            bid = f"{ep}:passthrough"
            incoming_key = self._extract_incoming_key(request, endpoint.auth_style)
            if incoming_key:
                candidates.append((bid, self._primary_backend(bid, endpoint, incoming_key)))

        # Fallbacks
        for fb in endpoint.fallbacks:
            if not fb.api_key:
                continue
            bid = f"{ep}:fb:{fb.name}"
            candidates.append((bid, {
                "id": bid,
                "base_url": fb.base_url,
                "api_key": fb.api_key,
                "auth_style": fb.auth_style,
                "model_prefix": fb.model_prefix,
                "model_map": fb.model_map,
                "translate": fb.translate,
            }))

        if not candidates:
            return None

        best = self.health.soonest_cooldown([bid for bid, _ in candidates])
        if not best:
            return candidates[0][1]
        for bid, backend in candidates:
            if bid == best[0]:
                return backend
        return None

    @staticmethod
    def _primary_backend(
        bid: str, endpoint: EndpointConfig, api_key: str
    ) -> dict[str, Any]:
        return {
            "id": bid,
            "base_url": endpoint.base_url,
            "api_key": api_key,
            "auth_style": endpoint.auth_style,
            "model_prefix": "",
            "model_map": {},
            "translate": None,
        }

    @staticmethod
    def _extract_incoming_key(request: web.Request, auth_style: str) -> str:
        if auth_style == "anthropic":
            return request.headers.get("x-api-key", "")
        if auth_style == "google":
            return request.headers.get("x-goog-api-key", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:]
        return auth

    # ------------------------------------------------------------------
    # Forward to upstream
    # ------------------------------------------------------------------

    async def _forward(
        self,
        request: web.Request,
        backend: dict[str, Any],
        path: str,
        body: bytes,
        model: str = "",
        fallback_header: str = "",
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> web.StreamResponse:
        assert self._session is not None

        needs_translate = backend["translate"] == "anthropic-to-openai"

        original_model = model
        if not original_model and needs_translate and body:
            try:
                original_model = json.loads(body).get("model", "")
            except Exception:
                pass

        effective_path = path
        if needs_translate and "v1/messages" in path:
            effective_path = "v1/chat/completions"

        base = backend["base_url"].rstrip("/")
        url = f"{base}/{effective_path}"
        if request.query_string:
            url += f"?{request.query_string}"

        headers = self._build_upstream_headers(request, backend)

        upstream_body = body
        if needs_translate and body:
            from .translate import anthropic_request_to_openai
            upstream_body = anthropic_request_to_openai(body)

        if body and (backend["model_map"] or backend["model_prefix"]):
            upstream_body = self._map_model(upstream_body, backend)

        # DeepSeek: inject empty thinking blocks for assistant messages that lack them.
        # DeepSeek API requires every assistant message in a multi-turn conversation to
        # carry its reasoning_content (even if empty). When the upstream returns
        # thinking="" the client may drop it; the gateway restores it before forwarding.
        if (billing_mode and "deepseek" in billing_mode
                and upstream_body
                and self._has_thinking_enabled(upstream_body)):
            upstream_body = self._ensure_thinking_blocks(upstream_body)

        t0 = time.monotonic()
        try:
            resp = await self._session.request(
                method=request.method,
                url=url,
                headers=headers,
                data=upstream_body,
                allow_redirects=False,
            )
        except aiohttp.ClientError as e:
            raise _ProxyError(502, json.dumps(
                {"error": {"message": f"Upstream connection error: {e}", "type": "gateway_error"}}
            ).encode())

        elapsed_ms = round((time.monotonic() - t0) * 1000)

        if resp.status in (429, 500, 502, 503, 529):
            err_body = await resp.read()
            resp.release()
            raise _ProxyError(resp.status, err_body)

        self.health.record_success(backend["id"])
        if model:
            self.health.record_success(backend["id"], model=model)
        self._observe_quota(resp, backend, billing_mode)

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return await self._stream(request, resp, backend, original_model, fallback_header, elapsed_ms, billing_mode, metadata, body)

        # The ChatGPT Codex backend labels its event streams `application/json`, so the body decides.
        probe = await probe_upstream_body(resp)
        if probe.kind == "event-stream":
            return await self._stream(request, resp, backend, original_model, fallback_header, elapsed_ms, billing_mode, metadata, body, head=probe.head)
        return await self._respond(resp, probe.body, backend, original_model, elapsed_ms, fallback_header, billing_mode, metadata, body)

    async def _respond(
        self,
        upstream: aiohttp.ClientResponse,
        resp_body: bytes,
        backend: dict[str, Any],
        original_model: str,
        elapsed_ms: int,
        fallback_header: str = "",
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
        request_body: bytes | None = None,
    ) -> web.Response:
        upstream.release()

        if backend["translate"] == "anthropic-to-openai":
            from .translate import openai_response_to_anthropic
            resp_body = openai_response_to_anthropic(resp_body, original_model)
            content_type = "application/json"
            charset = None
        else:
            raw_content_type = upstream.headers.get("content-type", "application/json")
            content_type, _, content_type_params = raw_content_type.partition(";")
            content_type = content_type.strip() or "application/json"
            charset = None
            if content_type_params:
                for param in content_type_params.split(";"):
                    key, _, value = param.partition("=")
                    if key.strip().lower() == "charset" and value.strip():
                        charset = value.strip().strip('"')
                        break

        response = web.Response(
            body=resp_body,
            status=upstream.status,
            content_type=content_type,
            charset=charset,
        )

        usage = parse_usage_response(resp_body, original_model)
        if usage is not None:
            await record_gateway_usage(
                backend=backend,
                usage=usage,
                elapsed_ms=elapsed_ms,
                pricing=self.pricing,
                tracker=self.usage,
                billing_mode=billing_mode,
                default_billing_mode=self.config.mode,
                metadata=metadata,
            )

        self._dump_api_call(request_body, resp_body, original_model, backend["id"], elapsed_ms)

        _forward_upstream_headers(upstream.headers, response.headers)
        response.headers["x-gateway-backend"] = backend["id"]
        response.headers["x-gateway-ms"] = str(elapsed_ms)
        if fallback_header:
            response.headers["x-gateway-model-fallback"] = fallback_header
        return response

    async def _stream(
        self,
        request: web.Request,
        upstream: aiohttp.ClientResponse,
        backend: dict[str, Any],
        original_model: str,
        fallback_header: str = "",
        elapsed_ms: int = 0,
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
        request_body: bytes | None = None,
        head: bytes = b"",
    ) -> web.StreamResponse:
        needs_translate = backend["translate"] == "anthropic-to-openai"
        dump_chunks: list[bytes] | None = [] if self._dump_dir is not None else None
        # Usage is always read off the UPSTREAM bytes, never the bytes sent to the client: on the
        # translate path the Anthropic-shaped stream is synthesized locally, and only the OpenAI
        # original carries the provider's own token counts.
        parser = StreamUsageParser(original_model)

        resp = web.StreamResponse()
        if not needs_translate:
            # Translate rewrites the body into a different protocol, so upstream's own
            # content headers would describe something the client is not receiving.
            _forward_upstream_headers(upstream.headers, resp.headers)
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["x-gateway-backend"] = backend["id"]
        if fallback_header:
            resp.headers["x-gateway-model-fallback"] = fallback_header
        await resp.prepare(request)

        async def _upstream_chunks():
            # `head` is what the content-type probe already pulled off the stream.
            if head:
                parser.push_bytes(head)
                yield head
            while True:
                try:
                    chunk = await upstream.content.readany()
                except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                    raise _UpstreamReadError(str(error)) from error
                if not chunk:
                    return
                parser.push_bytes(chunk)
                yield chunk

        if needs_translate:
            from .translate import openai_sse_to_anthropic_sse

            stream = openai_sse_to_anthropic_sse(_upstream_chunks(), original_model)
        else:
            stream = _upstream_chunks()

        completed = False
        try:
            async for chunk in stream:
                if dump_chunks is not None:
                    dump_chunks.append(chunk)
                await resp.write(chunk)
            completed = True
        except _UpstreamReadError:
            # An upstream that died after its terminal event still delivered a whole response.
            completed = parser.has_terminal_event()
            if not completed:
                logger.warning("Upstream stream interrupted before completion: %s", backend["id"])
        finally:
            upstream.release()

        if not completed:
            # Ending normally would hand the client a truncated stream that looks complete, and
            # billing the partial counts would record a response that was never delivered.
            self._abort_stream(request)
            return resp

        if parser.has_usage():
            await record_gateway_usage(
                backend=backend,
                usage=parser.usage,
                elapsed_ms=elapsed_ms,
                pricing=self.pricing,
                tracker=self.usage,
                billing_mode=billing_mode,
                default_billing_mode=self.config.mode,
                metadata=metadata,
            )
        if dump_chunks is not None:
            self._dump_api_call(
                request_body, b"".join(dump_chunks) or None,
                original_model, backend["id"], elapsed_ms,
            )
        return resp

    @staticmethod
    def _abort_stream(request: web.Request) -> None:
        """Tear the connection down so the client sees a failure, not a clean short response."""
        transport = request.transport
        if transport is not None:
            transport.abort()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_model(body: bytes) -> str:
        if not body:
            return ""
        try:
            return json.loads(body).get("model", "") or ""
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""

    async def _apply_global_model_health_precheck(self) -> None:
        if not self.config.status_check:
            return

        model_targets: set[str] = set()
        for endpoint in self.config.endpoints.values():
            model_targets.update(endpoint.model_fallbacks.keys())
            for candidates in endpoint.model_fallbacks.values():
                model_targets.update(candidates)

        if not model_targets:
            return

        client = StatusAPI()
        checks = await asyncio.gather(
            *(client.acheck_model(model) for model in sorted(model_targets)),
            return_exceptions=True,
        )
        degraded_models = {
            model
            for model, result in zip(sorted(model_targets), checks, strict=False)
            if not isinstance(result, Exception) and result.status in (Status.DEGRADED, Status.DOWN)
        }
        if not degraded_models:
            return

        for endpoint in self.config.endpoints.values():
            endpoint_models = set(endpoint.model_fallbacks.keys())
            for candidates in endpoint.model_fallbacks.values():
                endpoint_models.update(candidates)
            unhealthy_models = endpoint_models & degraded_models
            if not unhealthy_models:
                continue

            backend_ids = [f"{endpoint.name}:key:{i}" for i in range(len(endpoint.keys))]
            if not endpoint.keys or endpoint.passthrough:
                backend_ids.append(f"{endpoint.name}:passthrough")
            backend_ids.extend(f"{endpoint.name}:fb:{fb.name}" for fb in endpoint.fallbacks)

            for backend_id in backend_ids:
                for model in unhealthy_models:
                    self.health.record_error(backend_id, 529, model=model)
                    logger.info("Pre-marked %s model unhealthy from global status: %s", backend_id, model)

    def _apply_model_fallback(
        self,
        endpoint: EndpointConfig,
        backend_id: str,
        body: bytes,
        original_model: str,
    ) -> tuple[str, bytes, str]:
        if not body or not original_model:
            return original_model, body, ""

        if self.health.is_healthy(backend_id, model=original_model):
            return original_model, body, ""

        candidates = endpoint.model_fallbacks.get(original_model, [])
        if not candidates:
            return original_model, body, ""

        for candidate in candidates:
            if not self.health.is_healthy(backend_id, model=candidate):
                continue
            rewritten = self._replace_model(body, candidate)
            if rewritten != body:
                return candidate, rewritten, f"{original_model}->{candidate}"

        return original_model, body, ""

    @staticmethod
    def _build_upstream_headers(
        request: web.Request, backend: dict[str, Any]
    ) -> dict[str, str]:
        headers: dict[str, str] = {}

        skip = {
            "host", "authorization", "x-api-key", "x-goog-api-key",
            "content-length", "transfer-encoding", "connection",
        }
        for k, v in request.headers.items():
            if k.lower() not in skip:
                headers[k] = v

        style = AUTH_STYLES.get(backend["auth_style"], AUTH_STYLES["bearer"])
        header_name, prefix = style
        headers[header_name] = prefix + backend["api_key"]

        return headers

    @staticmethod
    def _replace_model(body: bytes, model: str) -> bytes:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body

        if not data.get("model"):
            return body
        data["model"] = model
        return json.dumps(data).encode()

    @staticmethod
    def _map_model(body: bytes, backend: dict[str, Any]) -> bytes:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body

        model = data.get("model")
        if not model:
            return body

        if model in backend["model_map"]:
            data["model"] = backend["model_map"][model]
        elif backend["model_prefix"]:
            data["model"] = backend["model_prefix"] + model

        return json.dumps(data).encode()

    @staticmethod
    def _has_thinking_enabled(body: bytes) -> bool:
        """Check whether the request has Anthropic extended thinking enabled.

        Looks for the ``thinking`` top-level field with type ``"enabled"`` or ``"auto"``.
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        thinking = data.get("thinking")
        if not isinstance(thinking, dict):
            return False
        return thinking.get("type") in ("enabled", "auto")

    @staticmethod
    def _ensure_thinking_blocks(body: bytes) -> bytes:
        """Inject empty thinking blocks for assistant messages that lack them.

        DeepSeek API requires every assistant message in a multi-turn conversation to
        carry its reasoning_content (even when empty). If the client dropped an empty
        thinking block, re-inject it so the upstream doesn't reject the request.
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body

        messages = data.get("messages")
        if not isinstance(messages, list):
            return body

        modified = False
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            if any(b.get("type") == "thinking" for b in content if isinstance(b, dict)):
                continue

            first_text_idx = None
            for i, b in enumerate(content):
                if isinstance(b, dict) and b.get("type") == "text":
                    first_text_idx = i
                    break
            if first_text_idx is None:
                continue

            content.insert(first_text_idx, {"type": "thinking", "thinking": ""})
            modified = True

        return json.dumps(data).encode() if modified else body

    def _dump_api_call(
        self,
        request_body: bytes | None,
        response_body: bytes | None,
        model: str,
        backend_id: str,
        elapsed_ms: int,
    ) -> None:
        """Dump request+response JSON to GATEWAY_DUMP_DIR. Failures must never break the proxy."""
        if self._dump_dir is None or not request_body:
            return
        try:
            now = datetime.now(timezone.utc)
            ts_iso = now.isoformat().replace("+00:00", "Z")
            file_name = ts_iso.replace(":", "-").replace(".", "-") + ".json"
            file_path = self._dump_dir / file_name
            try:
                request: Any = json.loads(request_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                request = request_body.decode("utf-8", errors="replace")
            response: Any = None
            if response_body:
                text = response_body.decode("utf-8", errors="replace")
                try:
                    response = json.loads(text)
                except json.JSONDecodeError:
                    response = text
            dump: dict[str, Any] = {
                "ts": ts_iso,
                "model": model or None,
                "backend": backend_id,
                "latency_ms": elapsed_ms,
                "request": request,
            }
            if response is not None:
                dump["response"] = response
            file_path.write_text(json.dumps(dump) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 — dump failure should never break the proxy
            logger.debug("Failed to dump API call", exc_info=True)

    # ------------------------------------------------------------------
    # Info endpoints
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        # Health check respects auth config (bypassed only when /health is in public_paths)
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )
        return web.json_response({
            "status": "ok",
            "mode": self.config.mode,
            "endpoints": list(self.config.endpoints.keys()),
        })

    async def _handle_status(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )

        info: dict[str, Any] = {}
        for ep_name, ep in self.config.endpoints.items():
            ep_info: dict[str, Any] = {"backends": [], "mode": "passthrough"}
            for i in range(len(ep.keys)):
                bid = f"{ep_name}:key:{i}"
                ep_info["backends"].append({
                    "id": bid, "type": "primary", "healthy": self.health.is_healthy(bid),
                })
            if not ep.keys or ep.passthrough:
                bid = f"{ep_name}:passthrough"
                ep_info["backends"].append({
                    "id": bid, "type": "passthrough", "healthy": self.health.is_healthy(bid),
                })
            if ep.keys and ep.passthrough:
                ep_info["mode"] = "hybrid"
            elif ep.keys:
                ep_info["mode"] = "managed"
            for fb in ep.fallbacks:
                bid = f"{ep_name}:fb:{fb.name}"
                ep_info["backends"].append({
                    "id": bid, "type": "fallback", "name": fb.name,
                    "healthy": self.health.is_healthy(bid),
                })
            info[ep_name] = ep_info

        health_summary = self.health.summary()
        model_health = health_summary.pop("model_health", {})

        return web.json_response({
            "mode": self.config.mode,
            "available_modes": list(self.config.endpoint_modes.keys()),
            "endpoints": info,
            "health_detail": health_summary,
            "model_health": model_health,
        })

    async def _handle_usage(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )

        if request.query.get("format") == "records":
            return self._usage_records_response(request)

        period = request.query.get("period", "today")
        group_by = request.query.get("group_by", "")

        valid_periods = ("today", "week", "month", "all")
        if period not in valid_periods:
            return web.json_response(
                {"error": {"message": f"Invalid period: {period}. Must be one of {valid_periods}", "type": "gateway_error"}},
                status=400,
            )

        valid_groups = ("", "model", "provider")
        if group_by not in valid_groups:
            return web.json_response(
                {"error": {"message": f"Invalid group_by: {group_by}. Must be one of {valid_groups[1:]}", "type": "gateway_error"}},
                status=400,
            )

        result: dict[str, Any] = {"summary": self.usage.summary(period=period)}

        if group_by == "model":
            result["models"] = self.usage.by_model(period=period)
        elif group_by == "provider":
            result["providers"] = self.usage.by_provider(period=period)

        return web.json_response(result)

    def _usage_records_response(self, request: web.Request) -> web.Response:
        """Return raw usage rows, newest file order, with `since` / `limit` / `offset` paging."""
        records = _filter_usage_since(self.usage.storage.read("all"), request.query.get("since"))
        limit = max(0, _query_int(request.query.get("limit"), 1000))
        offset = max(0, _query_int(request.query.get("offset"), 0))
        paged = records[offset : offset + limit] if limit > 0 else records[offset:]
        return web.json_response({"records": paged})

    async def _handle_quota(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "Unauthorized", "type": "gateway_error"}},
                status=401,
            )
        return web.json_response({"providers": self.quota.list(request.query.get("provider"))})

    def _observe_quota(
        self, upstream: aiohttp.ClientResponse, backend: dict[str, Any], billing_mode: str | None
    ) -> None:
        """Capture Anthropic's rate-limit headers.

        Only OAuth passthrough traffic is read: the unified rate-limit headers describe a
        subscription's quota, so a managed API key's response says nothing about it.
        """
        backend_id = backend.get("id", "")
        if not backend_id.startswith("anthropic:") or not backend_id.endswith(":passthrough"):
            return
        if backend.get("auth_style") != "bearer":
            return
        self.quota.observe(
            upstream.headers,
            provider="anthropic",
            mode=billing_mode or self.config.mode,
            status=upstream.status,
        )

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    @staticmethod
    def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            signal.signal(
                signal.SIGTERM,
                lambda s, f: loop.call_soon_threadsafe(shutdown_event.set),
            )

    # ------------------------------------------------------------------
    # PID file
    # ------------------------------------------------------------------

    def _write_pid_file(self) -> None:
        if not self._pid_file:
            return
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(os.getpid()), encoding="utf-8")
        logger.info("PID %d written to %s", os.getpid(), self._pid_file)

    def _remove_pid_file(self) -> None:
        if not self._pid_file:
            return
        try:
            self._pid_file.unlink(missing_ok=True)
            logger.info("PID file removed: %s", self._pid_file)
        except OSError as e:
            logger.warning("Failed to remove PID file %s: %s", self._pid_file, e)

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------

    def _print_banner(self):
        base = f"http://{self.config.host}:{self.config.port}"
        print()
        print(f"  aistatus gateway running on {base}")
        if self.config.mode != "default":
            print(f"  Active mode: {self.config.mode}")
        if self.config.auth and self.config.auth.enabled:
            print(f"  Authentication: enabled ({len(self.config.auth.keys)} key(s))")
        print()
        for ep_name, ep in self.config.endpoints.items():
            nk = len(ep.keys)
            nf = len(ep.fallbacks)
            if nk and ep.passthrough:
                key_info = f"{nk} key{'s' if nk != 1 else ''} + passthrough"
            elif nk:
                key_info = f"{nk} key{'s' if nk != 1 else ''}"
            else:
                key_info = "passthrough"
            fb_names = ", ".join(f.name for f in ep.fallbacks)
            fb_info = f" → fallback: {fb_names}" if fb_names else ""
            print(f"  /{ep_name}/*  ({key_info}{fb_info})")
        print()
        print("  Configure your CLI tools:")
        if "anthropic" in self.config.endpoints:
            print(f"    export ANTHROPIC_BASE_URL={base}/anthropic")
        if "openai" in self.config.endpoints:
            print(f"    export OPENAI_BASE_URL={base}/openai/v1")
        print()
        print(f"  Status:  {base}/status")
        print(f"  Health:  {base}/health")
        print(f"  Usage:   {base}/usage?period=today&group_by=model")
        if len(self.config.endpoint_modes) > 1:
            print(f"  Modes:   {list(self.config.endpoint_modes.keys())}")
        print()


class _UpstreamReadError(Exception):
    """Raised when reading the upstream response body fails partway through."""


class _ProxyError(Exception):
    """Retryable upstream error."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body
