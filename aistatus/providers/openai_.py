"""OpenAI provider adapter."""

from __future__ import annotations

import os
from typing import Any

from ..exceptions import ProviderNotConfigured, ProviderNotInstalled
from ..models import RouteResponse
from .base import ProviderAdapter, register


@register
class OpenAIAdapter(ProviderAdapter):
    def _get_api_key(self):
        if self.config.api_key:
            return self.config.api_key
        if self.config.env:
            key = os.environ.get(self.config.env)
            if key:
                return key
            raise ProviderNotConfigured("openai", self.config.env)
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            return key
        raise ProviderNotConfigured("openai", "OPENAI_API_KEY")

    def _get_base_url(self) -> str | None:
        return self.config.base_url

    def _get_default_headers(self) -> dict[str, str] | None:
        return self.config.headers

    def _get_client(self):
        try:
            import openai
        except ImportError:
            raise ProviderNotInstalled("openai", "openai")

        return openai.OpenAI(
            api_key=self._get_api_key(),
            base_url=self._get_base_url(),
            default_headers=self._get_default_headers(),
        )

    def _get_async_client(self):
        try:
            import openai
        except ImportError:
            raise ProviderNotInstalled("openai", "openai")

        return openai.AsyncOpenAI(
            api_key=self._get_api_key(),
            base_url=self._get_base_url(),
            default_headers=self._get_default_headers(),
        )

    @staticmethod
    def _content_blocks_to_openai(content: str | list) -> str | list:
        """Convert ContentBlock list to OpenAI message format."""
        if isinstance(content, str):
            return content
        result = []
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text":
                result.append({"type": "text", "text": block["text"]})
            elif block_type == "image_url":
                result.append({
                    "type": "image_url",
                    "image_url": block.get("image_url", {}),
                })
            elif block_type == "image":
                # Convert Anthropic base64 format to OpenAI image_url
                source = block.get("source", {})
                media_type = source.get("media_type", "image/png")
                data = source.get("data", "")
                result.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                })
            else:
                result.append({"type": "text", "text": str(block)})
        return result

    def _build_payload(
        self, model_name: str, messages: list[dict], timeout: float, **kw: Any
    ) -> dict[str, Any]:
        """Build the chat completions payload. Subclasses can override."""
        # Convert content blocks in messages
        processed_msgs = []
        for m in messages:
            msg = dict(m)
            if not isinstance(msg.get("content"), str) and msg.get("content") is not None:
                msg["content"] = self._content_blocks_to_openai(msg["content"])
            processed_msgs.append(msg)

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": processed_msgs,
            "timeout": timeout,
        }

        # Pass response_format natively (OpenAI supports it)
        response_format = kw.pop("response_format", None)
        if response_format:
            payload["response_format"] = response_format

        payload.update(kw)
        return payload

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
        payload = self._build_payload(model_name, messages, timeout, **kw)
        r = client.chat.completions.create(**payload)
        return self._to_response(r, model_id)

    async def acall(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_async_client()
        model_name = self.strip_provider(model_id)
        payload = self._build_payload(model_name, messages, timeout, **kw)
        r = await client.chat.completions.create(**payload)
        return self._to_response(r, model_id)
