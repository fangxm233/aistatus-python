"""Default configuration constants for auto-discovery."""

from __future__ import annotations

# Provider slug → (default_env_var, adapter_type_name)
# Used by Router to auto-discover available providers from environment variables.
AUTO_PROVIDERS: dict[str, tuple[str, str]] = {
    "anthropic":  ("ANTHROPIC_API_KEY",  "anthropic"),
    "openai":     ("OPENAI_API_KEY",     "openai"),
    "google":     ("GEMINI_API_KEY",     "google"),
    "openrouter": ("OPENROUTER_API_KEY", "openrouter"),
    "deepseek":   ("DEEPSEEK_API_KEY",   "deepseek"),
    "mistral":    ("MISTRAL_API_KEY",    "mistralai"),
    "xai":        ("XAI_API_KEY",        "xai"),
    "groq":       ("GROQ_API_KEY",       "groq"),
    "together":   ("TOGETHER_API_KEY",   "together"),
    "moonshot":   ("MOONSHOT_API_KEY",   "moonshotai"),
    "qwen":       ("DASHSCOPE_API_KEY",  "qwen"),
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
