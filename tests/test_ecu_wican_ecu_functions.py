"""Unit tests for WiCAN ECU diagnostic functions (goal-2 Part A).

Two layers, both hardware-free:

  1. The transport-agnostic ``FlashManager`` seam — ``read_dtcs`` / ``clear_dtcs``
     over a real ``UDSConnection`` driven by a ``FakeTransport``, and ``scan_ram``
     over a borrowed connection. This is the EXACT path a WiCAN ``UDSConnection``
     rides on, so a green run here is evidence the WiCAN diagnostics will work
     (the only thing FakeTransport doesn't model is the physical link, which the
     hardware bench tool covers).
  2. The pure helpers in ``tools/wican_bench_ecu.py`` (RAM sanity summary, DTC
     formatting, clear verdict).

No ECU, no WiCAN device, no socket, no _secure module needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ecu.flash_manager import FlashManager
from src.ecu.protocol import DTC, UDSConnection
from src.ecu.transport import FakeTransport

# The bench tool lives under tools/ (not an importable package); load by path,
# which runs only module-level imports, never main().
_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "wican_bench_ecu.py"
_spec = importlib.util.spec_from_file_location("wican_bench_ecu", _TOOL_PATH)
ecu_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ecu_tool)


# --- DTC response framing helpers (build the bytes the ECU would return) ----


def _dtc_count_resp(count: int) -> bytes:
    """ReadDTCCount positive response (SID 0x62): [echo, echo, count]."""
    return bytes([0x62, 0x02, 0x00, count])


def _dtc_status_resp(dtcs: list[tuple[int, int]]) -> bytes:
    """ReadDTCByStatus positive response (SID 0x58): [count, {hi, lo, status}...]."""
    body = bytearray([0x58, len(dtcs)])
    for code, status in dtcs:
        body += bytes([(code >> 8) & 0xFF, code & 0xFF, status])
    return bytes(body)


# --- FlashManager seam: READ DTC over a (fake) transport --------------------


class TestReadDtcsOverTransport:
    """read_dtcs(uds=...) works over a borrowed, non-J2534 UDSConnection."""

    def test_reads_and_parses_a_dtc(self):
        uds = UDSConnection(
            FakeTransport(
                responses=[
                    _dtc_count_resp(1),
                    _dtc_status_resp([(0x0301, 0x08)]),
                ]
            )
        )
        dtcs = FlashManager().read_dtcs(uds=uds)
        assert [d.code for d in dtcs] == [0x0301]
        assert dtcs[0].formatted == "P0301"
        assert dtcs[0].status == 0x08

    def test_no_dtcs_when_count_zero(self):
        # Count 0 short-circuits — no ReadDTCByStatus request is even sent.
        uds = UDSConnection(FakeTransport(responses=[_dtc_count_resp(0)]))
        assert FlashManager().read_dtcs(uds=uds) == []

    def test_conditions_not_correct_returns_empty(self):
        # ECU answers the count request with NRC 0x22 (conditions not correct):
        # read_dtcs must swallow it and return [], not raise.
        nrc_22 = bytes([0x7F, 0x22, 0x22])
        uds = UDSConnection(FakeTransport(responses=[nrc_22]))
        assert FlashManager().read_dtcs(uds=uds) == []


# --- FlashManager seam: CLEAR DTC over a (fake) transport -------------------


class TestClearDtcsOverTransport:
    """clear_dtcs(uds=...) sends the right UDS request over any transport."""

    def test_clear_sends_clear_request(self):
        transport = FakeTransport(responses=[bytes([0x54, 0xFF, 0x00])])
        uds = UDSConnection(transport)
        FlashManager().clear_dtcs(uds=uds)
        # ClearDiagnosticInformation: SID 0x14 + groupOfDTC 0xFF 0x00.
        assert transport.sent_payloads == [bytes([0x14, 0xFF, 0x00])]


# --- FlashManager seam: READ RAM over a borrowed connection -----------------


class TestScanRamOverTransport:
    """scan_ram dumps 192 pages (48 KB) from 0xFFFF0000 over the borrowed UDS."""

    @staticmethod
    def _make_fm():
        uds = MagicMock()
        uds.read_memory_by_address.side_effect = lambda addr, size, **kw: b"\x5a" * size
        fm = FlashManager()
        fm.use_uds(uds)
        # RAM scan authenticates over self._uds; stub it (no _secure in CI).
        fm._connect = lambda *a, **k: None
        fm._authenticate = lambda *a, **k: None
        return fm, uds

    def test_scans_full_ram_window(self):
        fm, uds = self._make_fm()
        ram = fm.scan_ram()
        assert len(ram) == 192 * 0x100  # 0xC000 = 48 KB
        assert uds.read_memory_by_address.call_count == 192

    def test_walks_addresses_from_ram_base(self):
        fm, uds = self._make_fm()
        fm.scan_ram()
        addrs = [c.args[0] for c in uds.read_memory_by_address.call_args_list]
        assert addrs[0] == 0xFFFF0000
        assert addrs[1] == 0xFFFF0100
        assert addrs[-1] == 0xFFFF0000 + 191 * 0x100

    def test_recovers_a_dropped_page(self):
        # Hardware exposed this: a single dropped frame used to abort the whole
        # RAM scan. It now retries the page (reads are idempotent), like read_rom.
        from src.ecu.exceptions import UDSTimeoutError

        state = {"n": 0}

        def rmba(addr, size, **kw):
            state["n"] += 1
            if state["n"] == 1:  # drop the very first page once
                raise UDSTimeoutError("page dropped")
            return b"\x5a" * size

        uds = MagicMock()
        uds.read_memory_by_address.side_effect = rmba
        fm = FlashManager()
        fm.use_uds(uds)
        fm._connect = lambda *a, **k: None
        fm._authenticate = lambda *a, **k: None

        ram = fm.scan_ram()
        assert len(ram) == 192 * 0x100
        # page 1 cost 2 reads (1 drop + 1 retry); 191 more pages => 193 total.
        assert uds.read_memory_by_address.call_count == 193
        uds.flush.assert_called_once()

    def test_dropped_page_logs_summary_not_per_block_warning(self, caplog):
        # A recovered drop is routine on a lossy link, not a warning. It must
        # produce a single INFO summary, not per-block WARNING spam.
        import logging
        from src.ecu.exceptions import UDSTimeoutError

        state = {"n": 0}

        def rmba(addr, size, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise UDSTimeoutError("page dropped")
            return b"\x5a" * size

        uds = MagicMock()
        uds.read_memory_by_address.side_effect = rmba
        fm = FlashManager()
        fm.use_uds(uds)
        fm._connect = lambda *a, **k: None
        fm._authenticate = lambda *a, **k: None

        with caplog.at_level(logging.INFO):
            fm.scan_ram()

        # No WARNING+ noise about the per-block read for a recovered drop...
        read_warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "read" in r.getMessage().lower()
        ]
        assert read_warnings == []
        # ...just one INFO summary recording the recovery.
        assert "1 page(s) re-requested" in caplog.text


# --- Pure helpers in tools/wican_bench_ecu.py -------------------------------


class TestSummarizeRam:
    def test_all_zero_is_not_plausible(self):
        s = ecu_tool.summarize_ram(bytes(0x100))
        assert s["nonzero"] == 0
        assert s["distinct_values"] == 1
        assert s["looks_plausible"] is False

    def test_all_ff_is_not_plausible(self):
        s = ecu_tool.summarize_ram(b"\xff" * 0x100)
        assert s["nonzero"] == 0x100
        assert s["distinct_values"] == 1
        assert s["looks_plausible"] is False

    def test_mixed_memory_is_plausible(self):
        data = bytes(range(256)) * 4  # spread of values, some zero some not
        s = ecu_tool.summarize_ram(data)
        assert s["distinct_values"] == 256
        assert 0 < s["nonzero"] < len(data)
        assert s["looks_plausible"] is True

    def test_empty_is_not_plausible(self):
        s = ecu_tool.summarize_ram(b"")
        assert s["bytes"] == 0
        assert s["looks_plausible"] is False


class TestFormatDtcLines:
    def test_dedupes_and_formats(self):
        dtcs = [DTC(0x0301, 0x08), DTC(0x0301, 0x08), DTC(0x0420, 0x01)]
        lines = ecu_tool.format_dtc_lines(dtcs)
        assert len(lines) == 2
        assert "P0301" in lines[0]
        assert "P0420" in lines[1]

    def test_empty_list(self):
        assert ecu_tool.format_dtc_lines([]) == []


class TestClearSucceeded:
    def test_empty_after_is_success(self):
        assert ecu_tool.clear_succeeded([DTC(0x0301, 0x08)], []) is True

    def test_unchanged_is_failure(self):
        before = [DTC(0x0301, 0x08)]
        after = [DTC(0x0301, 0x08)]
        assert ecu_tool.clear_succeeded(before, after) is False

    def test_fewer_codes_is_success(self):
        before = [DTC(0x0301, 0x08), DTC(0x0420, 0x01)]
        after = [DTC(0x0301, 0x08)]
        assert ecu_tool.clear_succeeded(before, after) is True


# --- clear-dtc safety guard in the tool -------------------------------------


class TestClearGuard:
    """run_clear_dtc refuses to mutate ECU state without explicit confirmation."""

    def test_refuses_without_confirmation(self):
        uds = MagicMock()
        rc = ecu_tool.run_clear_dtc(uds, confirmed=False)
        assert rc == 2
        # Nothing was sent to the ECU — not even a Tester Present.
        uds.tester_present.assert_not_called()
