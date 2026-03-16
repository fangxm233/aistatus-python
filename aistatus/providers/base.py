"""Base provider adapter and registry."""

from __future__ import annotations

import abc
from typing import Any, Type

from ..models import ProviderConfig, RouteResponse


class ProviderAdapter(abc.ABC):
    """Abstract base class for model provider adapters."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def slug(self) -> str:
        return self.config.slug

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


# ---------------------------------------------------------------------------
# Global Registry for Adapter Types
# ---------------------------------------------------------------------------

_ADAPTER_TYPES: dict[str, Type[ProviderAdapter]] = {}


def register(cls: Type[ProviderAdapter]):
    """Decorator to register a concrete provider adapter class."""
    # We use lowercase class names minus 'Adapter' or a manual identifier
    type_name = cls.__name__.lower().replace("adapter", "")
    _ADAPTER_TYPES[type_name] = cls
    return cls


def create_adapter(config: ProviderConfig) -> ProviderAdapter:
    """Instantiate the correct adapter based on its type name."""
    cls = _ADAPTER_TYPES.get(config.adapter_type.lower())
    if not cls:
        raise ValueError(f"Unknown adapter type: {config.adapter_type}")
    return cls(config)
