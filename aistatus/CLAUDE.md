一旦此文件夹有文件变化，请更新我

Core Python SDK package for aistatus routing, pricing, usage tracking, and CLI entrypoints.
Shared library code lives here; gateway-specific implementation remains under `gateway/`.
Public imports are exposed from `__init__.py`, while internal modules implement routing, pricing, and persistence.

| filename | role | function |
|---|---|---|
| `__init__.py` | public API | Export SDK entrypoints, models, exceptions, and lazy router helpers |
| `__main__.py` | CLI entry | Run package-level CLI commands |
| `_defaults.py` | config defaults | Hold default constants, provider aliases, and slug normalization helpers |
| `api.py` | API client | Query aistatus status/model APIs |
| `content.py` | content helpers | Extract text from and normalize ContentBlock lists |
| `middleware.py` | hook definitions | Middleware Protocol with before_request, after_response, on_error hooks |
| `stream.py` | stream utilities | collect_stream_text and stream_to_text_chunks async helpers |
| `exceptions.py` | error model | Define SDK exception types |
| `models.py` | data model | Define routing/status dataclasses and types |
| `pricing.py` | pricing lookup | Resolve model pricing and estimate token costs |
| `config.py` | upload config | Persist and resolve SDK upload identity/config from runtime, env, and file sources |
| `uploader.py` | upload bridge | Build usage upload payloads and POST them asynchronously with silent failure semantics |
| `router.py` | routing engine | Route requests across providers with fallback logic |
| `usage.py` | usage tracker | Record request usage and aggregate summaries |
| `usage_storage.py` | persistence | Store and read usage records on disk |
| `cli/` | CLI package | Subcommands and command-line integration |
| `providers/` | provider adapters | Integrate Anthropic, OpenAI, Google, and other backends |
| `gateway/` | gateway package | Local HTTP gateway server, config, and health tracking |
