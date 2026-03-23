"""Google Gemini provider adapter."""

from __future__ import annotations

from typing import Any

from ..exceptions import ProviderNotConfigured, ProviderNotInstalled
from ..models import RouteResponse
from .base import ProviderAdapter, register


@register
class GoogleAdapter(ProviderAdapter):
    @property
    def slug(self) -> str:
        return "google"

    def _get_api_key(self):
        import os
        if self.config.api_key:
            return self.config.api_key
        if self.config.env:
            key = os.environ.get(self.config.env)
            if key:
                return key
            raise ProviderNotConfigured("google", self.config.env)
        key = os.environ.get("GEMINI_API_KEY")
        if key:
            return key
        raise ProviderNotConfigured("google", "GEMINI_API_KEY")

    def _get_client(self):
        try:
            from google import genai
        except ImportError:
            raise ProviderNotInstalled("google", "google-genai")
        return genai.Client(api_key=self._get_api_key())

    @staticmethod
    def _content_blocks_to_google(content: str | list) -> list[dict]:
        """Convert ContentBlock list to Gemini parts format."""
        if isinstance(content, str):
            return [{"text": content}]
        result = []
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text":
                result.append({"text": block["text"]})
            elif block_type == "image_url":
                img = block.get("image_url", {})
                url = img.get("url", "")
                if url.startswith("data:"):
                    parts = url.split(",", 1)
                    if len(parts) == 2:
                        media_info = parts[0].replace("data:", "").replace(";base64", "")
                        result.append({
                            "inline_data": {
                                "mime_type": media_info,
                                "data": parts[1],
                            }
                        })
                    else:
                        result.append({"text": f"[Image: {url}]"})
                else:
                    result.append({"text": f"[Image: {url}]"})
            elif block_type == "image":
                source = block.get("source", {})
                result.append({
                    "inline_data": {
                        "mime_type": source.get("media_type", "image/png"),
                        "data": source.get("data", ""),
                    }
                })
            else:
                result.append({"text": str(block)})
        return result

    def _messages_to_contents(self, messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Convert OpenAI-style messages to Gemini contents format."""
        system = None
        contents = []
        for m in messages:
            if m["role"] == "system":
                content = m["content"]
                system = content if isinstance(content, str) else str(content)
            else:
                role = "model" if m["role"] == "assistant" else "user"
                parts = self._content_blocks_to_google(m["content"])
                contents.append({"role": role, "parts": parts})
        return system, contents

    @staticmethod
    def _apply_response_format(config: dict[str, Any], response_format: dict | None) -> dict[str, Any]:
        """Map response_format to generationConfig fields."""
        if not response_format:
            return config
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            config["response_mime_type"] = "application/json"
        elif fmt_type == "json_schema":
            schema = response_format.get("json_schema", {})
            config["response_mime_type"] = "application/json"
            if schema.get("schema"):
                config["response_schema"] = schema["schema"]
        return config

    def _to_response(self, r, model_id: str) -> RouteResponse:
        text = r.text or ""
        usage = getattr(r, "usage_metadata", None)
        return RouteResponse(
            content=text,
            model_used=model_id,
            provider_used="google",
            was_fallback=False,
            input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
            raw=r,
        )

    def call(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_client()
        model_name = self.strip_provider(model_id)
        system, contents = self._messages_to_contents(messages)

        response_format = kw.pop("response_format", None)

        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system

        # Map generation params
        if "temperature" in kw:
            config["temperature"] = kw.pop("temperature")
        if "top_p" in kw:
            config["top_p"] = kw.pop("top_p")
        if "max_tokens" in kw:
            config["max_output_tokens"] = kw.pop("max_tokens")

        config = self._apply_response_format(config, response_format)

        r = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config if config else None,
        )
        return self._to_response(r, model_id)

    async def acall(
        self, model_id: str, messages: list[dict], timeout: float, **kw: Any
    ) -> RouteResponse:
        client = self._get_client()
        model_name = self.strip_provider(model_id)
        system, contents = self._messages_to_contents(messages)

        response_format = kw.pop("response_format", None)

        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system

        if "temperature" in kw:
            config["temperature"] = kw.pop("temperature")
        if "top_p" in kw:
            config["top_p"] = kw.pop("top_p")
        if "max_tokens" in kw:
            config["max_output_tokens"] = kw.pop("max_tokens")

        config = self._apply_response_format(config, response_format)

        r = await client.aio.models.generate_content(
            model=model_name,
            contents=contents,
            config=config if config else None,
        )
        return self._to_response(r, model_id)
