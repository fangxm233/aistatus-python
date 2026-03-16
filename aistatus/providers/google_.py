"""Google Gemini provider adapter."""

from __future__ import annotations

from typing import Any

from ..exceptions import ProviderNotInstalled
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
            return os.environ.get(self.config.env)
        return os.environ.get("GEMINI_API_KEY")

    def _get_client(self):
        try:
            from google import genai
        except ImportError:
            raise ProviderNotInstalled("google", "google-genai")
        return genai.Client(api_key=self._get_api_key())

    def _messages_to_contents(self, messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Convert OpenAI-style messages to Gemini contents format."""
        system = None
        contents = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                role = "model" if m["role"] == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": m["content"]}]})
        return system, contents

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

        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system

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

        config: dict[str, Any] = {}
        if system:
            config["system_instruction"] = system

        r = await client.aio.models.generate_content(
            model=model_name,
            contents=contents,
            config=config if config else None,
        )
        return self._to_response(r, model_id)
