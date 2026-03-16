"""OpenAI provider adapter."""

from __future__ import annotations

import os
from typing import Any

from ..exceptions import ProviderNotInstalled
from ..models import RouteResponse
from .base import ProviderAdapter, register


@register
class OpenAIAdapter(ProviderAdapter):
    def _get_api_key(self):
        if self.config.api_key:
            return self.config.api_key
        if self.config.env:
            return os.environ.get(self.config.env)
        return os.environ.get("OPENAI_API_KEY")

    def _get_client(self):
        try:
            import openai
        except ImportError:
            raise ProviderNotInstalled("openai", "openai")
        
        return openai.OpenAI(
            api_key=self._get_api_key(),
            base_url=self.config.base_url
        )

    def _get_async_client(self):
        try:
            import openai
        except ImportError:
            raise ProviderNotInstalled("openai", "openai")
        
        return openai.AsyncOpenAI(
            api_key=self._get_api_key(),
            base_url=self.config.base_url
        )

    def _to_response(self, r, model_id: str) -> RouteResponse:
        content = r.choices[0].message.content or ""
        usage = r.usage
        return RouteResponse(
            content=content,
            model_used=model_id,
            provider_used=self.config.slug,
            was_fallback=False,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            raw=r,
        )

    def call(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_client()
        model_name = self.strip_provider(model_id)
        r = client.chat.completions.create(
            model=model_name,
            messages=messages,
            timeout=timeout,
            **kw,
        )
        return self._to_response(r, model_id)

    async def acall(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_async_client()
        model_name = self.strip_provider(model_id)
        r = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            timeout=timeout,
            **kw,
        )
        return self._to_response(r, model_id)
