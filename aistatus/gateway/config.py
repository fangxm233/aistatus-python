"""Gateway configuration: loading, validation, auto-discovery."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth import GatewayAuthConfig

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
    passthrough: bool = True
    fallbacks: list[FallbackConfig] = field(default_factory=list)
    model_fallbacks: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""

    host: str = "127.0.0.1"
    port: int = 9880
    status_check: bool = True
    mode: str = "default"
    auth: GatewayAuthConfig | None = None
    endpoints: dict[str, EndpointConfig] = field(default_factory=dict)
    endpoint_modes: dict[str, dict[str, EndpointConfig]] = field(default_factory=dict)

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

        return cls(
            host=host,
            port=port,
            endpoints=endpoints,
            endpoint_modes={"default": endpoints},
        )

    # --- private ---

    @classmethod
    def _from_dict(cls, raw: dict[str, Any]) -> GatewayConfig:
        host = raw.get("host", "127.0.0.1")
        port = raw.get("port", 9880)
        status_check = raw.get("status_check", True)

        # Parse auth block
        auth: GatewayAuthConfig | None = None
        raw_auth = raw.get("auth")
        if isinstance(raw_auth, dict):
            auth_keys = _resolve_keys(raw_auth.get("keys", []))
            auth = GatewayAuthConfig(
                enabled=raw_auth.get("enabled", True) is not False and len(auth_keys) > 0,
                keys=auth_keys,
                header=raw_auth.get("header", "authorization"),
                public_paths=raw_auth.get("public_paths", ["/health"]),
            )

        # Parse endpoint configs — support both flat and mode-aware nested configs
        endpoint_modes: dict[str, dict[str, EndpointConfig]] = {}
        discovered_modes: list[str] = []

        for ep_name in ("anthropic", "openai", "google"):
            ep_raw = raw.get(ep_name)
            if not ep_raw or not isinstance(ep_raw, dict):
                continue

            if _is_flat_endpoint_config(ep_raw):
                # Flat config → assign to "default" mode
                endpoint_modes.setdefault("default", {})
                endpoint_modes["default"][ep_name] = _parse_endpoint_config(ep_name, ep_raw)
            else:
                # Mode-aware nested config
                for mode_name, mode_raw in ep_raw.items():
                    if not isinstance(mode_raw, dict):
                        continue
                    endpoint_modes.setdefault(mode_name, {})
                    endpoint_modes[mode_name][ep_name] = _parse_endpoint_config(ep_name, mode_raw)
                    if mode_name not in discovered_modes:
                        discovered_modes.append(mode_name)

        # Determine active mode
        available_modes = list(endpoint_modes.keys())
        mode = raw.get("mode") or (discovered_modes[0] if discovered_modes else (available_modes[0] if available_modes else "default"))
        active_mode = mode if mode in endpoint_modes else (available_modes[0] if available_modes else "default")
        endpoint_modes.setdefault(active_mode, {})

        return cls(
            host=host,
            port=port,
            status_check=status_check,
            mode=active_mode,
            auth=auth,
            endpoints=endpoint_modes[active_mode],
            endpoint_modes=endpoint_modes,
        )


# --- helpers ---

def _is_flat_endpoint_config(value: dict) -> bool:
    """Check if a dict looks like a flat endpoint config (has keys/base_url/etc)."""
    endpoint_keys = {"keys", "base_url", "auth_style", "passthrough", "fallbacks", "model_fallbacks"}
    return any(k in value for k in endpoint_keys)


def _parse_endpoint_config(ep_name: str, ep_raw: dict[str, Any]) -> EndpointConfig:
    """Parse a single endpoint config dict into EndpointConfig."""
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

    passthrough = ep_raw.get("passthrough", True)
    model_fallbacks = _parse_model_fallbacks(ep_raw.get("model_fallbacks", {}))

    return EndpointConfig(
        name=ep_name, base_url=base_url, auth_style=auth_style,
        keys=keys, passthrough=bool(passthrough), fallbacks=fallbacks,
        model_fallbacks=model_fallbacks,
    )


def _parse_model_fallbacks(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("model_fallbacks must be a mapping")

    parsed: dict[str, list[str]] = {}
    for model, candidates in raw.items():
        source = str(model).strip()
        if not source:
            raise ValueError("source model must be a non-empty string")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"model_fallbacks[{source!r}] fallback list must be a non-empty list")

        parsed_candidates: list[str] = []
        for candidate in candidates:
            target = str(candidate).strip()
            if not target:
                raise ValueError(f"model_fallbacks[{source!r}] fallback target must be a non-empty string")
            parsed_candidates.append(target)
        parsed[source] = parsed_candidates

    return parsed


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

# ── Authentication ─────────────────────────────────────────────
# auth:
#   keys:
#     - $GATEWAY_API_KEY
#   # header: authorization  # default: Bearer scheme
#   # public_paths: ["/health"]  # default

# ── Anthropic (for Claude Code) ─────────────────────────────────
anthropic:
  # Multiple keys → automatic rotation on rate-limit / 5xx
  keys:
    - $ANTHROPIC_API_KEY
    # - sk-ant-your-second-key

  # Hybrid mode: when true (default), the caller's own API key is
  # tried after managed keys, before fallbacks.
  # Set to false to use only managed keys.
  # Automatic model-level degradation order.
  # When a model is unhealthy, later tasks can switch to the first healthy fallback.
  # model_fallbacks:
  #   claude-opus-4-6:
  #     - claude-sonnet-4-6
  #     - claude-haiku-4-5

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
