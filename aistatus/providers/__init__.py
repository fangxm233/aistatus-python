"""Provider adapters — import to trigger registration."""

from .anthropic_ import AnthropicAdapter  # noqa: F401
from .openai_ import OpenAIAdapter  # noqa: F401
from .google_ import GoogleAdapter  # noqa: F401
from .openrouter_ import OpenRouterAdapter  # noqa: F401
from .compatible_ import (
    DeepSeekAdapter,
    MistralAdapter,
    MistralAIAdapter,  # backward compat alias
    XAIAdapter,
    GroqAdapter,
    TogetherAdapter,
    MoonshotAIAdapter,
    QwenAdapter,
)  # noqa: F401
from .base import create_adapter, register_adapter_type  # noqa: F401
