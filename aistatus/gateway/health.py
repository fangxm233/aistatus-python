"""Health tracking for gateway backends.

Tracks per-backend error rates using a sliding window + cooldown mechanism.
When a backend returns 429 / 5xx, it is marked unhealthy for a cooldown period.

Supports dual-layer tracking:
- Backend level: is_healthy("anthropic:key:0")
- Model level: is_healthy("anthropic:key:0", model="claude-opus-4-6")

Model-level and backend-level health are independent. A model-specific error
(e.g. opus rate-limited) does not mark the backend unhealthy, and vice versa.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


# Cooldown durations (seconds) by HTTP status
_COOLDOWNS = {
    429: 30,   # rate-limited → back off 30s
    500: 15,
    502: 10,
    503: 10,
    529: 30,   # Anthropic overloaded
}
_DEFAULT_COOLDOWN = 10

# Sliding window for error rate tracking
_WINDOW_SIZE = 60  # seconds
_MAX_ERRORS_IN_WINDOW = 5  # mark unhealthy after this many errors


@dataclass
class _BackendState:
    cooldown_until: float = 0.0
    errors: deque = field(default_factory=lambda: deque(maxlen=50))
    total_requests: int = 0
    total_errors: int = 0


class HealthTracker:
    """Track backend health with cooldowns and error windows.

    Dual-layer: backend-level + (backend, model)-level tracking.
    When model is provided, operations target the model layer only.
    When model is omitted, operations target the backend layer only (backward compat).
    """

    def __init__(self):
        self._state: dict[str, _BackendState] = {}
        self._model_state: dict[tuple[str, str], _BackendState] = {}

    def is_healthy(self, backend_id: str, *, model: str | None = None) -> bool:
        s = self._get_state(backend_id, model)
        now = time.monotonic()

        # Check cooldown
        if now < s.cooldown_until:
            return False

        # Check error window
        cutoff = now - _WINDOW_SIZE
        recent = sum(1 for t in s.errors if t > cutoff)
        if recent >= _MAX_ERRORS_IN_WINDOW:
            return False

        return True

    def record_error(self, backend_id: str, status_code: int, *, model: str | None = None):
        s = self._get_state(backend_id, model)
        now = time.monotonic()
        s.errors.append(now)
        s.total_errors += 1
        s.total_requests += 1

        cooldown = _COOLDOWNS.get(status_code, _DEFAULT_COOLDOWN)
        s.cooldown_until = max(s.cooldown_until, now + cooldown)

    def record_success(self, backend_id: str, *, model: str | None = None):
        s = self._get_state(backend_id, model)
        s.total_requests += 1
        # Successful request clears cooldown (backend/model recovered)
        s.cooldown_until = 0.0

    def error_count(self, backend_id: str, *, model: str | None = None) -> int:
        return self._get_state(backend_id, model).total_errors

    def summary(self) -> dict[str, dict]:
        """Return a JSON-friendly summary of all backends and models."""
        now = time.monotonic()
        out = {}

        # Backend-level summary (unchanged format)
        for bid, s in self._state.items():
            cutoff = now - _WINDOW_SIZE
            recent_errors = sum(1 for t in s.errors if t > cutoff)
            out[bid] = {
                "healthy": self.is_healthy(bid),
                "recent_errors": recent_errors,
                "total_errors": s.total_errors,
                "total_requests": s.total_requests,
                "cooldown_remaining": max(0, round(s.cooldown_until - now, 1)),
            }

        # Model-level summary
        if self._model_state:
            model_health = {}
            for (bid, model), s in self._model_state.items():
                cutoff = now - _WINDOW_SIZE
                recent_errors = sum(1 for t in s.errors if t > cutoff)
                key = f"{bid}/{model}"
                model_health[key] = {
                    "healthy": self.is_healthy(bid, model=model),
                    "recent_errors": recent_errors,
                    "total_errors": s.total_errors,
                    "total_requests": s.total_requests,
                    "cooldown_remaining": max(0, round(s.cooldown_until - now, 1)),
                }
            out["model_health"] = model_health

        return out

    def _get_state(self, backend_id: str, model: str | None) -> _BackendState:
        """Return the appropriate state object for the given layer."""
        if model is not None:
            return self._model_state.setdefault((backend_id, model), _BackendState())
        return self._state.setdefault(backend_id, _BackendState())
