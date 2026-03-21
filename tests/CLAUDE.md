一旦此文件夹有文件变化，请更新我

SDK test suite for routing and gateway behavior.
Covers backend selection, model health/fallback, global pre-checks, charset-safe proxy responses, usage, and lifecycle regressions.
Tests are written to validate gateway features before implementation changes.

| filename | role | function |
|---|---|---|
| `test_hybrid_backend.py` | regression | Verify backend ordering, config parsing, and status reporting |
| `test_proxy_model_extraction.py` | regression | Verify model extraction, global model pre-checks, model health tracking, and model fallback behavior |
| `test_usage_endpoint.py` | regression | Verify `/usage` HTTP endpoint summaries and validation |
| `test_pricing.py` | regression | Verify pricing lookup handles versioned Claude model IDs and base aliases |
| `test_model_health.py` | regression | Verify `HealthTracker` backend/model dual-layer behavior |
| `test_graceful_shutdown.py` | regression | Verify signal handling and PID cleanup |
