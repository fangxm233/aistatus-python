"""OpenRouter provider adapter."""

from __future__ import annotations

import os
from ..exceptions import ProviderNotConfigured, ProviderNotInstalled
from ..models import RouteResponse
from .openai_ import OpenAIAdapter
from .base import register


@register
class OpenRouterAdapter(OpenAIAdapter):
    @property
    def slug(self) -> str:
        return "openrouter"

    def _get_api_key(self):
        if self.config.api_key:
            return self.config.api_key
        if self.config.env:
            key = os.environ.get(self.config.env)
            if key:
                return key
            raise ProviderNotConfigured("openrouter", self.config.env)
        key = os.environ.get("OPENROUTER_API_KEY")
        if key:
            return key
        raise ProviderNotConfigured("openrouter", "OPENROUTER_API_KEY")

    def _get_base_url(self) -> str | None:
        return self.config.base_url or "https://openrouter.ai/api/v1"

    def _get_default_headers(self) -> dict[str, str]:
        headers = {
            "HTTP-Referer": "https://aistatus.cc",
            "X-Title": "aistatus-sdk",
        }
        if self.config.headers:
            headers.update(self.config.headers)
        return headers

    def _to_response(self, r, model_id: str) -> RouteResponse:
        content = r.choices[0].message.content or ""
        usage = r.usage
        return RouteResponse(
            content=content,
            model_used=model_id,
            provider_used="openrouter",
            was_fallback=False,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            raw=r,
        )
