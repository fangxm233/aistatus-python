"""Anthropic provider adapter."""

from __future__ import annotations

import os
from typing import Any

from ..exceptions import ProviderNotInstalled
from ..models import RouteResponse
from .base import ProviderAdapter, register


@register
class AnthropicAdapter(ProviderAdapter):
    def _get_api_key(self):
        if self.config.api_key:
            return self.config.api_key
        if self.config.env:
            return os.environ.get(self.config.env)
        return os.environ.get("ANTHROPIC_API_KEY")

    def _get_client(self):
        try:
            import anthropic
        except ImportError:
            raise ProviderNotInstalled("anthropic", "anthropic")
        
        return anthropic.Anthropic(
            api_key=self._get_api_key(),
            base_url=self.config.base_url
        )

    def _get_async_client(self):
        try:
            import anthropic
        except ImportError:
            raise ProviderNotInstalled("anthropic", "anthropic")
        
        return anthropic.AsyncAnthropic(
            api_key=self._get_api_key(),
            base_url=self.config.base_url
        )

    def _to_response(self, r, model_id: str) -> RouteResponse:
        text_parts = [b.text for b in r.content if hasattr(b, "text")]
        return RouteResponse(
            content="\n".join(text_parts),
            model_used=model_id,
            provider_used=self.config.slug,
            was_fallback=False,
            input_tokens=getattr(r.usage, "input_tokens", 0),
            output_tokens=getattr(r.usage, "output_tokens", 0),
            raw=r,
        )

    def call(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_client()
        model_name = self.strip_provider(model_id)

        system = None
        user_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user_msgs.append(m)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": user_msgs,
            "max_tokens": kw.pop("max_tokens", 4096),
            "timeout": timeout,
            **kw,
        }
        if system:
            params["system"] = system

        r = client.messages.create(**params)
        return self._to_response(r, model_id)

    async def acall(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_async_client()
        model_name = self.strip_provider(model_id)

        system = None
        user_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user_msgs.append(m)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": user_msgs,
            "max_tokens": kw.pop("max_tokens", 4096),
            "timeout": timeout,
            **kw,
        }
        if system:
            params["system"] = system

        r = await client.messages.create(**params)
        return self._to_response(r, model_id)
