"""OpenAI-compatible provider adapters."""

from __future__ import annotations

import os

from ..exceptions import ProviderNotInstalled
from .base import register
from .openai_ import OpenAIAdapter

def _create_compatible_client(config, default_base_url: str, env_key_name: str, is_async: bool = False):
    try:
        import openai
    except ImportError:
        raise ProviderNotInstalled("openai", "openai")
    
    # 1. Direct API key passed in config
    # 2. Configured env var name
    # 3. Default fallback env var name
    api_key = config.api_key
    if not api_key:
        if config.env:
            api_key = os.environ.get(config.env)
        else:
            api_key = os.environ.get(env_key_name)
            
    base_url = config.base_url or default_base_url

    if is_async:
        return openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
    return openai.OpenAI(api_key=api_key, base_url=base_url)


@register
class DeepSeekAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.deepseek.com", "DEEPSEEK_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.deepseek.com", "DEEPSEEK_API_KEY", True)


@register
class MistralAIAdapter(OpenAIAdapter):
    def _get_client(self):
        return _create_compatible_client(self.config, "https://api.mistral.ai/v1", "MISTRAL_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://api.mistral.ai/v1", "MISTRAL_API_KEY", True)


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

@register
class QwenAdapter(OpenAIAdapter):
    def _get_client(self):
        # Aliyun DashScope OpenAI compatible endpoint
        return _create_compatible_client(self.config, "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", False)

    def _get_async_client(self):
        return _create_compatible_client(self.config, "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", True)
