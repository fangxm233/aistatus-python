"""OpenAI-compatible provider adapters."""

from __future__ import annotations

import os

from ..exceptions import ProviderNotConfigured, ProviderNotInstalled
from .base import register, register_adapter_type
from .openai_ import OpenAIAdapter

def _create_compatible_client(config, default_base_url: str, env_key_name: str, is_async: bool = False):
    try:
        import openai
    except ImportError:
        raise ProviderNotInstalled("openai", "openai")

    api_key = config.api_key
    if not api_key:
        if config.env:
            api_key = os.environ.get(config.env)
        else:
            api_key = os.environ.get(env_key_name)

    if not api_key:
        raise ProviderNotConfigured(config.slug, config.env or env_key_name)

    base_url = config.base_url or default_base_url
    headers = config.headers

    if is_async:
        return openai.AsyncOpenAI(api_key=api_key, base_url=base_url, default_headers=headers)
    return openai.OpenAI(api_key=api_key, base_url=base_url, default_headers=headers)


def _resolve_api_key(config, env_key_name: str) -> str:
    """Resolve API key from config or environment."""
    if config.api_key:
        return config.api_key
    if config.env:
        key = os.environ.get(config.env)
        if key:
            return key
    key = os.environ.get(env_key_name)
    if key:
        return key
    return ""


class _CachedCompatibleMixin:
    """Mixin that caches sync/async clients, rebuilding on key change."""
    _compat_base_url: str = ""
    _compat_env_key: str = ""

    def _get_client(self):
        key = _resolve_api_key(self.config, self._compat_env_key)
        if self._client is not None and self._client_key == key:
            return self._client
        self._client = _create_compatible_client(self.config, self._compat_base_url, self._compat_env_key, False)
        self._client_key = key
        return self._client

    def _get_async_client(self):
        key = _resolve_api_key(self.config, self._compat_env_key)
        if self._async_client is not None and self._async_client_key == key:
            return self._async_client
        self._async_client = _create_compatible_client(self.config, self._compat_base_url, self._compat_env_key, True)
        self._async_client_key = key
        return self._async_client


@register
class DeepSeekAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    _compat_base_url = "https://api.deepseek.com"
    _compat_env_key = "DEEPSEEK_API_KEY"


@register
class MistralAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    """Mistral adapter. Registered as 'mistral' (canonical name)."""
    _compat_base_url = "https://api.mistral.ai/v1"
    _compat_env_key = "MISTRAL_API_KEY"


# Backward compat: keep old name as alias
MistralAIAdapter = MistralAdapter
# Also register under "mistralai" for backward compatibility
register_adapter_type("mistralai", MistralAdapter)


@register
class XAIAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    _compat_base_url = "https://api.x.ai/v1"
    _compat_env_key = "XAI_API_KEY"


@register
class GroqAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    _compat_base_url = "https://api.groq.com/openai/v1"
    _compat_env_key = "GROQ_API_KEY"


@register
class TogetherAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    _compat_base_url = "https://api.together.xyz/v1"
    _compat_env_key = "TOGETHER_API_KEY"


@register
class MoonshotAIAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    _compat_base_url = "https://api.moonshot.cn/v1"
    _compat_env_key = "MOONSHOT_API_KEY"

# Also register "moonshot" as alias for MoonshotAI
register_adapter_type("moonshot", MoonshotAIAdapter)


@register
class QwenAdapter(_CachedCompatibleMixin, OpenAIAdapter):
    _compat_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    _compat_env_key = "DASHSCOPE_API_KEY"
