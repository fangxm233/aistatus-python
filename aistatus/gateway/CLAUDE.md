一旦此文件夹有文件变化，请更新我

Gateway proxy/config/health module for local LLM routing.
Handles config parsing, backend selection, model degradation fallback, and HTTP proxy responses.
Shared by the SDK gateway runtime without Cortex-specific logic.

| filename | role | function |
|---|---|---|
| `__init__.py` | entry | Start gateway server from SDK API |
| `__main__.py` | CLI | Expose `python -m aistatus.gateway` commands |
| `config.py` | config | Load and validate gateway.yaml, endpoints, and model fallbacks |
| `health.py` | health | Track backend/model health with cooldown windows |
| `server.py` | proxy | Serve `/health` `/status` `/usage` and proxy upstream requests |
| `translate.py` | protocol | Translate Anthropic/OpenAI request and SSE formats when needed |
