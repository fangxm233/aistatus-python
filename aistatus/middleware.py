"""Middleware hook definitions for request/response interception in the Router."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import ChatMessage, ProviderCallOptions, RouteOptions, RouteResponse


@dataclass
class BeforeRequestContext:
    """Context passed to beforeRequest hooks."""
    messages: list[ChatMessage]
    options: RouteOptions
    call_options: ProviderCallOptions
    provider: str | None = None  # set during candidate iteration
    model: str | None = None


@dataclass
class AfterResponseContext:
    """Context passed to afterResponse hooks."""
    response: RouteResponse
    provider: str
    model: str
    latency_ms: float  # wall-clock time in ms
    was_fallback: bool


@runtime_checkable
class Middleware(Protocol):
    """A middleware that can intercept requests/responses."""

    def before_request(self, ctx: BeforeRequestContext) -> None:
        """Called before each provider call. Can modify context or raise to abort."""
        ...

    def after_response(self, ctx: AfterResponseContext) -> None:
        """Called after a successful response."""
        ...

    def on_error(self, error: Exception, ctx: dict[str, str]) -> None:
        """Called when a provider call fails (before fallback)."""
        ...
