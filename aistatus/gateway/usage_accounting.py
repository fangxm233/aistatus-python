# input:  parsed GatewayUsage, backend metadata, pricing and usage tracker
# output: one refresh-priced persisted usage record
# pos:    Shared JSON and SSE Gateway accounting boundary
# >>> 一旦我被更新，务必更新我的开头注释与所属文件夹 CLAUDE.md <<<

from __future__ import annotations

from typing import Any

from ..pricing import CostCalculator
from ..usage import UsageTracker
from .stream_usage import GatewayUsage


def infer_provider(backend: dict[str, Any], model: str) -> str:
    """Attribute a record to a vendor.

    An explicit ``vendor/model`` prefix wins; otherwise the backend id decides, folding variants
    like ``openai-codex`` down to ``openai`` because the pricing table is keyed by vendor.
    """
    if "/" in model:
        return model.split("/", 1)[0]
    backend_id = backend.get("id", "")
    for vendor in ("anthropic", "openai", "google", "openrouter"):
        if backend_id.startswith(vendor):
            return vendor
    return backend_id.split(":", 1)[0] or "unknown"


async def record_gateway_usage(
    *,
    backend: dict[str, Any],
    usage: GatewayUsage,
    elapsed_ms: int,
    pricing: CostCalculator,
    tracker: UsageTracker,
    billing_mode: str | None = None,
    default_billing_mode: str | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    """Price and persist one response's usage. The single accounting path for JSON and SSE alike."""
    provider = infer_provider(backend, usage.model)
    model = usage.model or f"{provider}/unknown"

    if usage.cache_creation_input_tokens > 0 or usage.cache_read_input_tokens > 0:
        cost = await pricing.calculate_cost_with_cache_async(
            provider,
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
        )
    else:
        cost = await pricing.calculate_cost_async(
            provider, model, usage.input_tokens, usage.output_tokens
        )

    tracker.record_usage(
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        latency_ms=elapsed_ms,
        fallback=":fb:" in backend.get("id", ""),
        cost=cost,
        billing_mode=billing_mode or default_billing_mode,
        metadata=metadata,
    )
