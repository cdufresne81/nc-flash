"""Unit tests for the pre-flight link-quality gate (goal-2 Part B)."""

from __future__ import annotations

import pytest

from src.ecu.exceptions import UDSTimeoutError
from src.ecu.link_quality import (
    DEFAULT_MAX_P95_MS,
    check_link_quality,
    evaluate_link_quality,
    percentile_95,
)


class _FakeUds:
    """Minimal UDSConnection stand-in: tester_present optionally fails."""

    def __init__(self, fail_indices=()):
        self._fail = set(fail_indices)
        self.calls = 0

    def tester_present(self):
        i = self.calls
        self.calls += 1
        if i in self._fail:
            raise UDSTimeoutError(f"ping {i} dropped")


# --- percentile_95 ----------------------------------------------------------


def test_percentile_95_empty_is_zero():
    assert percentile_95([]) == 0.0


def test_percentile_95_nearest_rank():
    # 20 samples 1..20: 95th percentile (nearest-rank, idx int(0.95*19)=18) -> 19.
    assert percentile_95([float(x) for x in range(1, 21)]) == 19.0


# --- evaluate_link_quality (pure verdict) -----------------------------------


class TestEvaluateLinkQuality:
    def test_clean_link_passes(self):
        ok, reason = evaluate_link_quality(25, 25, 0.0, 50.0)
        assert ok is True
        assert "clean" in reason

    def test_zero_pings_fails(self):
        ok, _ = evaluate_link_quality(0, 0, 0.0, 0.0)
        assert ok is False

    def test_no_replies_fails(self):
        ok, reason = evaluate_link_quality(0, 25, 100.0, 0.0)
        assert ok is False
        assert "no replies" in reason

    def test_any_loss_fails_by_default(self):
        # Default max_loss_pct is 0 — a single drop blocks the flash.
        ok, reason = evaluate_link_quality(24, 25, 4.0, 50.0)
        assert ok is False
        assert "loss" in reason

    def test_high_latency_fails(self):
        ok, reason = evaluate_link_quality(25, 25, 0.0, DEFAULT_MAX_P95_MS + 1.0)
        assert ok is False
        assert "latency" in reason

    def test_loss_tolerated_when_allowed(self):
        ok, _ = evaluate_link_quality(24, 25, 4.0, 50.0, max_loss_pct=10.0)
        assert ok is True


# --- check_link_quality (driver over a fake uds) ----------------------------


class TestCheckLinkQuality:
    def test_clean_link_is_ok(self):
        uds = _FakeUds()
        result = check_link_quality(uds, pings=10)
        assert result.replies == 10
        assert result.loss_pct == 0.0
        assert result.ok is True

    def test_drops_block_the_flash(self):
        uds = _FakeUds(fail_indices=(2, 7))
        result = check_link_quality(uds, pings=10)
        assert result.replies == 8
        assert result.loss_pct == pytest.approx(20.0)
        assert result.ok is False

    def test_all_dropped_is_not_ok(self):
        uds = _FakeUds(fail_indices=range(10))
        result = check_link_quality(uds, pings=10)
        assert result.replies == 0
        assert result.ok is False

    def test_progress_callback_fires_per_ping(self):
        uds = _FakeUds()
        seen = []
        check_link_quality(uds, pings=5, progress_cb=lambda d, t: seen.append((d, t)))
        assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
