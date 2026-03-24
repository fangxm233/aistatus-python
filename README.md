# aistatus

Status-aware LLM routing for more reliable agents and coding CLIs.

`aistatus` is a Python SDK that checks provider and model availability through
`aistatus.cc`, picks a healthy route, and then calls your installed provider SDK
directly. Prompts and API keys stay in your own process. `aistatus` only helps
with status checks, routing, and fallback selection.

This package is useful when you are building:

- multi-step agents that can fail if one model call breaks mid-run
- coding CLIs that need stable model access during edit, retry, and repair loops
- internal tools that want graceful failover across multiple providers

## Why This Package Exists

Agent workflows are brittle when they assume one provider is always healthy.
That brittleness gets worse in long-running pipelines: a research agent, coding
assistant, or automation bot might make 10 to 50 model calls in one task. If a
single provider is degraded or temporarily unavailable, the whole run can fail.

`aistatus` adds a small routing layer in front of those calls:

- do a pre-flight health check before dispatching a request
- select a compatible fallback when the primary route is unavailable
- keep one Python API even when you use multiple providers
- return routing metadata so your app can observe fallback behavior

In practice, that means better stability for Agent systems and Coding CLI tools:
the workflow can keep moving instead of failing hard on one provider incident.

## How It Works

1. `aistatus` auto-discovers providers from environment variables, or you can
   register providers manually.
2. Before sending a request, it queries `aistatus.cc` for provider or model
   status and compatible alternatives.
3. If the primary route is healthy, it uses it.
4. If the primary route is unavailable, or a provider call fails,
   `aistatus` can automatically try the next available provider.
5. The actual LLM request is executed through the provider SDK installed in your
   environment, not proxied through `aistatus`.
6. You get back a unified `RouteResponse` with the chosen model, provider, and
   fallback metadata.

If the status API is unreachable, the router falls back to model-prefix
guessing and only uses adapters that are available locally.

## What You Get

- Real-time pre-flight checks for providers and models
- Automatic fallback across compatible providers
- Tier-based routing for fast / standard / premium model groups
- One sync and async API across multiple model vendors
- Direct provider SDK calls with local API keys
- Auto-discovery from standard environment variables
- Manual registration for custom or self-hosted OpenAI-compatible endpoints
- Unified response metadata for logging and reliability analysis

## Supported Providers

Current built-in adapters cover:

- Anthropic
- OpenAI
- Google Gemini
- OpenRouter
- DeepSeek
- Mistral
- xAI
- Groq
- Together
- Moonshot
- Qwen / DashScope

OpenAI-compatible providers reuse the `openai` Python client under the hood.

## Install

Install the base package plus the provider SDKs you actually want to use:

```bash
pip install aistatus
pip install aistatus[anthropic]
pip install aistatus[openai]
pip install aistatus[google]
pip install aistatus[all]
```

Notes:

- `aistatus[openai]` also covers OpenAI-compatible providers such as OpenRouter,
  DeepSeek, Mistral, xAI, Groq, Together, Moonshot, and Qwen.
- The base package includes the router and status API client.
- Provider extras install the vendor SDKs used for actual model calls.

## Quickstart

Set at least one provider API key, then route by model name:

```python
from aistatus import route

resp = route(
    "Summarize the latest deployment status.",
    model="claude-sonnet-4-6",
)

print(resp.content)
print(resp.model_used)
print(resp.provider_used)
print(resp.was_fallback)
print(resp.fallback_reason)
```

If the primary provider is unavailable, `aistatus` will try compatible
providers that are both healthy and configured in your environment.

## Why This Helps Agents And Coding CLIs

For simple scripts, a retry loop may be enough. For agents and coding tools, it
usually is not.

- An agent often chains planning, retrieval, synthesis, and repair into one run.
- A coding CLI may need several model calls for diagnosis, patch generation,
  test-fix loops, and final explanation.
- When those systems depend on one provider, a brief outage can break the whole
  interaction.

`aistatus` improves stability by checking route health before the call and
falling back automatically when the preferred route is not available. That gives
you a more resilient default for production agents, internal coding tools, and
developer-facing CLIs.

## Tier Routing

