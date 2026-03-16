# aistatus

Smart AI model routing with real-time status awareness.

`aistatus` checks provider/model availability through `aistatus.cc`, then calls
your installed provider SDK directly. Your prompts and API keys stay on your side.

## Install

```bash
pip install aistatus
pip install aistatus[anthropic]
pip install aistatus[openai]
pip install aistatus[google]
pip install aistatus[all]
```

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
```

If the primary provider is unavailable, `aistatus` tries other compatible providers
that are available in your environment.

## Tier Routing

Tier routing is supported, but tiers must be configured explicitly:

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

## Async

```python
from aistatus import aroute

resp = await aroute(
    [{"role": "user", "content": "Hello"}],
    model="gpt-4o-mini",
)
```

## Status API

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

## Environment Variables

The SDK auto-discovers providers from standard environment variables:

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
