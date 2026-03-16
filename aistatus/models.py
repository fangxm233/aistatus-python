"""Data models for aistatus SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Provider Configuration
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    """Configuration for a specific provider instance."""
    slug: str           # User-defined unique ID (e.g. "my-openai")
    adapter_type: str   # One of: "openai", "anthropic", "google", "openrouter"
    api_key: str | None = None
    env: str | None = None  # Environment variable name to fetch api_key from
    base_url: str | None = None


# ---------------------------------------------------------------------------
# Response and Status models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteResponse:
    """Unified response returned by route()."""
    content: str
    model_used: str
    provider_used: str
    was_fallback: bool
    fallback_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    raw: Any = None

    def __str__(self) -> str:
        return self.content


@dataclass
class CheckResult:
    """Result of a pre-flight availability check."""
    provider: str
    status: Status
    status_detail: str | None = None
    model: str | None = None
    alternatives: list[Alternative] = field(default_factory=list)

    @property
    def is_available(self) -> bool:
        return self.status == Status.OPERATIONAL

@dataclass
class Alternative:
    slug: str
    name: str
    status: Status
    suggested_model: str

@dataclass
class ModelInfo:
    id: str
    name: str
    provider_slug: str
    context_length: int
    modality: str
    prompt_price: float
    completion_price: float

@dataclass
class ProviderStatus:
    slug: str
    name: str
    status: Status
    status_detail: str | None
    model_count: int


# ---------------------------------------------------------------------------
# Routing Configuration
# ---------------------------------------------------------------------------

@dataclass
class RouteConfig:
    """Configuration for a single route() call."""
    tier: str | None = None     # User-defined tier name
    model: str | None = None    # User-defined model name
    prefer: list[str] | None = None
    allow_fallback: bool = True
    provider_timeout: float = 30.0