Tier routing is explicit and predictable: you define ordered model groups and
let the router try them in sequence.

```python
from aistatus import Router

router = Router(check_timeout=2.0)
router.add_tier("fast", [
    "claude-haiku-4-5",
    "gpt-4o-mini",
    "gemini-2.0-flash",
])
router.add_tier("standard", [
    "claude-sonnet-4-6",
    "gpt-4o",
    "gemini-2.5-pro",
])

resp = router.route(
    "Explain quantum computing in one sentence.",
    tier="fast",
)
```

This is a good fit when you want stable behavioral buckets such as `fast`,
`standard`, or `premium`, without hard-coding one vendor per workflow step.

## Agent Pipeline Example

`aistatus` is especially useful for multi-step agents. A simple pattern is:

```python
from aistatus import route

plan = route(
    "How is embodied AI changing manufacturing?",
    model="claude-haiku-4-5",
    system="Break the topic into 3 research sub-questions. Be concise.",
)

answer = route(
    plan.content,
    model="claude-sonnet-4-6",
    prefer=["anthropic", "google"],
)
```

See [`examples/agent_pipeline.py`](examples/agent_pipeline.py) for a full
multi-step example that uses different model tiers for planning, research, and
synthesis.

## Manual Provider Registration

You can register custom providers directly when auto-discovery is not enough.
This is useful for self-hosted gateways or OpenAI-compatible endpoints.

```python
from aistatus import ProviderConfig, Router

router = Router(auto_discover=False)
router.register_provider(
    ProviderConfig(
        slug="local-vllm",
        adapter_type="openai",
        api_key="dummy",
        base_url="http://localhost:8000/v1",
    )
)

resp = router.route("Hello", model="gpt-4o-mini")
```

## Async

```python
from aistatus import aroute

resp = await aroute(
    [{"role": "user", "content": "Hello"}],
    model="gpt-4o-mini",
)
```

## Status API

You can also query `aistatus.cc` directly without sending any model request:

```python
from aistatus import StatusAPI

api = StatusAPI()

check = api.check_provider("anthropic")
print(check.status)
print(check.is_available)

for provider in api.providers():
    print(provider.name, provider.status.value)

for model in api.search_models("sonnet"):
    print(model.id, model.prompt_price, model.completion_price)
```

This is useful for dashboards, health checks, pre-deployment validation, or
building your own routing policy on top of the status data.

## Response Object

Every `route()` call returns a `RouteResponse`:

```python
@dataclass
class RouteResponse:
    content: str
    model_used: str
    provider_used: str
    was_fallback: bool
    fallback_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    raw: Any = None
```

The routing metadata makes it easy to log fallback events and understand how
stable your agent or CLI is in real traffic.

## Errors

```python
from aistatus import AllProvidersDown, ProviderNotInstalled, route

try:
    resp = route("Hello", model="claude-sonnet-4-6")
except AllProvidersDown as e:
    print(e.tried)
except ProviderNotInstalled as e:
    print(f"Install support for: {e.provider}")
```

Common failure modes:

- `AllProvidersDown`: no configured provider could successfully serve the call
- `ProviderNotInstalled`: the required provider SDK extra is missing
- `ProviderCallFailed`: the selected provider failed and fallback was disabled

## Gateway

The gateway is a local HTTP proxy that sits between your application and
provider APIs. It adds multi-key rotation, automatic failover across providers,
per-model health tracking, protocol translation, and usage recording — all
transparent to the calling application.

### Quick Start

The fastest way to start is auto-discovery, which reads your environment
variables and sets up endpoints automatically:

```bash
pip install aistatus[gateway]
python -m aistatus.gateway start --auto
```

Then point your tools at the gateway:

```bash
export ANTHROPIC_BASE_URL=http://localhost:9880/anthropic
export OPENAI_BASE_URL=http://localhost:9880/openai/v1
```

### Configuration

Create `~/.aistatus/gateway.yaml` for full control. Generate an example:

```bash
python -m aistatus.gateway init
```

A typical configuration:

