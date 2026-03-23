"""Default configuration constants for auto-discovery."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AutoProviderSpec:
    env_var: str
    adapter_type: str
    aliases: list[str] = field(default_factory=list)


# Provider slug → spec for auto-discovery from environment variables.
AUTO_PROVIDERS: dict[str, AutoProviderSpec] = {
    "anthropic":  AutoProviderSpec("ANTHROPIC_API_KEY",  "anthropic"),
    "openai":     AutoProviderSpec("OPENAI_API_KEY",     "openai"),
    "google":     AutoProviderSpec("GEMINI_API_KEY",     "google"),
    "openrouter": AutoProviderSpec("OPENROUTER_API_KEY", "openrouter"),
    "deepseek":   AutoProviderSpec("DEEPSEEK_API_KEY",   "deepseek"),
    "mistral":    AutoProviderSpec("MISTRAL_API_KEY",    "mistral", aliases=["mistralai"]),
    "xai":        AutoProviderSpec("XAI_API_KEY",        "xai",     aliases=["x-ai"]),
    "groq":       AutoProviderSpec("GROQ_API_KEY",       "groq"),
    "together":   AutoProviderSpec("TOGETHER_API_KEY",   "together"),
    "moonshot":   AutoProviderSpec("MOONSHOT_API_KEY",   "moonshot", aliases=["moonshotai"]),
    "qwen":       AutoProviderSpec("DASHSCOPE_API_KEY",  "qwen"),
}

# Canonical alias mapping: variant slug → canonical slug.
PROVIDER_ALIASES: dict[str, str] = {
    "anthropic":  "anthropic",
    "openai":     "openai",
    "google":     "google",
    "openrouter": "openrouter",
    "deepseek":   "deepseek",
    "mistral":    "mistral",
    "mistralai":  "mistral",
    "xai":        "xai",
    "x-ai":       "xai",
    "groq":       "groq",
    "together":   "together",
    "moonshot":   "moonshot",
    "moonshotai": "moonshot",
    "qwen":       "qwen",
}

# Model name prefix → provider slug.
# Used as fallback when aistatus.cc API is unreachable.
MODEL_PREFIX_MAP: dict[str, str] = {
    "claude":     "anthropic",
    "gpt":        "openai",
    "o1":         "openai",
    "o3":         "openai",
    "o4":         "openai",
    "chatgpt":    "openai",
    "gemini":     "google",
    "deepseek":   "deepseek",
    "mistral":    "mistral",
    "codestral":  "mistral",
    "pixtral":    "mistral",
    "grok":       "xai",
    "llama":      "groq",
    "qwen":       "qwen",
    "moonshot":   "moonshot",
}


def normalize_provider_slug(slug: str | None) -> str:
    """Normalize a provider slug to its canonical form."""
    value = (slug or "").strip().lower()
    return PROVIDER_ALIASES.get(value, value)


def extract_provider_slug(model_id: str | None) -> str | None:
    """Extract and normalize the provider slug from a 'provider/model' string."""
    value = (model_id or "").strip()
    if "/" not in value:
        return None
    return normalize_provider_slug(value.split("/", 1)[0])
