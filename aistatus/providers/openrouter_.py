"""OpenRouter provider adapter."""

from __future__ import annotations

import os
from ..exceptions import ProviderNotInstalled
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
            return os.environ.get(self.config.env)
        return os.environ.get("OPENROUTER_API_KEY")

    def _get_client(self):
        try:
            import openai
        except ImportError:
            raise ProviderNotInstalled("openrouter", "openai")
        
        # OpenRouter specific initialization
        return openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._get_api_key(),
            default_headers={
                "HTTP-Referer": "https://aistatus.cc",
                "X-Title": "aistatus-sdk",
            }
        )

    def _get_async_client(self):
        try:
            import openai
        except ImportError:
            raise ProviderNotInstalled("openrouter", "openai")
        
        return openai.AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._get_api_key(),
            default_headers={
                "HTTP-Referer": "https://aistatus.cc",
                "X-Title": "aistatus-sdk",
            }
        )

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