```yaml
port: 9880

anthropic:
  keys:
    - $ANTHROPIC_API_KEY
  fallbacks:
    - name: openrouter
      base_url: https://openrouter.ai/api/v1
      key: $OPENROUTER_API_KEY
      model_prefix: "anthropic/"
      translate: anthropic-to-openai
  model_fallbacks:
    claude-opus-4-6:
      - claude-sonnet-4-6
      - claude-haiku-4-5

openai:
  keys:
    - $OPENAI_API_KEY
  fallbacks:
    - name: openrouter
      base_url: https://openrouter.ai/api/v1
      key: $OPENROUTER_API_KEY
      model_prefix: "openai/"
```

Environment variable references (`$VAR_NAME`) are resolved at load time.

### How the Gateway Routes Requests

When a request arrives at `/{endpoint}/{path}`:

1. **Managed keys** — tries configured API keys in round-robin order
2. **Passthrough** — if hybrid mode is enabled (default), tries the caller's own
   API key
3. **Fallbacks** — tries secondary providers in order

If a backend returns a retryable error (429, 500, 502, 503, 529), the gateway
marks it unhealthy with a cooldown and tries the next backend. A successful
request clears the cooldown.

### Model Fallbacks

Model-level fallback chains let the gateway downgrade gracefully when a specific
model is degraded:

```yaml
anthropic:
  keys: [$ANTHROPIC_API_KEY]
  model_fallbacks:
    claude-opus-4-6: [claude-sonnet-4-6, claude-haiku-4-5]
```

When `claude-opus-4-6` is unhealthy, the gateway substitutes the first healthy
candidate. The response includes an `x-gateway-model-fallback` header showing
what happened.

### Protocol Translation

The gateway can translate between Anthropic and OpenAI formats automatically.
This lets you route Anthropic API calls to OpenAI-compatible backends like
OpenRouter:

```yaml
anthropic:
  fallbacks:
    - name: openrouter
      base_url: https://openrouter.ai/api/v1
      key: $OPENROUTER_API_KEY
      translate: anthropic-to-openai
```

Translation covers request format, response format, and streaming SSE events.

### Configuration Modes

Modes let you maintain multiple configurations and switch at runtime:

```yaml
mode: production

anthropic:
  production:
    keys: [$ANTHROPIC_PROD_KEY]
    passthrough: false
  development:
    keys: [$ANTHROPIC_DEV_KEY]
    passthrough: true
```

Switch the active mode at runtime:

```bash
curl -X POST http://localhost:9880/mode -d '{"mode": "development"}'
```

Or use per-request mode override without changing the global mode:

```
GET /m/development/anthropic/v1/messages
```

### Health Tracking

The gateway tracks health at both backend and model level using a sliding
60-second error window. After 5 errors in that window, a backend or model is
marked unhealthy with a status-code-specific cooldown (e.g. 30 seconds for 429,
15 seconds for 500). At startup, the gateway can pre-check model health via
`aistatus.cc` and pre-mark degraded models.

### Gateway Authentication

Protect the gateway from unauthorized access (separate from provider API keys):

```yaml
auth:
  enabled: true
  keys: [$GATEWAY_API_KEY]
  public_paths: [/health]
```

### Management Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Quick health check (always public) |
| `/status` | GET | Detailed backend and model health |
| `/usage`  | GET | Usage tracking with cost breakdown |
| `/mode`   | POST | Switch active configuration mode |

The `/usage` endpoint supports `?period=today|week|month|all` and
`?group_by=model|provider`.

### CLI Reference

```bash
python -m aistatus.gateway start [--config PATH] [--host HOST] [-p PORT] [--auto] [--pid-file PATH] [--log-file PATH]
python -m aistatus.gateway init [-o PATH]
```

### Programmatic Usage

```python
from aistatus.gateway import start

start(config_path="gateway.yaml", host="127.0.0.1", port=9880)
```

## Environment Variables

The router auto-discovers providers from standard environment variables:

```bash
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
DEEPSEEK_API_KEY=...
MISTRAL_API_KEY=...
XAI_API_KEY=...
GROQ_API_KEY=...
TOGETHER_API_KEY=...
MOONSHOT_API_KEY=...
DASHSCOPE_API_KEY=...
```

## License

MIT. See [LICENSE](LICENSE).
