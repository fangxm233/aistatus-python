# input: gateway config, auth rules, aiohttp upstream requests, pricing, upload config/uploader helpers, and optional GATEWAY_DUMP_DIR env
# output: local gateway HTTP endpoints, proxied upstream responses, usage accounting, optional usage upload, and optional request+response JSON dumps
# pos: SDK gateway runtime that fronts upstream providers with health/fallback, usage collection, and optional dump of full API call payloads to GATEWAY_DUMP_DIR
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


class GatewayServer:
    def __init__(self, config: GatewayConfig, pid_file: str | None = None):
        self.config = config
        self.health = HealthTracker()
        self.usage = UsageTracker(uploader=UsageUploader(get_config()))
        self.pricing = CostCalculator()
        self._session: aiohttp.ClientSession | None = None
        self._key_idx: dict[str, int] = {}  # round-robin counters
        self._pid_file: Path | None = Path(pid_file) if pid_file else None
        dump_dir_env = os.environ.get("GATEWAY_DUMP_DIR") or None
        self._dump_dir: Path | None = Path(dump_dir_env) if dump_dir_env else None
        if self._dump_dir is not None:
            self._dump_dir.mkdir(parents=True, exist_ok=True)

    async def run(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300, connect=10),
            connector=aiohttp.TCPConnector(limit=100),
        )

        await self._apply_global_model_health_precheck()

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/status", self._handle_status)
        app.router.add_get("/usage", self._handle_usage)
        app.router.add_post("/mode", self._handle_mode_switch)
        # Per-request mode routing: /m/{mode}/{metadata?}/{endpoint}/{path}
        app.router.add_route("*", "/m/{tail:.*}", self._handle_mode_dispatch)
        # Catch-all proxy: /{endpoint}/...
        app.router.add_route("*", "/{endpoint}/{path:.*}", self._handle_proxy)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.config.host, self.config.port)
        await site.start()

        self._write_pid_file()
        self._print_banner()

        shutdown_event = asyncio.Event()
        self._install_signal_handlers(shutdown_event)

        try:
            await shutdown_event.wait()
            logger.info("Shutdown signal received, stopping gracefully...")
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
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
        if not endpoint:
            return web.json_response(
                {"error": {"message": f"Unknown endpoint: {ep_name}", "type": "gateway_error"}},
                status=404,
            )

        return await self._proxy_request(request, endpoint, path)

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

        if not backends:
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

        content_type = resp.headers.get("content-type", "")
        is_streaming = "text/event-stream" in content_type

        if is_streaming:
            return await self._stream(request, resp, backend, original_model, fallback_header, elapsed_ms, billing_mode, metadata, body)
        else:
            return await self._respond(resp, backend, original_model, elapsed_ms, fallback_header, billing_mode, metadata, body)

    async def _respond(
        self,
        upstream: aiohttp.ClientResponse,
        backend: dict[str, Any],
        original_model: str,
        elapsed_ms: int,
        fallback_header: str = "",
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
        request_body: bytes | None = None,
    ) -> web.Response:
        resp_body = await upstream.read()
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

        self._record_usage_if_possible(
            backend=backend,
            response_body=resp_body,
            original_model=original_model,
            elapsed_ms=elapsed_ms,
            billing_mode=billing_mode,
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
    ) -> web.StreamResponse:
        needs_translate = backend["translate"] == "anthropic-to-openai"
        dump_chunks: list[bytes] | None = [] if self._dump_dir is not None else None

        if needs_translate:
            resp = web.StreamResponse()
            resp.content_type = "text/event-stream"
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["Connection"] = "keep-alive"
            resp.headers["x-gateway-backend"] = backend["id"]
            if fallback_header:
                resp.headers["x-gateway-model-fallback"] = fallback_header
            await resp.prepare(request)

            from .translate import openai_sse_to_anthropic_sse

            raw_chunks: list[bytes] = []

            async def _chunks():
                async for chunk in upstream.content.iter_any():
                    raw_chunks.append(chunk)
                    yield chunk

            try:
                async for translated in openai_sse_to_anthropic_sse(_chunks(), original_model):
                    if dump_chunks is not None:
                        dump_chunks.append(translated)
                    await resp.write(translated)
            finally:
                upstream.release()
                usage = self._extract_usage_from_sse(raw_chunks)
                if usage is not None:
                    self._record_stream_usage(
                        backend=backend,
                        original_model=original_model,
                        input_tokens=usage["input_tokens"],
                        output_tokens=usage["output_tokens"],
                        cache_creation_input_tokens=usage["cache_creation_input_tokens"],
                        cache_read_input_tokens=usage["cache_read_input_tokens"],
                        billing_mode=billing_mode,
                        metadata=metadata,
                    )
                if dump_chunks is not None:
                    self._dump_api_call(
                        request_body, b"".join(dump_chunks) or None,
                        original_model, backend["id"], elapsed_ms,
                    )
            return resp
        else:
            resp = web.StreamResponse()
            _forward_upstream_headers(upstream.headers, resp.headers)
            resp.content_type = "text/event-stream"
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["Connection"] = "keep-alive"
            resp.headers["x-gateway-backend"] = backend["id"]
            if fallback_header:
                resp.headers["x-gateway-model-fallback"] = fallback_header
            await resp.prepare(request)

            try:
                async for chunk in upstream.content.iter_any():
                    if dump_chunks is not None:
                        dump_chunks.append(chunk)
                    await resp.write(chunk)
            finally:
                upstream.release()
                if dump_chunks is not None:
                    self._dump_api_call(
                        request_body, b"".join(dump_chunks) or None,
                        original_model, backend["id"], elapsed_ms,
                    )
            return resp

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

    def _record_stream_usage(
        self,
        *,
        backend: dict[str, Any],
        original_model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        model = original_model or f"{self._infer_provider_from_backend(backend, original_model)}/unknown"
        provider = self._infer_provider_from_backend(backend, model)
        if cache_creation_input_tokens or cache_read_input_tokens:
            cost = self.pricing.calculate_cost_with_cache(
                provider,
                model,
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_read_input_tokens,
            )
        else:
            cost = self.pricing.calculate_cost(provider, model, input_tokens, output_tokens)
        self.usage.record_usage(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            latency_ms=0,
            fallback=":fb:" in backend["id"],
            cost=cost,
            billing_mode=billing_mode or self.config.mode,
            metadata=metadata,
        )

    def _record_usage_if_possible(
        self,
        *,
        backend: dict[str, Any],
        response_body: bytes,
        original_model: str,
        elapsed_ms: int,
        billing_mode: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        model = original_model or payload.get("model") or ""
        usage = payload.get("usage") or {}

        input_tokens = self._as_int(
            usage.get("input_tokens", usage.get("prompt_tokens", 0))
        )
        output_tokens = self._as_int(
            usage.get("output_tokens", usage.get("completion_tokens", 0))
        )
        cache_creation_in = self._as_int(usage.get("cache_creation_input_tokens", 0))
        cache_read_in = self._as_int(usage.get("cache_read_input_tokens", 0))

        if not model and not input_tokens and not output_tokens:
            return

        provider = self._infer_provider_from_backend(backend, model)

        if cache_creation_in or cache_read_in:
            cost = self.pricing.calculate_cost_with_cache(
                provider,
                model or f"{provider}/unknown",
                input_tokens,
                output_tokens,
                cache_creation_in,
                cache_read_in,
            )
        else:
            cost = self.pricing.calculate_cost(
                provider,
                model or f"{provider}/unknown",
                input_tokens,
                output_tokens,
            )

        self.usage.record_usage(
            provider=provider,
            model=model or f"{provider}/unknown",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_in,
            cache_read_input_tokens=cache_read_in,
            latency_ms=elapsed_ms,
            fallback=":fb:" in backend["id"],
            cost=cost,
            billing_mode=billing_mode or self.config.mode,
            metadata=metadata,
        )

    @staticmethod
    def _extract_usage_from_sse(chunks: list[bytes]) -> dict[str, int] | None:
        payload = b"".join(chunks).decode("utf-8", errors="ignore")
        input_tokens = 0
        output_tokens = 0
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
        found = False
        for event in payload.split("\n\n"):
            for line in event.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                usage = parsed.get("usage") or {}
                if not usage:
                    continue
                input_tokens = GatewayServer._as_int(usage.get("prompt_tokens", usage.get("input_tokens", input_tokens)))
                output_tokens = GatewayServer._as_int(usage.get("completion_tokens", usage.get("output_tokens", output_tokens)))
                cache_creation_input_tokens = GatewayServer._as_int(usage.get("cache_creation_input_tokens", cache_creation_input_tokens))
                cache_read_input_tokens = GatewayServer._as_int(usage.get("cache_read_input_tokens", cache_read_input_tokens))
                found = True
        if not found:
            return None
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        }

    @staticmethod
    def _infer_provider_from_backend(backend: dict[str, Any], model: str) -> str:
        if "/" in model:
            return model.split("/", 1)[0]
        backend_id = backend.get("id", "")
        if backend_id.startswith("anthropic"):
            return "anthropic"
        if backend_id.startswith("openai"):
            return "openai"
        if backend_id.startswith("google"):
            return "google"
        if backend_id.startswith("openrouter"):
            return "openrouter"
        return backend_id.split(":", 1)[0] or "unknown"

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

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


class _ProxyError(Exception):
    """Retryable upstream error."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body
