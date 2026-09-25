"""Transfer helpers: human size/duration/rate strings and the live rate meter."""

import pytest

from src.utils.transfer import (
    TransferRateMeter,
    format_duration,
    format_rate,
    format_size,
    format_time_left,
    transfer_summary,
)


def test_format_size():
    assert format_size(512) == "1 KB"  # never "0 KB" for a real file
    assert format_size(512 * 1024) == "512 KB"
    assert format_size(int(12.4 * 1024 * 1024)) == "12.4 MB"


@pytest.mark.parametrize(
    "seconds, text",
    [
        (0, "0.0s"),
        (4.24, "4.2s"),
        (59.96, "1m 00s"),  # never "60.0s"
        (68, "1m 08s"),
        (3599.6, "1h 00m"),
        (3725, "1h 02m"),
        (-1, "0.0s"),
    ],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_format_rate():
    assert format_rate(0) == "0 KB/s"
    assert format_rate(412 * 1024) == "412 KB/s"
    assert format_rate(1.25 * 1024 * 1024) == "1.2 MB/s"


def test_format_time_left_buckets():
    assert format_time_left(0.4) == "~5 s left"  # never "~0 s left"
    assert format_time_left(37) == "~40 s left"
    assert format_time_left(61) == "~2 min left"


def test_transfer_summary():
    mb27 = int(27.4 * 1024 * 1024)
    assert transfer_summary(mb27, 68) == "27.4 MB in 1m 08s, 413 KB/s"
    assert transfer_summary(1000, 0) == "1 KB in 0.0s, 0 KB/s"  # no div-by-zero


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_rate_meter_unknown_until_min_span():
    clock = _Clock()
    meter = TransferRateMeter(clock=clock)
    assert meter.update(0) == 0.0
    clock.t = 1.0
    assert meter.update(400_000) == 0.0  # a first-chunk burst is not a rate
    clock.t = 3.0
    assert meter.update(300_000 * 3) == pytest.approx(300_000)


def test_rate_meter_is_steady_through_bursts_and_stalls():
    # Guards against a return to a sliding window: on the bursty WiCAN link
    # the shown rate must stay near the true average, not swing with each burst.
    clock = _Clock()
    meter = TransferRateMeter(clock=clock)
    done = 0
    meter.update(done)
    readings = []
    for _ in range(12):  # 1 s bursts at 400 KB/s, then 3 s stalls → 100 KB/s
        for _ in range(10):
            clock.t += 0.1
            done += 40_000
            readings.append(meter.update(done))
        for _ in range(30):
            clock.t += 0.1
            readings.append(meter.update(done))
    settled = readings[200:]  # from ~20 s in
    assert min(settled) >= 99_000 and max(settled) < 120_000
    assert readings[-1] == pytest.approx(100_000, rel=0.05)
