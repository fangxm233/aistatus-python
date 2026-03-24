# Changelog

## 0.0.3 — 2026-03-23

### Gateway

A complete local HTTP proxy for AI API failover, running on `localhost:9880`.

- **Multi-key rotation** — configure multiple API keys per endpoint, rotated
  round-robin with automatic advance on error
- **Hybrid backend selection** — managed keys tried first, then the caller's
  own API key (passthrough), then fallback providers
- **Fallback chains** — route to secondary providers (e.g. OpenRouter) when
  the primary is down
- **Model-level fallback** — configure degradation chains per model
  (e.g. opus → sonnet → haiku); response includes
  `x-gateway-model-fallback` header
- **Protocol translation** — automatic Anthropic ↔ OpenAI format conversion
  for cross-provider fallback, including streaming SSE events
- **Health tracking** — per-backend and per-model health with sliding 60-second
  error window and status-code-specific cooldowns (429→30s, 500→15s, 502/503→10s)
- **Pre-flight status check** — queries `aistatus.cc` at startup to pre-mark
  globally degraded models
- **Configuration modes** — maintain multiple YAML configs (production/dev)
  and switch at runtime via `POST /mode` or per-request via `/m/{mode}/...`
- **Gateway authentication** — protect the proxy with separate API keys,
  constant-time comparison via `hmac.compare_digest`
- **Usage tracking** — per-provider/model cost breakdown via `/usage` endpoint
  with period (`today|week|month|all`) and `group_by` filters
- **Management endpoints** — `/health`, `/status`, `/usage`, `/mode`
- **CLI** — `python -m aistatus.gateway start [--auto|--config PATH]` and
  `python -m aistatus.gateway init` to generate example config
- **Graceful shutdown** on SIGTERM/SIGINT with PID file and log file support

### Router

Major feature sync bringing parity with the TypeScript SDK.

- **Slug alias system** — register multiple slugs for the same provider
  (e.g. `my-openai` aliased to `openai`)
- **`ProviderNotConfigured` exception** — raised when the required API key or
  explicit provider config is missing (separate from `ProviderNotInstalled`)
- **`prefer` parameter** — `route(prefer=["anthropic", "google"])` to bias
  fallback ordering toward preferred providers
- **`system` parameter** — `route("Hello", system="Be concise.")` for
  convenient system prompt without manual message wrapping
- **String message shortcut** — pass a plain string to `route()` instead of
  a full messages list
- **Enhanced cost calculation** — cache token tracking (creation + read) in
  `RouteResponse`, correct cost calc that accounts for cached tokens

### Provider Adapters

- **All adapters** (Anthropic, OpenAI, Google, OpenRouter, compatible) —
  expanded to support streaming, structured output, multimodal content,
  system prompts, and tool use
- **OpenRouter adapter** — rewritten with proper model prefix handling and
  fallback model mapping

### New Modules

- `aistatus.content` — content block utilities
- `aistatus.middleware` — hook definitions for request/response interception
- `aistatus.stream` — streaming response utilities

### API Client

- `StatusAPI` — expanded with model search, trending, benchmarks, market
  pricing, and recommendation endpoints
- Pricing lookup — handles versioned Claude model IDs correctly

### Fixes

- `health.py` — replace unbounded `defaultdict` with bounded dict + `setdefault`
- `translate.py` — emit terminal SSE events on stream truncation
- `auth.py` — use `hmac.compare_digest` for constant-time key comparison
- `router.py` — `_build_response` no longer ignores cache tokens in cost calc
- `server.py` — fix `set.update` string splitting bug + streaming release leak
- `server.py` — fix gateway response charset handling

## 0.0.2 — 2026-03-16

- Usage tracking layer with CLI output formats
- Version bump and PyPI publishing workflow

## 0.0.1 — 2026-03-15

- Initial SDK release
- Router with auto-discovery, model routing, and tier-based fallback
- Provider adapters: Anthropic, OpenAI, Google Gemini, OpenRouter, and
  OpenAI-compatible (DeepSeek, Mistral, xAI, Groq, Together, Moonshot, Qwen)
- StatusAPI client for `aistatus.cc`
- Environment variable auto-discovery
