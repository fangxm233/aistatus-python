"""Core routing engine with auto-discovery and model-based resolution."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .api import StatusAPI
from ._defaults import AUTO_PROVIDERS, MODEL_PREFIX_MAP
from .exceptions import (
    AllProvidersDown,
    ProviderCallFailed,
)
from .models import (
    ProviderConfig,
    RouteResponse,
    Status,
)
from .providers.base import ProviderAdapter, create_adapter
from .usage import UsageTracker

log = logging.getLogger("aistatus")


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
    ):
        """
        Args:
            base_url: aistatus.cc API base URL.
            check_timeout: Timeout for status check API calls.
            providers: If set, only auto-discover these provider slugs.
            auto_discover: Scan env vars to register providers automatically.
        """
        self.api = StatusAPI(base_url=base_url, timeout=check_timeout)
        self.adapters: dict[str, ProviderAdapter] = {}
        self._tiers: dict[str, list[str]] = {}
        self.usage = UsageTracker() if track_usage else None

        if auto_discover:
            self._auto_discover(only=providers)

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def _auto_discover(self, only: list[str] | None = None):
        """Scan environment variables and register adapters for providers that have API keys."""
        for slug, (env_var, adapter_type) in AUTO_PROVIDERS.items():
            if only and slug not in only:
                continue
            if os.environ.get(env_var):
                try:
                    config = ProviderConfig(
                        slug=slug,
                        adapter_type=adapter_type,
                        env=env_var,
                    )
                    self.adapters[slug] = create_adapter(config)
                    log.debug("Auto-discovered provider: %s", slug)
                except Exception as e:
                    log.debug("Skipping provider %s: %s", slug, e)

    def register_provider(self, config: ProviderConfig):
        """Manually register a provider with custom configuration."""
        adapter = create_adapter(config)
        self.adapters[config.slug] = adapter

    def add_tier(self, name: str, models: list[str]):
        """Configure a tier as an ordered list of model names to try.

        Example::

            router.add_tier("fast", ["claude-haiku-4-5", "gpt-4o-mini", "gemini-2.0-flash"])
            router.add_tier("standard", ["claude-sonnet-4-6", "gpt-4o"])
        """
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
    ) -> list[tuple[str, str]]:
        """Resolve model name → ordered list of (provider_slug, model_id) candidates."""
        candidates: list[tuple[str, str]] = []

        try:
            check = self.api.check_model(model)
            primary = (check.provider, model)

            if check.is_available:
                candidates.append(primary)
                for alt in check.alternatives:
                    if alt.status == Status.OPERATIONAL:
                        candidates.append((alt.slug, alt.suggested_model))
            else:
                # Primary is down — try alternatives first, then primary as last resort
                for alt in check.alternatives:
                    if alt.status == Status.OPERATIONAL:
                        candidates.append((alt.slug, alt.suggested_model))
                candidates.append(primary)
        except Exception:
            # API unreachable — fall back to prefix guessing
            log.debug("aistatus.cc API unreachable, guessing provider for '%s'", model)
            candidates = self._guess_provider(model)

        # Filter to providers we have adapters for
        candidates = [(s, m) for s, m in candidates if s in self.adapters]

        # Apply prefer ordering
        if prefer and len(candidates) > 1:
            def sort_key(c: tuple[str, str]) -> int:
                try:
                    return prefer.index(c[0])
                except ValueError:
                    return len(prefer)
            candidates.sort(key=sort_key)

        return candidates

    async def _aresolve_model(
        self, model: str, prefer: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Async version of _resolve_model."""
        candidates: list[tuple[str, str]] = []

        try:
            check = await self.api.acheck_model(model)
            primary = (check.provider, model)

            if check.is_available:
                candidates.append(primary)
                for alt in check.alternatives:
                    if alt.status == Status.OPERATIONAL:
                        candidates.append((alt.slug, alt.suggested_model))
            else:
                for alt in check.alternatives:
                    if alt.status == Status.OPERATIONAL:
                        candidates.append((alt.slug, alt.suggested_model))
                candidates.append(primary)
        except Exception:
            log.debug("aistatus.cc API unreachable, guessing provider for '%s'", model)
            candidates = self._guess_provider(model)

        candidates = [(s, m) for s, m in candidates if s in self.adapters]

        if prefer and len(candidates) > 1:
            def sort_key(c: tuple[str, str]) -> int:
                try:
                    return prefer.index(c[0])
                except ValueError:
                    return len(prefer)
            candidates.sort(key=sort_key)

        return candidates

    def _guess_provider(self, model: str) -> list[tuple[str, str]]:
        """Guess provider from model name prefix when API is unreachable."""
        model_lower = model.lower()
        for prefix, slug in MODEL_PREFIX_MAP.items():
            if model_lower.startswith(prefix):
                return [(slug, model)]
        # Unknown model — try all registered adapters
        return [(slug, model) for slug in self.adapters]

    # ------------------------------------------------------------------
    # Core routing
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
        **kwargs: Any,
    ) -> RouteResponse:
        """Route an LLM request with automatic provider resolution and fallback.

        Args:
            messages: User message string or OpenAI-style message list.
            model: Model name (e.g. "claude-sonnet-4-6"). Provider auto-resolved.
            tier: Tier name (requires prior add_tier() configuration).
            system: System prompt (convenience, only when messages is a string).
            allow_fallback: Try alternative providers if primary is unavailable.
            timeout: Provider call timeout in seconds.
            prefer: Preferred provider order for fallback selection.
            **kwargs: Passed to provider SDK (max_tokens, temperature, etc.).
        """
        if not model and not tier:
            raise ValueError("Either 'model' or 'tier' must be specified")

        msgs = self._normalize_messages(messages, system)

        if tier:
            return self._route_tier(msgs, tier, allow_fallback, timeout, prefer, **kwargs)
        return self._route_model(msgs, model, allow_fallback, timeout, prefer, **kwargs)

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
        **kwargs: Any,
    ) -> RouteResponse:
        """Async version of route()."""
        if not model and not tier:
            raise ValueError("Either 'model' or 'tier' must be specified")

        msgs = self._normalize_messages(messages, system)

        if tier:
            return await self._aroute_tier(msgs, tier, allow_fallback, timeout, prefer, **kwargs)
        return await self._aroute_model(msgs, model, allow_fallback, timeout, prefer, **kwargs)

    # ------------------------------------------------------------------
    # Model routing (sync + async)
    # ------------------------------------------------------------------

    def _route_model(
        self, messages: list[dict], model: str,
        allow_fallback: bool, timeout: float,
        prefer: list[str] | None, **kwargs: Any,
    ) -> RouteResponse:
        candidates = self._resolve_model(model, prefer)
        if not candidates:
            raise AllProvidersDown([f"no adapter for model '{model}'"])

        tried: list[str] = []
        first = candidates[0]

        for slug, model_id in candidates:
            adapter = self.adapters[slug]
            try:
                started = time.monotonic()
                resp = adapter.call(model_id, messages, timeout, **kwargs)
                latency_ms = round((time.monotonic() - started) * 1000)
                is_fallback = (slug, model_id) != first
                used = resp.model_used
                if "/" not in used:
                    used = f"{slug}/{used}"
                cost_usd = self.usage.cost_calculator.calculate_cost(
                    provider=slug,
                    model=used,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                ) if self.usage is not None else resp.cost_usd
                routed = RouteResponse(
                    content=resp.content,
                    model_used=used,
                    provider_used=slug,
                    was_fallback=is_fallback,
                    fallback_reason=f"{first[0]} unavailable" if is_fallback else None,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    cost_usd=cost_usd,
                    raw=resp.raw,
                )
                if self.usage is not None:
                    self.usage.record(routed, latency_ms)
                return routed
            except Exception as e:
                log.warning("Call to %s/%s failed: %s", slug, model_id, e)
                tried.append(f"{slug}[error]")
                if not allow_fallback:
                    raise ProviderCallFailed(slug, model_id, e)
                continue

        raise AllProvidersDown(tried)

    async def _aroute_model(
        self, messages: list[dict], model: str,
        allow_fallback: bool, timeout: float,
        prefer: list[str] | None, **kwargs: Any,
    ) -> RouteResponse:
        candidates = await self._aresolve_model(model, prefer)
        if not candidates:
            raise AllProvidersDown([f"no adapter for model '{model}'"])

        tried: list[str] = []
        first = candidates[0]

        for slug, model_id in candidates:
            adapter = self.adapters[slug]
            try:
                started = time.monotonic()
                resp = await adapter.acall(model_id, messages, timeout, **kwargs)
                latency_ms = round((time.monotonic() - started) * 1000)
                is_fallback = (slug, model_id) != first
                used = resp.model_used
                if "/" not in used:
                    used = f"{slug}/{used}"
                cost_usd = self.usage.cost_calculator.calculate_cost(
                    provider=slug,
                    model=used,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                ) if self.usage is not None else resp.cost_usd
                routed = RouteResponse(
                    content=resp.content,
                    model_used=used,
                    provider_used=slug,
                    was_fallback=is_fallback,
                    fallback_reason=f"{first[0]} unavailable" if is_fallback else None,
                    input_tokens=resp.input_tokens,
                    output_tokens=resp.output_tokens,
                    cost_usd=cost_usd,
                    raw=resp.raw,
                )
                if self.usage is not None:
                    self.usage.record(routed, latency_ms)
                return routed
            except Exception as e:
                log.warning("Call to %s/%s failed: %s", slug, model_id, e)
                tried.append(f"{slug}[error]")
                if not allow_fallback:
                    raise ProviderCallFailed(slug, model_id, e)
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
