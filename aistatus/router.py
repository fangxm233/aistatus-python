"""Core routing engine with auto-discovery and model-based resolution."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from .api import StatusAPI
from ._defaults import AUTO_PROVIDERS, MODEL_PREFIX_MAP, normalize_provider_slug
from .exceptions import (
    AllProvidersDown,
    ProviderCallFailed,
)
from .gateway.health import HealthTracker
from .middleware import (
    AfterResponseContext,
    BeforeRequestContext,
    Middleware,
)
from .models import (
    ChatMessage,
    ProviderCallOptions,
    ProviderConfig,
    RouteResponse,
    Status,
    StreamChunk,
)
from .providers.base import ProviderAdapter, create_adapter
from .usage import UsageTracker

log = logging.getLogger("aistatus")

DEFAULT_RETRY_DELAY = 1.0  # seconds


@dataclass
class _ResolvedCandidate:
    provider_slug: str
    adapter_key: str
    model_id: str


class StreamCallbacks:
    """Callbacks for route_stream_callbacks()."""
    def __init__(
        self,
        on_token: Callable[[str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
        on_complete: Callable[[], None] | None = None,
    ):
        self.on_token = on_token
        self.on_error = on_error
        self.on_usage = on_usage
        self.on_complete = on_complete


class Router:
    """Dynamic router for LLM requests with auto-discovery.

    Zero-config usage::

        router = Router()  # auto-discovers providers from env vars
        resp = router.route("Hello!", model="claude-sonnet-4-6")

    Custom provider::

        router = Router()
        router.register_provider(ProviderConfig(
            slug="my-vllm", adapter_type="openai",
            api_key="sk-xxx", base_url="http://localhost:8000/v1",
        ))

    Tier-based routing::

        router = Router()
        router.add_tier("fast", ["claude-haiku-4-5", "gpt-4o-mini"])
        resp = router.route("Hello!", tier="fast")
    """

    def __init__(
        self,
        *,
        base_url: str = "https://aistatus.cc",
        check_timeout: float = 3.0,
        providers: list[str] | None = None,
        auto_discover: bool = True,
        track_usage: bool = True,
        health_tracking: bool = True,
        middleware: list[Any] | None = None,
    ):
        self.api = StatusAPI(base_url=base_url, timeout=check_timeout)
        self.adapters: dict[str, ProviderAdapter] = {}
        self._adapter_index: dict[str, str] = {}  # alias → adapter key
        self._tiers: dict[str, list[str]] = {}
        self.usage = UsageTracker() if track_usage else None
        self.health: HealthTracker | None = HealthTracker() if health_tracking else None
        self._middleware: list[Any] = list(middleware or [])

        if auto_discover:
            self._auto_discover(only=providers)

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def use(self, mw: Any) -> None:
        """Add a middleware to the chain."""
        self._middleware.append(mw)

    def _auto_discover(self, only: list[str] | None = None):
        """Scan environment variables and register adapters for providers that have API keys."""
        for slug, spec in AUTO_PROVIDERS.items():
            if only and slug not in only:
                continue
            if os.environ.get(spec.env_var):
                try:
                    config = ProviderConfig(
                        slug=slug,
                        adapter_type=spec.adapter_type,
                        env=spec.env_var,
                        aliases=spec.aliases if spec.aliases else None,
                    )
                    self.register_provider(config)
                    log.debug("Auto-discovered provider: %s", slug)
                except Exception as e:
                    log.debug("Skipping provider %s: %s", slug, e)

    def register_provider(self, config: ProviderConfig):
        """Manually register a provider with custom configuration."""
        adapter = create_adapter(config)
        self.adapters[config.slug] = adapter
        self._index_adapter(adapter, config)

    def _index_adapter(self, adapter: ProviderAdapter, config: ProviderConfig):
        """Index adapter by its slug and any aliases."""
        slug = normalize_provider_slug(config.slug)
        self._adapter_index[slug] = config.slug
        for alias in adapter.aliases:
            normalized_alias = normalize_provider_slug(alias)
            self._adapter_index[normalized_alias] = config.slug

    def add_tier(self, name: str, models: list[str]):
        """Configure a tier as an ordered list of model names to try."""
        self._tiers[name] = list(models)

    # ------------------------------------------------------------------
    # Message normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_messages(
        messages: str | list[dict], system: str | None = None,
    ) -> list[dict]:
        """Normalize messages to OpenAI-style list format."""
        if isinstance(messages, str):
            msgs: list[dict] = [{"role": "user", "content": messages}]
        else:
            msgs = list(messages)

        if system:
            msgs.insert(0, {"role": "system", "content": system})

        return msgs

    # ------------------------------------------------------------------
    # Model resolution via aistatus.cc API
    # ------------------------------------------------------------------

    def _resolve_model(
        self, model: str, prefer: list[str] | None = None,
    ) -> list[_ResolvedCandidate]:
        """Resolve model name → ordered list of resolved candidates."""
        try:
            check = self.api.check_model(model)
            primary_provider = check.provider or (self._guess_provider(model) or [{}])[0].get("slug", "")
            primary = {"slug": primary_provider, "model": check.model or model}

            alternatives = [
                {"slug": alt.slug, "model": alt.suggested_model or model}
                for alt in check.alternatives
                if alt.status == Status.OPERATIONAL
            ]

            if check.is_available:
                raw = [primary] + alternatives
            else:
                raw = alternatives + [primary]
        except Exception:
            log.debug("aistatus.cc API unreachable, guessing provider for '%s'", model)
            raw = self._guess_provider(model)

        candidates = [{"slug": c["slug"], "model": c.get("model", model)} for c in raw]
        return self._sort_and_bind_candidates(candidates, prefer)

    async def _aresolve_model(
        self, model: str, prefer: list[str] | None = None,
    ) -> list[_ResolvedCandidate]:
        """Async version of _resolve_model."""
        try:
            check = await self.api.acheck_model(model)
            primary_provider = check.provider or (self._guess_provider(model) or [{}])[0].get("slug", "")
            primary = {"slug": primary_provider, "model": check.model or model}

            alternatives = [
                {"slug": alt.slug, "model": alt.suggested_model or model}
                for alt in check.alternatives
                if alt.status == Status.OPERATIONAL
            ]

            if check.is_available:
                raw = [primary] + alternatives
            else:
                raw = alternatives + [primary]
        except Exception:
            log.debug("aistatus.cc API unreachable, guessing provider for '%s'", model)
            raw = self._guess_provider(model)

        candidates = [{"slug": c["slug"], "model": c.get("model", model)} for c in raw]
        return self._sort_and_bind_candidates(candidates, prefer)

    def _guess_provider(self, model: str) -> list[dict[str, str]]:
        """Guess provider from model name prefix when API is unreachable."""
        model_lower = model.lower()

        # Check if model has provider/ prefix
        if "/" in model:
            direct = normalize_provider_slug(model.split("/", 1)[0])
            if direct in self._adapter_index:
                return [{"slug": direct, "model": model}]

        for prefix, slug in MODEL_PREFIX_MAP.items():
            if model_lower.startswith(prefix):
                return [{"slug": slug, "model": model}]

        # Unknown model — try all registered adapters
        return [{"slug": slug, "model": model} for slug in self.adapters]

    def _sort_and_bind_candidates(
        self,
        candidates: list[dict[str, str]],
        prefer: list[str] | None = None,
    ) -> list[_ResolvedCandidate]:
        """Normalize slugs, bind to adapters, sort by preference, and deduplicate."""
        prefer_order = [normalize_provider_slug(p) for p in (prefer or [])]

        resolved: list[_ResolvedCandidate] = []
        for c in candidates:
            slug = normalize_provider_slug(c["slug"])
            adapter_key = self._adapter_index.get(slug)
            if not adapter_key:
                continue
            resolved.append(_ResolvedCandidate(
                provider_slug=slug,
                adapter_key=adapter_key,
                model_id=c["model"],
            ))

        # Sort by preference
        if prefer_order:
            def _score(rc: _ResolvedCandidate) -> int:
                if rc.provider_slug in prefer_order:
                    return prefer_order.index(rc.provider_slug)
                if rc.adapter_key in prefer_order:
                    return prefer_order.index(rc.adapter_key)
                return len(prefer_order)
            resolved.sort(key=_score)

        return _dedupe_candidates(resolved)

    # ------------------------------------------------------------------
    # Core routing (sync)
    # ------------------------------------------------------------------

    def route(
        self,
        messages: str | list[dict],
        *,
        model: str | None = None,
        tier: str | None = None,
        system: str | None = None,
        allow_fallback: bool = True,
        timeout: float = 30.0,
        prefer: list[str] | None = None,
        model_fallbacks: dict[str, list[str]] | None = None,
        retry_on_rate_limit: bool = True,
        retry_delay: float = 1.0,
        **kwargs: Any,
    ) -> RouteResponse:
        """Route an LLM request with automatic provider resolution and fallback."""
        if not model and not tier:
            raise ValueError("Either 'model' or 'tier' must be specified")

        msgs = self._normalize_messages(messages, system)

        if tier:
            return self._route_tier(
                msgs, tier, allow_fallback, timeout, prefer,
                model_fallbacks=model_fallbacks,
                retry_on_rate_limit=retry_on_rate_limit,
                retry_delay=retry_delay,
                **kwargs,
            )
        return self._route_model(
            msgs, model, allow_fallback, timeout, prefer,
            model_fallbacks=model_fallbacks,
            retry_on_rate_limit=retry_on_rate_limit,
            retry_delay=retry_delay,
            **kwargs,
        )

    async def aroute(
        self,
        messages: str | list[dict],
        *,
        model: str | None = None,
        tier: str | None = None,
        system: str | None = None,
        allow_fallback: bool = True,
        timeout: float = 30.0,
        prefer: list[str] | None = None,
        model_fallbacks: dict[str, list[str]] | None = None,
        retry_on_rate_limit: bool = True,
        retry_delay: float = 1.0,
        **kwargs: Any,
    ) -> RouteResponse:
        """Async version of route()."""
        if not model and not tier:
            raise ValueError("Either 'model' or 'tier' must be specified")

        msgs = self._normalize_messages(messages, system)

        if tier:
            return await self._aroute_tier(
                msgs, tier, allow_fallback, timeout, prefer,
                model_fallbacks=model_fallbacks,
                retry_on_rate_limit=retry_on_rate_limit,
                retry_delay=retry_delay,
                **kwargs,
            )
        return await self._aroute_model(
            msgs, model, allow_fallback, timeout, prefer,
            model_fallbacks=model_fallbacks,
            retry_on_rate_limit=retry_on_rate_limit,
            retry_delay=retry_delay,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def route_stream(
        self,
        messages: str | list[dict],
        *,
        model: str | None = None,
        system: str | None = None,
        allow_fallback: bool = True,
        timeout: float = 30.0,
        prefer: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream LLM responses as async generator of StreamChunk."""
        if not model:
            raise ValueError("'model' must be specified for streaming")

        msgs = self._normalize_messages(messages, system)
        candidates = await self._aresolve_model(model, prefer)

        if not candidates:
            raise AllProvidersDown([f"no adapter for model '{model}'"])

        for candidate in candidates:
            adapter = self.adapters.get(candidate.adapter_key)
            if not adapter:
                continue

            # Skip unhealthy providers
            if self.health and not self.health.is_healthy(candidate.provider_slug):
                continue

            try:
                if hasattr(adapter, 'call_stream') and adapter.call_stream is not None:
                    async for chunk in adapter.call_stream(candidate.model_id, msgs, timeout, **kwargs):
                        yield chunk
                    if self.health:
                        self.health.record_success(candidate.provider_slug)
                    return
                else:
                    # Fallback: call() then emit as chunks
                    resp = await adapter.acall(candidate.model_id, msgs, timeout, **kwargs)
                    if self.health:
                        self.health.record_success(candidate.provider_slug)

                    yield {"type": "text", "text": resp.content}
                    yield {
                        "type": "usage",
                        "input_tokens": resp.input_tokens,
                        "output_tokens": resp.output_tokens,
                        "cache_creation_input_tokens": resp.cache_creation_input_tokens,
                        "cache_read_input_tokens": resp.cache_read_input_tokens,
                    }
                    yield {"type": "done"}
                    return
            except Exception as e:
                status = getattr(e, "status", None) or getattr(e, "status_code", None)
                if self.health and status:
                    self.health.record_error(candidate.provider_slug, status)
                if allow_fallback is False:
                    yield {"type": "error", "error": e}
                    return

        raise AllProvidersDown([f"no streaming adapter for model '{model}'"])

    async def route_stream_callbacks(
        self,
        messages: str | list[dict],
        *,
        callbacks: StreamCallbacks,
        model: str | None = None,
        system: str | None = None,
        allow_fallback: bool = True,
        timeout: float = 30.0,
        prefer: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Stream with callback-based interface."""
        try:
            async for chunk in self.route_stream(
                messages, model=model, system=system,
                allow_fallback=allow_fallback, timeout=timeout,
                prefer=prefer, **kwargs,
            ):
                chunk_type = chunk.get("type")
                if chunk_type == "text" and callbacks.on_token:
                    callbacks.on_token(chunk.get("text", ""))  # type: ignore[typeddict-item]
                elif chunk_type == "error" and callbacks.on_error:
                    callbacks.on_error(chunk.get("error", Exception("Unknown streaming error")))  # type: ignore[typeddict-item]
                elif chunk_type == "usage" and callbacks.on_usage:
                    callbacks.on_usage({
                        "input_tokens": chunk.get("input_tokens", 0),  # type: ignore[typeddict-item]
                        "output_tokens": chunk.get("output_tokens", 0),  # type: ignore[typeddict-item]
                        "cache_creation_input_tokens": chunk.get("cache_creation_input_tokens", 0),  # type: ignore[typeddict-item]
                        "cache_read_input_tokens": chunk.get("cache_read_input_tokens", 0),  # type: ignore[typeddict-item]
                    })
                elif chunk_type == "done" and callbacks.on_complete:
                    callbacks.on_complete()
        except Exception as e:
            if callbacks.on_error:
                callbacks.on_error(e)
            else:
                raise

    # ------------------------------------------------------------------
    # Model routing (sync + async) — with middleware, health, retry, fallback
    # ------------------------------------------------------------------

    def _route_model(
        self, messages: list[dict], model: str,
        allow_fallback: bool, timeout: float,
        prefer: list[str] | None,
        model_fallbacks: dict[str, list[str]] | None = None,
        retry_on_rate_limit: bool = True,
        retry_delay: float = 1.0,
        **kwargs: Any,
    ) -> RouteResponse:
        candidates = self._resolve_model(model, prefer)
        if not candidates:
            raise AllProvidersDown([f"no adapter for model '{model}'"])

        tried: list[str] = []
        first = candidates[0]
        should_retry = retry_on_rate_limit

        for candidate in candidates:
            adapter = self.adapters.get(candidate.adapter_key)
            if not adapter:
                continue

            # Skip providers in health cooldown
            if self.health and not self.health.is_healthy(candidate.provider_slug):
                tried.append(f"{candidate.provider_slug}[cooldown]")
                continue

            try:
                started = time.monotonic()

                # Execute beforeRequest middleware
                self._run_before_request(messages, candidate, kwargs)

                resp = adapter.call(candidate.model_id, messages, timeout, **kwargs)

                if self.health:
                    self.health.record_success(candidate.provider_slug)

                routed = self._build_response(resp, candidate, first)
                latency_ms = round((time.monotonic() - started) * 1000)

                # Execute afterResponse middleware
                self._run_after_response(routed, candidate, latency_ms, candidate != first)

                if self.usage is not None:
                    self.usage.record(routed, latency_ms)
                return routed
            except Exception as e:
                status = getattr(e, "status", None) or getattr(e, "status_code", None)

                if self.health and status:
                    self.health.record_error(candidate.provider_slug, status)

                # Execute onError middleware
                self._run_on_error(e, candidate)

                # Retry on 429
                if should_retry and status == 429:
                    try:
                        time.sleep(retry_delay)
                        started_retry = time.monotonic()
                        self._run_before_request(messages, candidate, kwargs)
                        resp = adapter.call(candidate.model_id, messages, timeout, **kwargs)
                        if self.health:
                            self.health.record_success(candidate.provider_slug)
                        routed = self._build_response(resp, candidate, first)
                        latency_ms = round((time.monotonic() - started_retry) * 1000)
                        self._run_after_response(routed, candidate, latency_ms, candidate != first)
                        if self.usage is not None:
                            self.usage.record(routed, latency_ms)
                        return routed
                    except Exception as retry_e:
                        retry_status = getattr(retry_e, "status", None) or getattr(retry_e, "status_code", None)
                        if self.health and retry_status:
                            self.health.record_error(candidate.provider_slug, retry_status)
                        self._run_on_error(retry_e, candidate)
                        tried.append(f"{candidate.provider_slug}[retry-failed]")
                else:
                    log.warning("Call to %s/%s failed: %s", candidate.provider_slug, candidate.model_id, e)
                    tried.append(f"{candidate.provider_slug}[error]")

                if not allow_fallback:
                    raise ProviderCallFailed(candidate.provider_slug, candidate.model_id, e)
                continue

        # Model fallback: try alternative models
        if model_fallbacks and model in model_fallbacks:
            for fallback_model in model_fallbacks[model]:
                try:
                    return self._route_model(
                        messages, fallback_model, allow_fallback, timeout, prefer,
                        model_fallbacks=None,  # prevent recursion
                        retry_on_rate_limit=retry_on_rate_limit,
                        retry_delay=retry_delay,
                        **kwargs,
                    )
                except AllProvidersDown as e:
                    tried.extend(e.tried)
                    continue
                except ProviderCallFailed:
                    if not allow_fallback:
                        raise
                    continue

        raise AllProvidersDown(tried)

    async def _aroute_model(
        self, messages: list[dict], model: str,
        allow_fallback: bool, timeout: float,
        prefer: list[str] | None,
        model_fallbacks: dict[str, list[str]] | None = None,
        retry_on_rate_limit: bool = True,
        retry_delay: float = 1.0,
        **kwargs: Any,
    ) -> RouteResponse:
        candidates = await self._aresolve_model(model, prefer)
        if not candidates:
            raise AllProvidersDown([f"no adapter for model '{model}'"])

        tried: list[str] = []
        first = candidates[0]
        should_retry = retry_on_rate_limit

        for candidate in candidates:
            adapter = self.adapters.get(candidate.adapter_key)
            if not adapter:
                continue

            if self.health and not self.health.is_healthy(candidate.provider_slug):
                tried.append(f"{candidate.provider_slug}[cooldown]")
                continue

            try:
                started = time.monotonic()
                await self._arun_before_request(messages, candidate, kwargs)
                resp = await adapter.acall(candidate.model_id, messages, timeout, **kwargs)

                if self.health:
                    self.health.record_success(candidate.provider_slug)

                routed = self._build_response(resp, candidate, first)
                latency_ms = round((time.monotonic() - started) * 1000)

                await self._arun_after_response(routed, candidate, latency_ms, candidate != first)

                if self.usage is not None:
                    self.usage.record(routed, latency_ms)
                return routed
            except Exception as e:
                status = getattr(e, "status", None) or getattr(e, "status_code", None)

                if self.health and status:
                    self.health.record_error(candidate.provider_slug, status)

                await self._arun_on_error(e, candidate)

                if should_retry and status == 429:
                    try:
                        await asyncio.sleep(retry_delay)
                        started_retry = time.monotonic()
                        await self._arun_before_request(messages, candidate, kwargs)
                        resp = await adapter.acall(candidate.model_id, messages, timeout, **kwargs)
                        if self.health:
                            self.health.record_success(candidate.provider_slug)
                        routed = self._build_response(resp, candidate, first)
                        latency_ms = round((time.monotonic() - started_retry) * 1000)
                        await self._arun_after_response(routed, candidate, latency_ms, candidate != first)
                        if self.usage is not None:
                            self.usage.record(routed, latency_ms)
                        return routed
                    except Exception as retry_e:
                        retry_status = getattr(retry_e, "status", None) or getattr(retry_e, "status_code", None)
                        if self.health and retry_status:
                            self.health.record_error(candidate.provider_slug, retry_status)
                        await self._arun_on_error(retry_e, candidate)
                        tried.append(f"{candidate.provider_slug}[retry-failed]")
                else:
                    log.warning("Call to %s/%s failed: %s", candidate.provider_slug, candidate.model_id, e)
                    tried.append(f"{candidate.provider_slug}[error]")

                if not allow_fallback:
                    raise ProviderCallFailed(candidate.provider_slug, candidate.model_id, e)
                continue

        # Model fallback chains
        if model_fallbacks and model in model_fallbacks:
            for fallback_model in model_fallbacks[model]:
                try:
                    return await self._aroute_model(
                        messages, fallback_model, allow_fallback, timeout, prefer,
                        model_fallbacks=None,
                        retry_on_rate_limit=retry_on_rate_limit,
                        retry_delay=retry_delay,
                        **kwargs,
                    )
                except AllProvidersDown as e:
                    tried.extend(e.tried)
                    continue
                except ProviderCallFailed:
                    if not allow_fallback:
                        raise
                    continue

        raise AllProvidersDown(tried)

    # ------------------------------------------------------------------
    # Tier routing (sync + async)
    # ------------------------------------------------------------------

    def _route_tier(
        self, messages: list[dict], tier: str,
        allow_fallback: bool, timeout: float,
        prefer: list[str] | None, **kwargs: Any,
    ) -> RouteResponse:
        models = self._tiers.get(tier)
        if not models:
            raise ValueError(
                f"Tier '{tier}' not configured. "
                f"Use router.add_tier('{tier}', ['model-a', 'model-b']) first."
            )

        tried_all: list[str] = []
        for m in models:
            try:
                return self._route_model(messages, m, allow_fallback, timeout, prefer, **kwargs)
            except AllProvidersDown as e:
                tried_all.extend(e.tried)
                continue
            except ProviderCallFailed:
                if not allow_fallback:
                    raise
                continue

        raise AllProvidersDown(tried_all)

    async def _aroute_tier(
        self, messages: list[dict], tier: str,
        allow_fallback: bool, timeout: float,
        prefer: list[str] | None, **kwargs: Any,
    ) -> RouteResponse:
        models = self._tiers.get(tier)
        if not models:
            raise ValueError(
                f"Tier '{tier}' not configured. "
                f"Use router.add_tier('{tier}', ['model-a', 'model-b']) first."
            )

        tried_all: list[str] = []
        for m in models:
            try:
                return await self._aroute_model(
                    messages, m, allow_fallback, timeout, prefer, **kwargs,
                )
            except AllProvidersDown as e:
                tried_all.extend(e.tried)
                continue
            except ProviderCallFailed:
                if not allow_fallback:
                    raise
                continue

        raise AllProvidersDown(tried_all)

    # ------------------------------------------------------------------
    # Response builder
    # ------------------------------------------------------------------

    def _build_response(
        self,
        resp: RouteResponse,
        candidate: _ResolvedCandidate,
        first: _ResolvedCandidate,
    ) -> RouteResponse:
        """Build the final RouteResponse with proper model naming and fallback info."""
        used = resp.model_used
        if "/" not in used:
            used = f"{candidate.provider_slug}/{used}"
        is_fallback = (
            candidate.provider_slug != first.provider_slug
            or candidate.model_id != first.model_id
        )
        cost_usd = self.usage.cost_calculator.calculate_cost(
            provider=candidate.provider_slug,
            model=used,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
        ) if self.usage is not None else resp.cost_usd
        return RouteResponse(
            content=resp.content,
            model_used=used,
            provider_used=candidate.provider_slug,
            was_fallback=is_fallback,
            fallback_reason=f"{first.provider_slug} unavailable" if is_fallback else None,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cache_creation_input_tokens=resp.cache_creation_input_tokens,
            cache_read_input_tokens=resp.cache_read_input_tokens,
            cost_usd=cost_usd,
            raw=resp.raw,
        )

    # ------------------------------------------------------------------
    # Middleware execution helpers (sync)
    # ------------------------------------------------------------------

    def _run_before_request(self, messages: list[dict], candidate: _ResolvedCandidate, kwargs: dict):
        for mw in self._middleware:
            fn = getattr(mw, "before_request", None)
            if fn:
                ctx = BeforeRequestContext(
                    messages=messages,
                    options=RouteOptions(),
                    call_options=ProviderCallOptions(),
                    provider=candidate.provider_slug,
                    model=candidate.model_id,
                )
                fn(ctx)

    def _run_after_response(self, response: RouteResponse, candidate: _ResolvedCandidate, latency_ms: float, was_fallback: bool):
        for mw in self._middleware:
            fn = getattr(mw, "after_response", None)
            if fn:
                ctx = AfterResponseContext(
                    response=response,
                    provider=candidate.provider_slug,
                    model=candidate.model_id,
                    latency_ms=latency_ms,
                    was_fallback=was_fallback,
                )
                fn(ctx)

    def _run_on_error(self, error: Exception, candidate: _ResolvedCandidate):
        for mw in self._middleware:
            fn = getattr(mw, "on_error", None)
            if fn:
                fn(error, {"provider": candidate.provider_slug, "model": candidate.model_id})

    # ------------------------------------------------------------------
    # Middleware execution helpers (async)
    # ------------------------------------------------------------------

    async def _arun_before_request(self, messages: list[dict], candidate: _ResolvedCandidate, kwargs: dict):
        for mw in self._middleware:
            fn = getattr(mw, "before_request", None)
            if fn:
                ctx = BeforeRequestContext(
                    messages=messages,
                    options=RouteOptions(),
                    call_options=ProviderCallOptions(),
                    provider=candidate.provider_slug,
                    model=candidate.model_id,
                )
                result = fn(ctx)
                if asyncio.iscoroutine(result):
                    await result

    async def _arun_after_response(self, response: RouteResponse, candidate: _ResolvedCandidate, latency_ms: float, was_fallback: bool):
        for mw in self._middleware:
            fn = getattr(mw, "after_response", None)
            if fn:
                ctx = AfterResponseContext(
                    response=response,
                    provider=candidate.provider_slug,
                    model=candidate.model_id,
                    latency_ms=latency_ms,
                    was_fallback=was_fallback,
                )
                result = fn(ctx)
                if asyncio.iscoroutine(result):
                    await result

    async def _arun_on_error(self, error: Exception, candidate: _ResolvedCandidate):
        for mw in self._middleware:
            fn = getattr(mw, "on_error", None)
            if fn:
                result = fn(error, {"provider": candidate.provider_slug, "model": candidate.model_id})
                if asyncio.iscoroutine(result):
                    await result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _dedupe_candidates(candidates: list[_ResolvedCandidate]) -> list[_ResolvedCandidate]:
    """Remove duplicate (adapter_key, model_id) pairs, keeping first occurrence."""
    seen: set[str] = set()
    result: list[_ResolvedCandidate] = []
    for c in candidates:
        key = f"{c.adapter_key}:{c.model_id}"
        if key in seen:
            continue
        seen.add(key)
        result.append(c)
    return result


# Need to import here to avoid circular import at module level
from .models import RouteOptions  # noqa: E402
