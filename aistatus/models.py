"""Data models for aistatus SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, TypedDict


class Status(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Message & Content Types
# ---------------------------------------------------------------------------

MessageRole = Literal["system", "user", "assistant", "tool"]


class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class ImageUrlDetail(TypedDict, total=False):
    url: str
    detail: Literal["auto", "low", "high"]


class ImageUrlBlock(TypedDict):
    type: Literal["image_url"]
    image_url: ImageUrlDetail


class ImageBase64Source(TypedDict):
    type: Literal["base64"]
    media_type: str
    data: str


class ImageBase64Block(TypedDict):
    type: Literal["image"]
    source: ImageBase64Source


ContentBlock = TextBlock | ImageUrlBlock | ImageBase64Block


class ChatMessage(TypedDict, total=False):
    role: str  # MessageRole or any string
    content: str | list[ContentBlock]
    name: str
    tool_call_id: str


# ---------------------------------------------------------------------------
# Response Format
# ---------------------------------------------------------------------------

class TextResponseFormat(TypedDict):
    type: Literal["text"]


class JsonObjectResponseFormat(TypedDict):
    type: Literal["json_object"]


class JsonSchemaSpec(TypedDict, total=False):
    name: str
    schema: dict[str, Any]
    strict: bool


class JsonSchemaResponseFormat(TypedDict):
    type: Literal["json_schema"]
    json_schema: JsonSchemaSpec


ResponseFormat = TextResponseFormat | JsonObjectResponseFormat | JsonSchemaResponseFormat


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
    aliases: list[str] | None = None
    headers: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Call Options
# ---------------------------------------------------------------------------

@dataclass
class ProviderCallOptions:
    """Options passed to provider adapters."""
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    response_format: ResponseFormat | None = None
    provider_options: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


@dataclass
class RouteOptions(ProviderCallOptions):
    """Options for a route() call, extending ProviderCallOptions."""
    model: str | None = None
    tier: str | None = None
    system: str | None = None
    allow_fallback: bool = True
    timeout: float = 30.0
    prefer: list[str] | None = None
    model_fallbacks: dict[str, list[str]] | None = None
    retry_on_rate_limit: bool = True
    retry_delay: float = 1.0


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
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
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
# Routing Configuration (legacy, kept for backward compat)
# ---------------------------------------------------------------------------

@dataclass
class RouteConfig:
    """Configuration for a single route() call."""
    tier: str | None = None     # User-defined tier name
    model: str | None = None    # User-defined model name
    prefer: list[str] | None = None
    allow_fallback: bool = True
    provider_timeout: float = 30.0


# ---------------------------------------------------------------------------
# Stream Chunk Types
# ---------------------------------------------------------------------------

class StreamTextChunk(TypedDict):
    type: Literal["text"]
    text: str


class StreamUsageChunk(TypedDict, total=False):
    type: Literal["usage"]
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class StreamDoneChunk(TypedDict):
    type: Literal["done"]


class StreamErrorChunk(TypedDict):
    type: Literal["error"]
    error: Exception


StreamChunk = StreamTextChunk | StreamUsageChunk | StreamDoneChunk | StreamErrorChunk
