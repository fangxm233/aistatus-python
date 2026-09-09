# input:  Anthropic rate-limit response headers and a filesystem snapshot path
# output: normalized provider quota snapshots and atomic persistence
# pos:    Gateway latest-value store for provider quota headers
# >>> 一旦我被更新，务必更新我的开头注释与所属文件夹 CLAUDE.md <<<

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("aistatus.gateway")

DEFAULT_PATH = Path.home() / ".aistatus" / "quota.json"

#: Window type paired with the token Anthropic uses for it on the wire.
WINDOW_HEADERS: tuple[tuple[str, str], ...] = (("five_hour", "5h"), ("seven_day", "7d"))
WINDOW_TYPES = frozenset({"five_hour", "seven_day"})


@dataclass
class QuotaWindow:
    """Utilization of one rate-limit window, as a fraction, with its reset time."""

    type: str
    utilization: float
    resets_at: float


@dataclass
class ProviderQuota:
    """The most recent quota reading seen for one provider."""

    provider: str
    mode: str
    windows: list[QuotaWindow]
    observed_at: int


def _finite_header(headers: Any, name: str, minimum: float, maximum: float = math.inf) -> float | None:
    raw = headers.get(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and minimum <= value <= maximum else None


def _explicit_window(headers: Any, window_type: str, wire_name: str) -> QuotaWindow | None:
    prefix = f"anthropic-ratelimit-unified-{wire_name}"
    utilization = _finite_header(headers, f"{prefix}-utilization", 0, 1)
    resets_at = _finite_header(headers, f"{prefix}-reset", 0)
    if utilization is None or resets_at is None:
        return None
    return QuotaWindow(type=window_type, utilization=utilization, resets_at=resets_at)


def _claimed_type(value: str | None) -> str | None:
    if value in ("5h", "five_hour"):
        return "five_hour"
    if value in ("7d", "seven_day"):
        return "seven_day"
    return None


def _rejected_window(headers: Any) -> QuotaWindow | None:
    """A 429 names the window it rejected on rather than reporting per-window utilization."""
    if headers.get("anthropic-ratelimit-unified-status") != "rejected":
        return None
    window_type = _claimed_type(headers.get("anthropic-ratelimit-unified-representative-claim"))
    resets_at = _finite_header(headers, "anthropic-ratelimit-unified-reset", 0)
    if window_type is None or resets_at is None:
        return None
    return QuotaWindow(type=window_type, utilization=1.0, resets_at=resets_at)


def parse_windows(headers: Any) -> list[QuotaWindow]:
    explicit = [
        window
        for window_type, wire_name in WINDOW_HEADERS
        if (window := _explicit_window(headers, window_type, wire_name)) is not None
    ]
    if explicit:
        return explicit
    rejected = _rejected_window(headers)
    return [rejected] if rejected else []


def _valid_window(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    utilization = value.get("utilization")
    resets_at = value.get("resets_at")
    return (
        value.get("type") in WINDOW_TYPES
        and isinstance(utilization, (int, float))
        and not isinstance(utilization, bool)
        and math.isfinite(utilization)
        and 0 <= utilization <= 1
        and isinstance(resets_at, (int, float))
        and not isinstance(resets_at, bool)
        and math.isfinite(resets_at)
        and resets_at >= 0
    )


def _valid_snapshot(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    windows = value.get("windows")
    observed_at = value.get("observed_at")
    return (
        isinstance(value.get("provider"), str)
        and bool(value["provider"])
        and isinstance(value.get("mode"), str)
        and bool(value["mode"])
        and isinstance(observed_at, (int, float))
        and not isinstance(observed_at, bool)
        and math.isfinite(observed_at)
        and isinstance(windows, list)
        and bool(windows)
        and all(_valid_window(window) for window in windows)
    )


class QuotaSnapshotStore:
    """Latest-value store for provider quota headers, persisted so restarts keep the reading.

    Validates on load: the file is user-writable state, and a malformed entry should be dropped
    rather than served to a caller as if the provider had reported it.
    """

    def __init__(self, file_path: Path | str = DEFAULT_PATH) -> None:
        self._file_path = Path(file_path)
        self._snapshots: dict[str, ProviderQuota] = {}
        for raw in self._read_file():
            self._snapshots[raw["provider"]] = ProviderQuota(
                provider=raw["provider"],
                mode=raw["mode"],
                windows=[QuotaWindow(**window) for window in raw["windows"]],
                observed_at=int(raw["observed_at"]),
            )

    def list(self, provider: str | None = None) -> list[dict[str, Any]]:
        snapshots = [
            snapshot
            for snapshot in self._snapshots.values()
            if not provider or snapshot.provider == provider
        ]
        snapshots.sort(key=lambda snapshot: snapshot.provider)
        return [asdict(snapshot) for snapshot in snapshots]

    def observe(self, headers: Any, *, provider: str, mode: str, status: int,
                now: float | None = None) -> bool:
        """Record the quota headers of one response. Returns whether a snapshot was stored.

        A 429 is kept because it carries the most interesting reading of all; other error statuses
        are ignored since their headers may not reflect real quota state.
        """
        if status != 429 and not (200 <= status < 300):
            return False
        windows = parse_windows(headers)
        if not windows:
            return False
        self._snapshots[provider] = ProviderQuota(
            provider=provider,
            mode=mode,
            windows=windows,
            observed_at=int(now if now is not None else time.time()),
        )
        self._persist_best_effort()
        return True

    # --- private ---

    def _read_file(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return []
        providers = payload.get("providers")
        if not isinstance(providers, list):
            return []
        return [entry for entry in providers if _valid_snapshot(entry)]

    def _persist_best_effort(self) -> None:
        try:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            staging = self._file_path.with_suffix(f".{os.getpid()}.tmp")
            payload = {"version": 1, "providers": self.list()}
            staging.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            staging.chmod(0o600)
            os.replace(staging, self._file_path)
        except OSError as error:
            logger.warning("Failed to persist quota snapshot: %s", error)
