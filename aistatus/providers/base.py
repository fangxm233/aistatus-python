"""Base provider adapter and registry."""

from __future__ import annotations

import abc
from collections.abc import AsyncGenerator
from typing import Any, Type

from .._defaults import normalize_provider_slug
from ..models import ProviderConfig, RouteResponse, StreamChunk


class ProviderAdapter(abc.ABC):
    """Abstract base class for model provider adapters."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def slug(self) -> str:
        return self.config.slug

    @property
    def aliases(self) -> list[str]:
        return self.config.aliases or []

    def supports_provider(self, slug: str) -> bool:
        """Check if this adapter supports the given provider slug (name or alias)."""
        normalized = normalize_provider_slug(slug)
        return normalized == normalize_provider_slug(self.slug) or normalized in [
            normalize_provider_slug(a) for a in self.aliases
        ]

    def strip_provider(self, model_id: str) -> str:
        """Strip provider prefix (e.g. 'anthropic/claude-3' -> 'claude-3')."""
        if "/" in model_id:
            return model_id.split("/", 1)[1]
        return model_id

    @abc.abstractmethod
    def call(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        """Synchronous provider call."""
        pass

    @abc.abstractmethod
    async def acall(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        """Asynchronous provider call."""
        pass

    def call_stream(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> AsyncGenerator[StreamChunk, None] | None:
        """Optional streaming. Returns None if not supported."""
        return None

    async def acall_stream(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> AsyncGenerator[StreamChunk, None] | None:
        """Async optional streaming. Returns None if not supported."""
        return self.call_stream(model_id, messages, timeout, **kw)


# ---------------------------------------------------------------------------
# Global Registry for Adapter Types
# ---------------------------------------------------------------------------

_ADAPTER_TYPES: dict[str, Type[ProviderAdapter]] = {}


def register(cls: Type[ProviderAdapter]):
    """Decorator to register a concrete provider adapter class."""
    type_name = cls.__name__.lower().replace("adapter", "")
    _ADAPTER_TYPES[type_name] = cls
    return cls


def register_adapter_type(type_name: str, cls: Type[ProviderAdapter]):
    """Explicitly register an adapter type by name."""
    _ADAPTER_TYPES[type_name.lower()] = cls


def create_adapter(config: ProviderConfig) -> ProviderAdapter:
    """Instantiate the correct adapter based on its type name."""
    cls = _ADAPTER_TYPES.get(config.adapter_type.lower())
    if not cls:
        raise ValueError(f"Unknown adapter type: {config.adapter_type}")
    return cls(config)
