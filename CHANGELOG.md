# Changelog

## 0.0.4 — 2026-04-04

Opt-in usage upload pipeline and cache-aware pricing for the leaderboard flow.

### New Modules

#### `aistatus.config` — persistent upload configuration

Manages SDK-wide upload identity, backed by `~/.aistatus/config.yaml`.

- **`AIStatusConfig` dataclass** — five fields:
  - `upload_enabled: bool = False` — master switch for usage uploads
  - `name: str = ""` — user or organization display name
  - `organization: str = ""` — organization identifier
  - `email: str = ""` — contact email for the upload identity
  - `base_url: str = "https://aistatus.cc"` — upload API endpoint
- **`get_config() → AIStatusConfig`** — returns a lazy-loaded singleton;
  thread-safe via double-checked locking (`threading.Lock`).
  On first call: loads `~/.aistatus/config.yaml` (YAML `upload:` section),
  then overlays environment variables. Subsequent calls return the cached
  instance.
- **`configure(*, upload, name, organization, email) → AIStatusConfig`** —
  mutates the singleton in-memory, then persists the entire config back to
  `~/.aistatus/config.yaml` via `yaml.safe_dump`. Returns the updated config.
- **Config precedence**: `configure()` overrides > env vars > YAML file > defaults
- **Environment variables**:
  | Variable | Maps to | Type |
  |---|---|---|
  | `AISTATUS_UPLOAD` | `upload_enabled` | bool (`1/true/yes/on`) |
  | `AISTATUS_NAME` | `name` | str |
  | `AISTATUS_ORG` | `organization` | str |
  | `AISTATUS_EMAIL` | `email` | str |

#### `aistatus.uploader` — fire-and-forget usage upload

Bridges local usage tracking to the remote leaderboard API.

- **`UsageUploader` class**:
  - Constructor takes `AIStatusConfig`; reads `config.base_url` for the
    endpoint.
  - **Shared thread pool**: class-level `ThreadPoolExecutor(max_workers=2,
    thread_name_prefix="aistatus-upload")`, initialized once via
    double-checked locking. Registered for `atexit` shutdown with
    `wait=False` so it never blocks interpreter exit.
  - **`upload(record: dict)`** — the only public method:
    1. Guards: skips if `upload_enabled` is `False` or if `name`/`email`
       are empty
    2. Builds a sanitized payload:
       - Maps short keys to full names: `in` → `input_tokens`,
         `out` → `output_tokens`, `cache_creation_in` →
         `cache_creation_input_tokens`, `cache_read_in` →
         `cache_read_input_tokens`
       - Truncates `name` (200 chars), `organization` (200), `email` (254)
       - Includes `sdk_version` (package `__version__`)
    3. Submits `_post(payload)` to the shared executor — fire-and-forget,
       never awaits
  - **`_post(payload)`** — `urllib.request.urlopen` POST to
    `{base_url}/api/usage/upload` with `Content-Type: application/json`,
    5-second timeout. Catches all exceptions silently.
- **`UsageUploadSink` Protocol** (in `usage.py`) — structural typing
  interface (`upload(record: dict) → None`) so `UsageTracker` doesn't
  depend directly on `UsageUploader`.

### Enhanced

#### Usage tracking pipeline

- **`UsageTracker.__init__`** — new optional `uploader: UsageUploadSink | None`
  parameter. When set, both `record()` and `record_usage()` forward every
  usage record to `uploader.upload()` after persisting to local storage.
- **`UsageTracker.record()`** — now includes conditional `cache_creation_in`
  and `cache_read_in` keys in the record dict when the response carries
  non-zero cache tokens.
- **`UsageTracker.record_usage()`** — new keyword args:
  `cache_creation_input_tokens: int = 0`,
  `cache_read_input_tokens: int = 0`, `billing_mode: str | None`. Same
  conditional inclusion and upload fan-out.
- **`UsageTracker.calculate_cost()`** — automatically routes to
  `calculate_cost_with_cache()` when the response has non-zero cache
  tokens; falls back to the basic `calculate_cost()` otherwise.

#### Router & Gateway wiring

- **`Router.__init__`** — when `track_usage=True`, constructs:
  ```python
  UsageTracker(uploader=UsageUploader(get_config()))
  ```
  All routed requests (sync and streaming) now feed the upload pipeline
  automatically with zero caller effort.
