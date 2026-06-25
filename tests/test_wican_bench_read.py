"""Unit tests for the WiCAN bench-read instrumentation helpers.

Covers the hardware-free logic added for the read-speed work (Phase 0):
the per-block timing summary, the ECU read-size probe, and the per-block
timing harness. All drive a tiny duck-typed fake UDS connection, so no
WiCAN device, ECU, or _secure module is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.ecu.constants import ROM_SIZE
from src.ecu.exceptions import NegativeResponseError, UDSTimeoutError

# The bench script lives under tools/ (not an importable package), so load it
# by path. Importing it runs only module-level imports, never main().
_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "wican_bench_read.py"
_spec = importlib.util.spec_from_file_location("wican_bench_read", _TOOL_PATH)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


class _FakeUds:
    """Minimal stand-in for UDSConnection: only what the harness calls."""

    def __init__(self, responder):
        self._responder = responder
        self.flush_count = 0
        self.calls = 0

    def read_memory_by_address(self, address, size, timeout=None, pending_max=None):
        self.calls += 1
        return self._responder(address, size)

    def flush(self):
        self.flush_count += 1


# --- summarize_block_times -------------------------------------------------


def test_summarize_empty_returns_zeros():
    stats = bench.summarize_block_times([], 0x400)
    assert stats["n"] == 0
    assert stats["avg"] == 0.0
    assert stats["extrapolated_1mb_s"] == 0.0


def test_summarize_basic_stats_and_extrapolation():
    # avg = 250 ms; ROM_SIZE / 0x400 = 1024 blocks; 0.25 s * 1024 = 256 s.
    stats = bench.summarize_block_times([100.0, 200.0, 300.0, 400.0], 0x400)
    assert stats["n"] == 4
    assert stats["min"] == 100.0
    assert stats["max"] == 400.0
    assert stats["avg"] == pytest.approx(250.0)
    assert stats["extrapolated_1mb_s"] == pytest.approx(0.25 * (ROM_SIZE / 0x400))


def test_summarize_extrapolation_scales_with_block_size():
    # Bigger blocks => fewer blocks per MB => smaller extrapolated total.
    small = bench.summarize_block_times([200.0], 0x400)
    big = bench.summarize_block_times([200.0], 0xFFE)
    assert big["extrapolated_1mb_s"] < small["extrapolated_1mb_s"]


# --- probe_read_sizes ------------------------------------------------------


def test_probe_marks_supported_sizes_ok_and_rejected_nrc():
    def responder(addr, size):
        if size <= 0x800:
            return b"\x00" * size
        raise NegativeResponseError(0x31, "requestOutOfRange")

    uds = _FakeUds(responder)
    results = bench.probe_read_sizes(uds, [0x400, 0x800, 0xFFE], 0x0, 1000)

    assert [r["ok"] for r in results] == [True, True, False]
    assert "NRC 0x31" in results[2]["status"]


def test_probe_flags_short_read_as_not_ok():
    def responder(addr, size):
        # ECU returns fewer bytes than requested (truncated/garbled).
        return b"\x00" * (size - 1)

    uds = _FakeUds(responder)
    results = bench.probe_read_sizes(uds, [0x400], 0x0, 1000)
    assert results[0]["ok"] is False
    assert "SHORT" in results[0]["status"]


# --- bench_blocks ----------------------------------------------------------


def test_bench_counts_clean_blocks_and_excludes_drops():
    # Second read raises (a dropped block); the rest succeed.
    def responder(addr, size):
        if addr == 0x400:  # the 2nd block (start 0, block 0x400)
            raise UDSTimeoutError("dropped frame")
        return b"\x00" * size

    uds = _FakeUds(responder)
    stats = bench.bench_blocks(uds, 4, 0x400, 0x0, 1000)

    assert stats["n"] == 3  # 3 clean, 1 dropped
    assert uds.calls == 4
    assert uds.flush_count == 1  # flushed once after the drop


def test_bench_short_block_counts_as_error_not_clean():
    def responder(addr, size):
        return b"\x00" * (size - 2)  # always short

    uds = _FakeUds(responder)
    stats = bench.bench_blocks(uds, 3, 0x400, 0x0, 1000)
    assert stats["n"] == 0  # no clean blocks
