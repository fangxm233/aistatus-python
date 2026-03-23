"""Anthropic provider adapter."""

from __future__ import annotations

import os
from typing import Any

from ..exceptions import ProviderNotConfigured, ProviderNotInstalled
from ..models import RouteResponse
from .base import ProviderAdapter, register


@register
class AnthropicAdapter(ProviderAdapter):
    def _get_api_key(self):
        if self.config.api_key:
            return self.config.api_key
        if self.config.env:
            key = os.environ.get(self.config.env)
            if key:
                return key
            raise ProviderNotConfigured("anthropic", self.config.env)
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key
        raise ProviderNotConfigured("anthropic", "ANTHROPIC_API_KEY")

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
        usage = getattr(r, "usage", None)
        return RouteResponse(
            content="\n".join(text_parts),
            model_used=model_id,
            provider_used=self.config.slug,
            was_fallback=False,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) if usage else 0,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
            raw=r,
        )

    @staticmethod
    def _content_blocks_to_anthropic(content: str | list) -> str | list:
        """Convert ContentBlock list to Anthropic message format."""
        if isinstance(content, str):
            return content
        result = []
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text":
                result.append({"type": "text", "text": block["text"]})
            elif block_type == "image_url":
                # Convert image_url to Anthropic base64 format if possible
                img = block.get("image_url", {})
                url = img.get("url", "")
                if url.startswith("data:"):
                    # Parse data URL: data:image/png;base64,...
                    parts = url.split(",", 1)
                    if len(parts) == 2:
                        media_info = parts[0].replace("data:", "").replace(";base64", "")
                        result.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_info,
                                "data": parts[1],
                            },
                        })
                    else:
                        result.append({"type": "text", "text": f"[Image: {url}]"})
                else:
                    result.append({"type": "text", "text": f"[Image: {url}]"})
            elif block_type == "image":
                result.append(block)
            else:
                result.append({"type": "text", "text": str(block)})
        return result

    @staticmethod
    def _apply_response_format(params: dict[str, Any], response_format: dict | None, system: str | None) -> str | None:
        """Translate response_format to system prompt instructions for Anthropic."""
        if not response_format:
            return system
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            json_instruction = "You must respond with valid JSON only. No other text."
            if system:
                return f"{system}\n\n{json_instruction}"
            return json_instruction
        elif fmt_type == "json_schema":
            schema = response_format.get("json_schema", {})
            schema_name = schema.get("name", "response")
            schema_def = schema.get("schema", {})
            json_instruction = (
                f"You must respond with valid JSON matching the schema '{schema_name}': "
                f"{schema_def}. No other text."
            )
            if system:
                return f"{system}\n\n{json_instruction}"
            return json_instruction
        return system

    def call(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_client()
        model_name = self.strip_provider(model_id)

        # Extract response_format from kwargs
        response_format = kw.pop("response_format", None)

        system = None
        user_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"] if isinstance(m["content"], str) else str(m["content"])
            else:
                msg = dict(m)
                if not isinstance(msg.get("content"), str):
                    msg["content"] = self._content_blocks_to_anthropic(msg["content"])
                user_msgs.append(msg)

        # Apply response format as system prompt instruction
        system = self._apply_response_format(kw, response_format, system)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": user_msgs,
            "max_tokens": kw.pop("max_tokens", 4096),
            "timeout": timeout,
            **kw,
        }
        if system:
            params["system"] = system

        # Apply custom headers from config
        if self.config.headers:
            params.setdefault("extra_headers", {}).update(self.config.headers)

        r = client.messages.create(**params)
        return self._to_response(r, model_id)

    async def acall(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_async_client()
        model_name = self.strip_provider(model_id)

        response_format = kw.pop("response_format", None)

        system = None
        user_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"] if isinstance(m["content"], str) else str(m["content"])
            else:
                msg = dict(m)
                if not isinstance(msg.get("content"), str):
                    msg["content"] = self._content_blocks_to_anthropic(msg["content"])
                user_msgs.append(msg)

        system = self._apply_response_format(kw, response_format, system)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": user_msgs,
            "max_tokens": kw.pop("max_tokens", 4096),
            "timeout": timeout,
            **kw,
        }
        if system:
            params["system"] = system

        if self.config.headers:
            params.setdefault("extra_headers", {}).update(self.config.headers)

        r = await client.messages.create(**params)
        return self._to_response(r, model_id)
