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


@register
class DeepSeekAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.deepseek.com", "DEEPSEEK_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.deepseek.com", "DEEPSEEK_API_KEY", True)


@register
class MistralAdapter(OpenAIAdapter):
    """Mistral adapter. Registered as 'mistral' (canonical name)."""
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.mistral.ai/v1", "MISTRAL_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.mistral.ai/v1", "MISTRAL_API_KEY", True)


# Backward compat: keep old name as alias
MistralAIAdapter = MistralAdapter
# Also register under "mistralai" for backward compatibility
register_adapter_type("mistralai", MistralAdapter)


@register
class XAIAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.x.ai/v1", "XAI_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.x.ai/v1", "XAI_API_KEY", True)


@register
class GroqAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.groq.com/openai/v1", "GROQ_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.groq.com/openai/v1", "GROQ_API_KEY", True)


@register
class TogetherAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.together.xyz/v1", "TOGETHER_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.together.xyz/v1", "TOGETHER_API_KEY", True)


@register
class MoonshotAIAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", True)

# Also register "moonshot" as alias for MoonshotAI
register_adapter_type("moonshot", MoonshotAIAdapter)


@register
class QwenAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", True)
