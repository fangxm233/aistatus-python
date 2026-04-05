一旦此文件夹有文件变化，请更新我

Provider adapter modules for the Python SDK routing layer.
They normalize provider credentials, message translation, client lifecycle, and sync/async call behavior behind a shared interface.
Shared abstractions live in `base.py`, with concrete adapters per upstream provider family.

| filename | role | function |
|---|---|---|
| `__init__.py` | package | Export provider adapter modules for SDK import paths |
| `base.py` | abstraction | Define the provider adapter interface, optional streaming hooks, registry, and adapter factory |
| `anthropic_.py` | provider adapter | Map SDK chat requests onto Anthropic Messages APIs with cached sync/async clients |
| `openai_.py` | provider adapter | Map SDK chat requests onto OpenAI chat/completions APIs |
| `google_.py` | provider adapter | Map SDK chat requests onto Google Gemini APIs with cached client reuse and timeout propagation |
| `compatible_.py` | provider adapter | Register OpenAI-compatible adapters such as DeepSeek and OpenRouter-style backends |
| `openrouter_.py` | provider adapter | Implement OpenRouter-specific provider behavior on top of OpenAI-compatible clients |
