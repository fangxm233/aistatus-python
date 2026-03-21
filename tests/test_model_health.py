"""Tests for model-level health tracking in HealthTracker.

Verifies dual-layer tracking: backend-level + (backend, model)-level.
Model-level health is independent from backend-level health.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from aistatus.gateway.health import HealthTracker


class TestModelHealthIndependence:
    """Model-level and backend-level health are tracked independently."""

    def test_model_error_does_not_affect_backend_health(self):
        """Errors recorded with model= should not mark the backend unhealthy."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        # Record 5 model-level errors (enough to trigger unhealthy)
        for _ in range(5):
            ht.record_error(bid, 529, model="claude-opus-4-6")

        # Model is unhealthy
        assert not ht.is_healthy(bid, model="claude-opus-4-6")
        # Backend is still healthy (no backend-level errors recorded)
        assert ht.is_healthy(bid)

    def test_backend_error_does_not_affect_model_health(self):
        """Errors recorded without model= should not mark any model unhealthy."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        # Record 5 backend-level errors
        for _ in range(5):
            ht.record_error(bid, 429)

        # Backend is unhealthy
        assert not ht.is_healthy(bid)
        # Model-level health is unaffected (no model-level errors)
        assert ht.is_healthy(bid, model="claude-opus-4-6")

    def test_different_models_tracked_independently(self):
        """Each model has its own health state for the same backend."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        # Opus gets rate-limited, sonnet is fine
        for _ in range(5):
            ht.record_error(bid, 529, model="claude-opus-4-6")
        ht.record_success(bid, model="claude-sonnet-4-6")

        assert not ht.is_healthy(bid, model="claude-opus-4-6")
        assert ht.is_healthy(bid, model="claude-sonnet-4-6")
        # Backend itself is still healthy
        assert ht.is_healthy(bid)


class TestModelHealthRecordError:
    """record_error with model= parameter."""

    def test_model_cooldown_applied(self):
        """Model-level cooldown is set on error."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 529, model="claude-opus-4-6")

        # Model should be in cooldown (529 → 30s cooldown)
        assert not ht.is_healthy(bid, model="claude-opus-4-6")

    def test_model_error_count_tracked(self):
        """error_count with model= returns model-level count."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 429, model="claude-opus-4-6")
        ht.record_error(bid, 429, model="claude-opus-4-6")
        ht.record_error(bid, 429, model="claude-sonnet-4-6")

        assert ht.error_count(bid, model="claude-opus-4-6") == 2
        assert ht.error_count(bid, model="claude-sonnet-4-6") == 1
        # Backend-level error count is 0 (no backend-level errors)
        assert ht.error_count(bid) == 0


class TestModelHealthRecordSuccess:
    """record_success with model= parameter."""

    def test_model_success_clears_model_cooldown(self):
        """Success at model level clears model cooldown."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 529, model="claude-opus-4-6")
        assert not ht.is_healthy(bid, model="claude-opus-4-6")

        ht.record_success(bid, model="claude-opus-4-6")
        # After window errors are < threshold, model should be healthy again
        # (success clears cooldown, and only 1 error in window < 5 threshold)
        assert ht.is_healthy(bid, model="claude-opus-4-6")

    def test_model_success_does_not_clear_backend_cooldown(self):
        """Model-level success does not affect backend-level cooldown."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        # Backend-level error
        ht.record_error(bid, 429)
        assert not ht.is_healthy(bid)

        # Model-level success
        ht.record_success(bid, model="claude-opus-4-6")

        # Backend still unhealthy
        assert not ht.is_healthy(bid)


class TestModelHealthBackwardCompat:
    """Backward compatibility: no model= behaves exactly as before."""

    def test_no_model_record_error_same_as_before(self):
        """record_error without model= only affects backend level."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 429)
        assert ht.error_count(bid) == 1

    def test_no_model_is_healthy_same_as_before(self):
        """is_healthy without model= checks backend level only."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        assert ht.is_healthy(bid)
        for _ in range(5):
            ht.record_error(bid, 429)
        assert not ht.is_healthy(bid)

    def test_no_model_record_success_same_as_before(self):
        """record_success without model= clears backend cooldown."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 429)
        assert not ht.is_healthy(bid)

        ht.record_success(bid)
        assert ht.is_healthy(bid)


class TestModelHealthSummary:
    """summary() includes model_health data."""

    def test_summary_includes_model_health(self):
        """summary() returns model_health alongside backend health."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 529, model="claude-opus-4-6")
        ht.record_success(bid, model="claude-sonnet-4-6")

        s = ht.summary()

        # Backend-level entry should exist (even if no backend-level errors)
        # because model-level activity implies the backend was used
        assert "model_health" in s

        mh = s["model_health"]
        key = "anthropic:key:0/claude-opus-4-6"
        assert key in mh
        assert mh[key]["total_errors"] == 1
        assert not mh[key]["healthy"]

        key_s = "anthropic:key:0/claude-sonnet-4-6"
        assert key_s in mh
        assert mh[key_s]["total_errors"] == 0
        assert mh[key_s]["healthy"]

    def test_summary_backend_level_unchanged(self):
        """Backend-level summary format is unchanged."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        ht.record_error(bid, 429)
        ht.record_success(bid)

        s = ht.summary()

        # Backend-level entry exists with same format as before
        assert bid in s
        assert "healthy" in s[bid]
        assert "recent_errors" in s[bid]
        assert "total_errors" in s[bid]
        assert "total_requests" in s[bid]
        assert "cooldown_remaining" in s[bid]


class TestModelHealthWindowBehavior:
    """Sliding window works correctly at model level."""

    def test_model_errors_expire_from_window(self):
        """Model-level errors older than window are not counted."""
        ht = HealthTracker()
        bid = "anthropic:key:0"

        # Record errors that appear to be in the past
        with patch("aistatus.gateway.health.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            for _ in range(5):
                ht.record_error(bid, 529, model="claude-opus-4-6")

            # Now check health at a time after the window expired
            mock_time.monotonic.return_value = 200.0  # 100s later, window is 60s
            assert ht.is_healthy(bid, model="claude-opus-4-6")
