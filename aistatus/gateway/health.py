"""Health tracking for gateway backends.

Tracks per-backend error rates using a sliding window + cooldown mechanism.
When a backend returns 429 / 5xx, it is marked unhealthy for a cooldown period.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
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
    """Track backend health with cooldowns and error windows."""

    def __init__(self):
        self._state: dict[str, _BackendState] = defaultdict(_BackendState)

    def is_healthy(self, backend_id: str) -> bool:
        s = self._state[backend_id]
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

    def record_error(self, backend_id: str, status_code: int):
        s = self._state[backend_id]
        now = time.monotonic()
        s.errors.append(now)
        s.total_errors += 1
        s.total_requests += 1

        cooldown = _COOLDOWNS.get(status_code, _DEFAULT_COOLDOWN)
        s.cooldown_until = max(s.cooldown_until, now + cooldown)

    def record_success(self, backend_id: str):
        s = self._state[backend_id]
        s.total_requests += 1
        # Successful request clears cooldown (backend recovered)
        s.cooldown_until = 0.0

    def error_count(self, backend_id: str) -> int:
        return self._state[backend_id].total_errors

    def summary(self) -> dict[str, dict]:
        """Return a JSON-friendly summary of all backends."""
        now = time.monotonic()
        out = {}
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
        return out
