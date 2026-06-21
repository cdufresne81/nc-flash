"""Pre-flight link-quality gate for the lossy-link (WiCAN) flash path.

A flash MUST NOT start over a link that drops frames. The Mazda NC write path
has **no mid-stream resend** — there is no block sequence counter, so resending
a consumed ``TransferData`` block shifts everything and bricks the ECU (see
``flash_manager`` / ``WICAN_TRANSPORT.md`` §6). The only safe recovery from a
dropped block is to abort and restart the whole flash. This gate runs a burst of
Tester Present round-trips and refuses the flash up front if the link isn't clean
enough, turning a mid-flash failure (expensive, restart-from-scratch) into a
cheap pre-flight refusal.

Reads and diagnostics are idempotent and are **never** gated — this is
flash-only. The check is transport-agnostic: it works over any open
``UDSConnection``. A wired J2534 link answers every ping with low latency, so it
always passes; the WiCAN (WiFi) link is the real subject.

Headless module: standard library + sibling core modules only (no PySide6).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .exceptions import ECUError

logger = logging.getLogger(__name__)

#: Tester Present round-trips fired by the gate (matches the bench smoke test).
DEFAULT_PINGS = 25

#: Maximum tolerated packet loss for a flash. Zero: a single dropped Tester
#: Present means the link is not trustworthy for a no-resend write.
DEFAULT_MAX_LOSS_PCT = 0.0

#: Maximum tolerated p95 round-trip latency (ms). A clean WiCAN link answers in
#: ~55 ms; a p95 well above that signals congestion/retransmits that raise the
#: odds of a mid-flash drop. Generous default; tune from a bench latency trace.
DEFAULT_MAX_P95_MS = 250.0


@dataclass
class LinkQualityResult:
    """Outcome of a :func:`check_link_quality` run."""

    pings: int
    replies: int
    loss_pct: float
    p95_ms: float
    ok: bool
    reason: str
    #: Raw round-trip times for the replies (kept for bench/UI display of the
    #: latency distribution; the pass/fail verdict only needs p95 + loss).
    latencies_ms: list[float] = field(default_factory=list)


def percentile_95(values: list[float]) -> float:
    """Return the 95th-percentile value (nearest-rank; pure, no I/O).

    Matches the estimator used by the bench tool so the gate and the bench
    report the same p95 for the same samples. Empty input returns 0.0.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))
    return ordered[idx]


def evaluate_link_quality(
    replies: int,
    pings: int,
    loss_pct: float,
    p95_ms: float,
    *,
    max_loss_pct: float = DEFAULT_MAX_LOSS_PCT,
    max_p95_ms: float = DEFAULT_MAX_P95_MS,
) -> tuple[bool, str]:
    """Decide pass/fail from the measured stats (pure; unit-testable).

    Returns ``(ok, reason)``. ``reason`` is a short human-readable cause on
    failure, or a confirmation string on success.
    """
    if pings <= 0:
        return False, "no pings were sent"
    if replies == 0:
        return False, (
            "no replies at all — link down, wrong port, 'monitoring' off, or "
            "ignition off"
        )
    if loss_pct > max_loss_pct:
        return False, (
            f"packet loss {loss_pct:.1f}% exceeds the {max_loss_pct:.1f}% flash "
            f"limit ({replies}/{pings} replied)"
        )
    if p95_ms > max_p95_ms:
        return False, (
            f"p95 latency {p95_ms:.0f} ms exceeds the {max_p95_ms:.0f} ms flash "
            "limit (link congested)"
        )
    return True, f"clean — {replies}/{pings} replied, p95 {p95_ms:.0f} ms"


def check_link_quality(
    uds,
    *,
    pings: int = DEFAULT_PINGS,
    max_loss_pct: float = DEFAULT_MAX_LOSS_PCT,
    max_p95_ms: float = DEFAULT_MAX_P95_MS,
    progress_cb: callable | None = None,
) -> LinkQualityResult:
    """Fire ``pings`` Tester Present round-trips and grade the link.

    Args:
        uds: An open ``UDSConnection`` over any transport.
        pings: Number of Tester Present round-trips.
        max_loss_pct: Maximum tolerated loss (default 0 — flash needs a clean link).
        max_p95_ms: Maximum tolerated p95 round-trip latency in ms.
        progress_cb: Optional ``cb(done, total)`` called after each ping.

    Returns:
        A :class:`LinkQualityResult`. A failed result (``ok is False``) means the
        caller must NOT start a flash.
    """
    latencies: list[float] = []
    failures = 0
    for i in range(pings):
        t0 = time.monotonic()
        try:
            uds.tester_present()
            latencies.append((time.monotonic() - t0) * 1000.0)
        except ECUError as exc:
            failures += 1
            logger.debug("link-quality ping %d/%d failed: %s", i + 1, pings, exc)
        if progress_cb:
            progress_cb(i + 1, pings)

    replies = len(latencies)
    loss_pct = (100.0 * failures / pings) if pings else 100.0
    p95 = percentile_95(latencies)
    ok, reason = evaluate_link_quality(
        replies,
        pings,
        loss_pct,
        p95,
        max_loss_pct=max_loss_pct,
        max_p95_ms=max_p95_ms,
    )
    result = LinkQualityResult(
        pings=pings,
        replies=replies,
        loss_pct=loss_pct,
        p95_ms=p95,
        ok=ok,
        reason=reason,
        latencies_ms=latencies,
    )
    logger.info(
        "link-quality: %s (loss %.1f%%, p95 %.0f ms)",
        "PASS" if ok else "FAIL",
        loss_pct,
        p95,
    )
    return result
