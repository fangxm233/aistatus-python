"""Gateway configuration: loading, validation, auto-discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".aistatus"
CONFIG_FILE = CONFIG_DIR / "gateway.yaml"

# Default base URLs (without trailing /v1 — the SDK path includes it)
DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "google": "https://generativelanguage.googleapis.com",
}

# How each endpoint type sends authentication
AUTH_STYLES: dict[str, tuple[str, str]] = {
    # (header_name, prefix)
    "anthropic": ("x-api-key", ""),
    "openai": ("authorization", "Bearer "),
    "bearer": ("authorization", "Bearer "),
    "google": ("x-goog-api-key", ""),
}

# Env var → endpoint mapping for auto-discovery
AUTO_DISCOVER_MAP = {
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "GEMINI_API_KEY": "google",
}

# Known OpenAI-compatible fallback providers
FALLBACK_PRESETS: dict[str, dict[str, str]] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env": "TOGETHER_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY",
    },
}


@dataclass
class FallbackConfig:
    """A fallback backend."""

    name: str
    base_url: str
    api_key: str
    auth_style: str = "bearer"
    model_prefix: str = ""  # prepended to model name, e.g. "anthropic/"
    model_map: dict[str, str] = field(default_factory=dict)
    translate: str | None = None  # "anthropic-to-openai" | None


@dataclass
class EndpointConfig:
    """One endpoint group (anthropic / openai / google)."""

    name: str
    base_url: str
    auth_style: str  # "anthropic" | "bearer" | "google"
    keys: list[str] = field(default_factory=list)
    fallbacks: list[FallbackConfig] = field(default_factory=list)


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""

    host: str = "127.0.0.1"
    port: int = 9880
    status_check: bool = True
    endpoints: dict[str, EndpointConfig] = field(default_factory=dict)

    # --- loaders ---

    @classmethod
    def load(cls, path: Path | None = None) -> GatewayConfig:
        path = path or CONFIG_FILE
        if not path.exists():
            # No config file → try auto-discovery
            return cls.auto_discover()

        import yaml  # type: ignore[import-untyped]

        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls._from_dict(raw)

    @classmethod
    def auto_discover(cls, host: str = "127.0.0.1", port: int = 9880) -> GatewayConfig:
        """Build config automatically from environment variables."""
        endpoints: dict[str, EndpointConfig] = {}

        for env_var, ep_name in AUTO_DISCOVER_MAP.items():
            key = os.environ.get(env_var)
            if not key:
                continue
            auth = ep_name if ep_name in AUTH_STYLES else "bearer"
            base = DEFAULT_BASE_URLS.get(ep_name, "")
            endpoints[ep_name] = EndpointConfig(
                name=ep_name, base_url=base, auth_style=auth, keys=[key],
            )

        # Auto-add OpenRouter as fallback if its key exists
        or_key = os.environ.get("OPENROUTER_API_KEY")
        if or_key:
            or_base = FALLBACK_PRESETS["openrouter"]["base_url"]
            for ep_name, ep in endpoints.items():
                prefix = f"{ep_name}/" if ep_name != "openai" else "openai/"
                translate = "anthropic-to-openai" if ep_name == "anthropic" else None
                ep.fallbacks.append(
                    FallbackConfig(
                        name="openrouter",
                        base_url=or_base,
                        api_key=or_key,
                        model_prefix=prefix,
                        translate=translate,
                    )
                )

        return cls(host=host, port=port, endpoints=endpoints)

    # --- private ---

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> GatewayConfig:
        host = raw.get("host", "127.0.0.1")
        port = raw.get("port", 9880)
        status_check = raw.get("status_check", True)

        endpoints: dict[str, EndpointConfig] = {}
        for ep_name in ("anthropic", "openai", "google"):
            ep_raw = raw.get(ep_name)
            if not ep_raw:
                continue

            keys = _resolve_keys(ep_raw.get("keys", []))
            auth_style = ep_raw.get("auth_style", ep_name if ep_name in AUTH_STYLES else "bearer")
            base_url = ep_raw.get("base_url", DEFAULT_BASE_URLS.get(ep_name, ""))

            fallbacks: list[FallbackConfig] = []
            for fb in ep_raw.get("fallbacks", []):
                fb_key = _resolve_single(fb.get("key", fb.get("api_key", "")))
                fallbacks.append(
                    FallbackConfig(
                        name=fb.get("name", "fallback"),
                        base_url=fb["base_url"],
                        api_key=fb_key,
                        auth_style=fb.get("auth_style", "bearer"),
                        model_prefix=fb.get("model_prefix", ""),
                        model_map=fb.get("model_map", {}),
                        translate=fb.get("translate"),
                    )
                )

            endpoints[ep_name] = EndpointConfig(
                name=ep_name, base_url=base_url, auth_style=auth_style,
                keys=keys, fallbacks=fallbacks,
            )

        return cls(host=host, port=port, status_check=status_check, endpoints=endpoints)


# --- helpers ---

def _resolve_single(val: str) -> str:
    """Resolve $ENV_VAR references."""
    if isinstance(val, str) and val.startswith("$"):
        return os.environ.get(val[1:], "")
    return val


def _resolve_keys(raw_keys: list) -> list[str]:
    out: list[str] = []
    for k in raw_keys:
        v = _resolve_single(str(k))
        if v:
            out.append(v)
    return out


def generate_config() -> str:
    """Generate an example gateway.yaml with comments."""
    return """\
# aistatus gateway configuration
# Docs: https://aistatus.cc/docs
#
# After editing, start with:
#   python -m aistatus.gateway start
#
# Or skip this file entirely with auto-discovery:
#   python -m aistatus.gateway start --auto

port: 9880

# ── Anthropic (for Claude Code) ─────────────────────────────────
anthropic:
  # Multiple keys → automatic rotation on rate-limit / 5xx
  keys:
    - $ANTHROPIC_API_KEY
    # - sk-ant-your-second-key

  fallbacks:
    # OpenRouter serves Claude models via OpenAI-compatible API
    - name: openrouter
      base_url: https://openrouter.ai/api/v1
      key: $OPENROUTER_API_KEY
      model_prefix: "anthropic/"
      translate: anthropic-to-openai

# ── OpenAI (for Codex) ──────────────────────────────────────────
openai:
  keys:
    - $OPENAI_API_KEY

  fallbacks:
    - name: openrouter
      base_url: https://openrouter.ai/api/v1
      key: $OPENROUTER_API_KEY
      model_prefix: "openai/"

    # DeepSeek as budget fallback (different models)
    # - name: deepseek
    #   base_url: https://api.deepseek.com
    #   key: $DEEPSEEK_API_KEY
    #   model_map:
    #     gpt-4o: deepseek-chat
    #     gpt-4o-mini: deepseek-chat

# ── Google (for Gemini CLI) ─────────────────────────────────────
# google:
#   keys:
#     - $GEMINI_API_KEY
"""
