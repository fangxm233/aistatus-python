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

__version__ = "0.0.2"

# Core routing
from .router import Router  # noqa: F401

# Data models (for type hints and advanced usage)
from .models import (  # noqa: F401
    RouteResponse,
    RouteConfig,
    CheckResult,
    Status,
    ProviderStatus,
    ProviderConfig,
    ModelInfo,
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
    CheckAPIUnreachable,
)

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
    "CheckResult",
    "Status",
    "ProviderStatus",
    "ProviderConfig",
    "ModelInfo",
    # Exceptions
    "AIStatusError",
    "AllProvidersDown",
    "ProviderCallFailed",
    "NoBudgetMatch",
    "ProviderNotInstalled",
    "CheckAPIUnreachable",
]
