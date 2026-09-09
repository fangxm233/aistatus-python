一旦此文件夹有文件变化，请更新我

Gateway proxy/config/health module for local LLM routing.
Handles config parsing, global model pre-checks, backend selection, model degradation fallback, HTTP proxy responses, and charset-safe response forwarding.
Shared by the SDK gateway runtime without Cortex-specific logic.

| filename | role | function |
|---|---|---|
| `__init__.py` | entry | Start gateway server from SDK API; passes the config path through so server can watch it for hot reload |
| `__main__.py` | CLI | Expose `python -m aistatus.gateway` commands |
| `auth.py` | auth | GatewayAuthConfig dataclass and check_gateway_auth validation |
| `config.py` | config | Load and validate gateway.yaml, endpoints, auth, mode-aware configs, model fallbacks, the `max_body_size_mb` request cap, and the `websocket` proxy switch |
| `health.py` | health | Track backend/model health with cooldown windows that persist through recent-error periods |
| `server.py` | proxy | Serve `/health` `/status` `/usage` `/quota`, pre-mark globally degraded models, proxy upstream requests, account every response through `usage_accounting`, and hot-reload config via `reload_config()` + an mtime-polling watcher task |
| `routing.py` | routing | Pure URL resolution (`/m/{mode}/{metadata}/{endpoint}/{path}` and plain forms) shared by HTTP proxying and WebSocket upgrades |
| `quota_snapshot.py` | quota | Parse Anthropic unified rate-limit headers into per-provider snapshots, validate them, and persist atomically for `/quota` |
| `stream_usage.py` | protocol | Parse usage off a response stream incrementally across Anthropic Messages SSE, OpenAI chat-completions SSE and the OpenAI Responses API (SSE or WebSocket-framed) |
| `websocket_proxy.py` | proxy | Bidirectional WebSocket tunnel built on aiohttp's own client/server WebSockets, recording one usage row per completed response on a pooled socket |
| `usage_accounting.py` | accounting | Single priced-and-persisted usage record path shared by streaming and JSON responses, with vendor attribution |
| `translate.py` | protocol | Translate Anthropic/OpenAI request and SSE formats with non-text warnings and usage-preserving terminal events |

Note: the WebSocket proxy is deliberately implemented differently from the TypeScript SDK's.
That one tunnels raw bytes and sniffs frames, to avoid taking a WebSocket dependency into a package
whose only dependency is a YAML parser. aiohttp is already a hard dependency here and ships both
sides of the protocol, so this uses it directly — aiohttp decodes and re-encodes each frame rather
than passing bytes through untouched. This is a deliberate divergence, not a missed port.
