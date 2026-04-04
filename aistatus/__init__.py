# input: package imports plus optional provider SDKs discovered by router/provider modules
# output: public aistatus SDK API exports including routing, pricing, usage, and upload config helpers
# pos: package root that defines the stable Python SDK import surface
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

"""aistatus — Smart AI model routing with real-time status awareness.

Quickstart::

    from aistatus import route

    resp = route("Hello!", model="claude-sonnet-4-6")
    print(resp.content)          # "Hello! How can I help?"
    print(resp.model_used)       # "anthropic/claude-sonnet-4-6"
    print(resp.was_fallback)     # False

Tier-based routing (requires configuration)::

    from aistatus import Router

    router = Router()
    router.add_tier("fast", ["claude-haiku-4-5", "gpt-4o-mini"])
    resp = router.route("Hello!", tier="fast")
"""

__version__ = "0.0.3"

# Core routing
from .router import Router  # noqa: F401

# Data models (for type hints and advanced usage)
from .models import (  # noqa: F401
    RouteResponse,
    RouteConfig,
    RouteOptions,
    CheckResult,
    Status,
    ProviderStatus,
    ProviderConfig,
    ProviderCallOptions,
    ModelInfo,
    # Content block types
    TextBlock,
    ImageUrlBlock,
    ImageBase64Block,
    # Stream chunk types
    StreamChunk,
)

# Status API (for direct queries without routing)
from .api import StatusAPI  # noqa: F401

# Exceptions
from .exceptions import (  # noqa: F401
    AIStatusError,
    AllProvidersDown,
    ProviderCallFailed,
    NoBudgetMatch,
    ProviderNotInstalled,
    ProviderNotConfigured,
    CheckAPIUnreachable,
)

# Content helpers
from .content import extract_text_from_content, normalize_content  # noqa: F401

# Middleware types
from .middleware import Middleware, BeforeRequestContext, AfterResponseContext  # noqa: F401

# Stream helpers
from .stream import collect_stream_text, stream_to_text_chunks  # noqa: F401

# Provider adapter base + registration
from .providers.base import ProviderAdapter, register_adapter_type  # noqa: F401

# Usage & pricing
from .usage import UsageTracker  # noqa: F401
from .usage_storage import UsageStorage  # noqa: F401
from .pricing import CostCalculator  # noqa: F401
from .config import AIStatusConfig, configure, get_config  # noqa: F401

# Trigger adapter registration on import
from . import providers as _providers  # noqa: F401

# ---------------------------------------------------------------------------
# Module-level convenience functions (lazy-initialized default Router)
# ---------------------------------------------------------------------------

_default_router: Router | None = None


def _get_default_router() -> Router:
    global _default_router
    if _default_router is None:
        _default_router = Router()
    return _default_router


def route(
    messages: str | list[dict],
    *,
    model: str | None = None,
    tier: str | None = None,
    system: str | None = None,
    allow_fallback: bool = True,
    timeout: float = 30.0,
    prefer: list[str] | None = None,
    **kwargs,
) -> RouteResponse:
    """One-liner routing with auto-discovered providers.

    Args:
        messages: User message string or OpenAI-style message list.
        model: Model name (e.g. "claude-sonnet-4-6"). Provider resolved via API.
        tier: Tier name. Requires prior configuration on the default router.
        system: Optional system prompt (convenience for string messages).
        allow_fallback: Try alternatives if primary provider is down.
        timeout: Provider call timeout in seconds.
        prefer: Provider preference order for fallback.
        **kwargs: Passed to provider SDK (max_tokens, temperature, etc.).
    """
    return _get_default_router().route(
        messages, model=model, tier=tier, system=system,
        allow_fallback=allow_fallback, timeout=timeout, prefer=prefer,
        **kwargs,
    )


async def aroute(
    messages: str | list[dict],
    *,
    model: str | None = None,
    tier: str | None = None,
    system: str | None = None,
    allow_fallback: bool = True,
    timeout: float = 30.0,
    prefer: list[str] | None = None,
    **kwargs,
) -> RouteResponse:
    """Async version of route()."""
    return await _get_default_router().aroute(
        messages, model=model, tier=tier, system=system,
        allow_fallback=allow_fallback, timeout=timeout, prefer=prefer,
        **kwargs,
    )


__all__ = [
    # One-liners
    "route",
    "aroute",
    # Router class
    "Router",
    # API client
    "StatusAPI",
    # Models
    "RouteResponse",
    "RouteConfig",
    "RouteOptions",
    "CheckResult",
    "Status",
    "ProviderStatus",
    "ProviderConfig",
    "ProviderCallOptions",
    "ModelInfo",
    # Content block types
    "TextBlock",
    "ImageUrlBlock",
    "ImageBase64Block",
    # Stream chunks
    "StreamChunk",
    # Exceptions
    "AIStatusError",
    "AllProvidersDown",
    "ProviderCallFailed",
    "NoBudgetMatch",
    "ProviderNotInstalled",
    "ProviderNotConfigured",
    "CheckAPIUnreachable",
    # Content helpers
    "extract_text_from_content",
    "normalize_content",
    # Middleware
    "Middleware",
    "BeforeRequestContext",
    "AfterResponseContext",
    # Stream helpers
    "collect_stream_text",
    "stream_to_text_chunks",
    # Provider base
    "ProviderAdapter",
    "register_adapter_type",
    # Usage & pricing
    "UsageTracker",
    "UsageStorage",
    "CostCalculator",
    "AIStatusConfig",
    "configure",
    "get_config",
]