- **`GatewayServer.__init__`** — same pattern:
  ```python
  self.usage = UsageTracker(uploader=UsageUploader(get_config()))
  ```
  Both `_record_stream_usage()` and `_record_usage_if_possible()` now
  capture and forward cache token fields.

#### Data model additions

- **`RouteResponse`** — two new frozen dataclass fields:
  - `cache_creation_input_tokens: int = 0`
  - `cache_read_input_tokens: int = 0`

  Backward-compatible (both default to 0).

- **`StreamUsageChunk`** (TypedDict) — already had optional
  `cache_creation_input_tokens` and `cache_read_input_tokens` keys;
  these are now propagated through to `RouteResponse` and usage records.

#### Cache-aware pricing

- **`CostCalculator.calculate_cost_with_cache()`** — new method:
  ```python
  def calculate_cost_with_cache(
      self, provider, model,
      input_tokens, output_tokens,
      cache_creation_input_tokens,
      cache_read_input_tokens,
  ) -> float
  ```
  Cost formula:
  - Base input: `input_tokens × input_per_million`
  - Cache creation: `cache_creation_input_tokens × write_price`
    (fetched from API; fallback **1.25×** input price)
  - Cache read: `cache_read_input_tokens × read_price`
    (fetched from API; fallback **0.10×** input price)
  - Output: `output_tokens × output_per_million`

- **`_fetch_pricing()`** — now also extracts `input_cache_read` and
  `input_cache_write` from the API response, returning them as
  `input_cache_read_per_million` and `input_cache_write_per_million`
  in the pricing dict.

### Fixes & Hardening

#### Security

- **Gateway auth** — `/health` endpoint now respects `public_paths` config
  and requires auth when not listed
- **Anthropic adapter** — concatenate multiple system messages instead of
  last-wins; prevents silent prompt loss
- **Router** — deduplicate system messages in `_normalize_messages()` when
  both `system` option and messages list contain a system role

#### Provider adapters — client reuse

- **Compatible adapters** (DeepSeek, Groq, Mistral, xAI, etc.) — cache
  HTTP client per `(base_url, api_key)` to prevent connection leak;
  previously created a new `httpx.Client` per request
- **OpenAI adapter** — same client caching pattern
- **Anthropic adapter** — same client caching pattern
- **Google adapter** — check for API key change before reusing cached
  client to avoid stale credentials

#### API client

- **`StatusAPI`** — reuse `httpx.Client` across calls instead of creating
  per-request; add proper `close()` / context manager support

#### Gateway server

- **SSE translator** — fix `finish_reason`/`[DONE]` ordering edge case
  on stream truncation; emit error SSE event on mid-stream upstream failure
- **Streaming usage** — gateway `_record_stream_usage()` now correctly
  captures cache token fields from streamed chunks
- **Translate path** — usage extraction for translated (cross-provider)
  requests now recorded properly

#### Usage & pricing

- **Retry latency** — include sleep backoff time in retry latency
  measurement (was undercounting)
- **Pricing cache** — atomic write-then-rename for `pricing-cache.json`
  to prevent corruption on concurrent access; file cache read hardened
  against malformed JSON

#### Uploader

- **Thread safety** — `UsageUploader` uses class-level
  `threading.Lock` + double-checked locking for shared executor init
  (was racy on first concurrent use)

#### Tests

- New and expanded test suites: `test_uploader.py` (316 lines),
  `test_proxy_model_extraction.py`, `test_pricing.py`,
  `test_model_health.py` — covering retry handling, translate-path
  usage extraction, cooldown persistence, and cache-safe persistence

### Public API

New top-level exports in `aistatus.__init__`:

```python
from aistatus import AIStatusConfig, configure, get_config
```

### User Flow

```python
from aistatus import configure, Router

# One-time setup (persisted to ~/.aistatus/config.yaml)
configure(name="Alice", email="alice@example.com", upload=True)

# Every route() call now uploads usage in the background
router = Router()
resp = router.route("Hello", model="claude-sonnet-4-6")
# → usage record POSTed to aistatus.cc/api/usage/upload (async, silent)
```

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
