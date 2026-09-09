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
| `quota_snapshot.py` | quota | Parse Anthropic unified rate-limit headers into per-provider snapshots, validate them, and persist atomically for `/quota` |
| `stream_usage.py` | protocol | Parse usage off a response stream incrementally across Anthropic Messages SSE, OpenAI chat-completions SSE and the OpenAI Responses API (SSE or WebSocket-framed) |
| `usage_accounting.py` | accounting | Single priced-and-persisted usage record path shared by streaming and JSON responses, with vendor attribution |
| `translate.py` | protocol | Translate Anthropic/OpenAI request and SSE formats with non-text warnings and usage-preserving terminal events |
