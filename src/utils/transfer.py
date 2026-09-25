"""Human-facing file-transfer helpers: size/duration/rate strings + a rate meter.

Used by the WiCAN trip-log download (the headless client's Activity Log lines
and the Trip Logs window's live progress). Standard library only, so the
headless ``src.ecu`` modules and bench tools can use it without Qt or numpy.
"""

import math
import time

#: History a rate needs before it is shown (see :class:`TransferRateMeter`).
_RATE_MIN_SPAN_S = 3.0


def format_size(num_bytes: int) -> str:
    """Human size for status lines and prompts: ``512 KB`` / ``12.4 MB``."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, round(num_bytes / 1024))} KB"


def format_duration(seconds: float) -> str:
    """Human elapsed time: ``4.2s`` / ``1m 08s`` / ``1h 02m``."""
    seconds = max(0.0, seconds)
    if seconds < 59.95:  # would print as "60.0s"
        return f"{seconds:.1f}s"
    whole = int(round(seconds))
    if whole < 3600:
        return f"{whole // 60}m {whole % 60:02d}s"
    return f"{whole // 3600}h {whole % 3600 // 60:02d}m"


def format_time_left(seconds: float) -> str:
    """Coarse remaining-time text for a live progress line (``~40 s left``).

    Bucketed (5 s / whole minutes) so a 10 Hz display does not flicker.
    """
    if seconds < 60:
        return f"~{max(5, 5 * math.ceil(seconds / 5))} s left"
    return f"~{math.ceil(seconds / 60)} min left"


def format_rate(bytes_per_s: float) -> str:
    """Human transfer rate: ``412 KB/s`` / ``1.2 MB/s``."""
    if bytes_per_s >= 1024 * 1024:
        return f"{bytes_per_s / (1024 * 1024):.1f} MB/s"
    return f"{max(0, round(bytes_per_s / 1024))} KB/s"


def transfer_summary(num_bytes: int, elapsed_s: float) -> str:
    """``27.4 MB in 1m 08s, 412 KB/s`` — the log wording for a finished transfer."""
    rate = num_bytes / elapsed_s if elapsed_s > 0 else 0.0
    return (
        f"{format_size(num_bytes)} in {format_duration(elapsed_s)}, "
        f"{format_rate(rate)}"
    )


class TransferRateMeter:
    """Average transfer rate since the first sample (what a live display shows).

    Deliberately not a short sliding window: the WiCAN serves SD files over
    WiFi in bursts separated by stalls, so a windowed rate jumps around
    constantly (bench 2026-09-24: 9-400 KB/s around a ~95 KB/s average).
    Reads 0.0 (unknown) for the first ``_RATE_MIN_SPAN_S`` seconds, so the
    first chunk's burst never shows as an absurd number.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._start = None  # (t, cumulative bytes) of the first sample

    def update(self, done: int) -> float:
        """Record *done* cumulative bytes now; return bytes/s (0.0 = not yet known)."""
        now = self._clock()
        if self._start is None:
            self._start = (now, done)
        t0, d0 = self._start
        span = now - t0
        return (done - d0) / span if span >= _RATE_MIN_SPAN_S else 0.0
