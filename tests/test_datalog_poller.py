"""Host Datalogger engine against a scripted ECU (no hardware)."""

from collections import deque

import pytest

from src.core.datalog_channels import MODE_DID, MODE_MEM, MODE_OBD, Channel
from src.ecu.datalog_poller import (
    ECU_LOST_AFTER_S,
    DatalogEcuLost,
    DatalogNothingToPoll,
    DatalogPoller,
    format_row,
)
from src.ecu.protocol import UDSConnection
from src.ecu.transport import EcuTransport

PIDS = {0x0C: bytes([0x1B, 0x58]), 0x43: bytes([0, 0x40]), 0x05: bytes([0x5A])}
DIDS = {0x1746: bytes([0x0A]), 0x1410: bytes([0x01, 0x00])}


class FakeEcu(EcuTransport):
    """Answers OBD 0x01 (packed too), 0x22 and 0x23 like the NC ECU."""

    def __init__(self, pids=None, dids=None, memory=None):
        self.pids = dict(PIDS if pids is None else pids)
        self.dids = dict(DIDS if dids is None else dids)
        self.memory = dict(memory or {})
        self.refuse = {}  # request bytes -> NRC
        self.silent = False
        self.pack_garbage = False
        self.sent = []
        self.flushes = 0
        self._rx = deque()

    def open(self):
        pass

    def close(self):
        pass

    @property
    def description(self):
        return "fake"

    def flush(self):
        self.flushes += 1

    def inject(self, payload: bytes):
        """A late reply already waiting in the adapter's buffer."""
        self._rx.append(payload)

    def receive_message(self, timeout_ms):
        return self._rx.popleft() if self._rx else None

    def send_message(self, payload, timeout_ms):
        payload = bytes(payload)
        self.sent.append(payload)
        if self.silent:
            return
        if payload in self.refuse:
            self._rx.append(bytes([0x7F, payload[0], self.refuse[payload]]))
            return
        sid = payload[0]
        if sid == 0x01:
            out = bytearray([0x41])
            for pid in payload[1:]:
                if pid not in self.pids:
                    self._rx.append(bytes([0x7F, 0x01, 0x31]))
                    return
                out += bytes([pid]) + self.pids[pid]
            if self.pack_garbage and len(payload) > 2:
                out = out[:-1]
            self._rx.append(bytes(out))
        elif sid == 0x22:
            did = int.from_bytes(payload[1:3], "big")
            if did not in self.dids:
                self._rx.append(bytes([0x7F, 0x22, 0x31]))
                return
            self._rx.append(bytes([0x62]) + payload[1:3] + self.dids[did])
        elif sid == 0x23:
            addr = int.from_bytes(payload[1:5], "big")
            size = int.from_bytes(payload[5:7], "big")
            if addr not in self.memory:
                self._rx.append(bytes([0x7F, 0x23, 0x22]))  # stock ROM
                return
            self._rx.append(bytes([0x63]) + self.memory[addr][:size])


def _ch(name, mode, ident, size=1, **kw):
    return Channel(name, mode, ident, size=size, **kw)


RPM = _ch("RPM", MODE_OBD, 0x0C, 2, formula="x*0.25")
LOAD = _ch("LOAD", MODE_OBD, 0x43, 2, formula="x*0.0039215684")
ECT = _ch("ECT", MODE_OBD, 0x05, 1, formula="x-40", every=4)
KNOCKR = _ch("KNOCKR", MODE_DID, 0x1746, 1, formula="x*0.3521126761")
FUEL_PW = _ch("FUEL_PW", MODE_DID, 0x1410, 2, formula="x*0.008")


def _poller(ecu, channels, **kw):
    return DatalogPoller(UDSConnection(ecu), channels, **kw)


def test_packed_obd_request_and_values():
    ecu = FakeEcu()
    p = _poller(ecu, [RPM, LOAD, ECT, KNOCKR, FUEL_PW])
    res = p.sweep()
    assert ecu.sent[0] == bytes([0x01, 0x0C, 0x43, 0x05])  # one packed request
    assert ecu.sent[1:] == [bytes.fromhex("221746"), bytes.fromhex("221410")]
    assert res.requests == 3 and res.failed == 0
    rpm, load, ect, knock, pw = res.values
    assert rpm == 1750.0 and ect == 50.0
    assert load == pytest.approx(0.25, abs=0.01)
    assert knock == pytest.approx(3.52, abs=0.01)
    assert pw == pytest.approx(2.048)
    assert p.header == "Time,RPM,LOAD,ECT,KNOCKR,FUEL_PW"


def test_every_n_polls_less_often_and_repeats_the_last_value():
    ecu = FakeEcu()
    p = _poller(ecu, [RPM, ECT], pack=False)
    for _ in range(8):
        res = p.sweep()
        assert res.values[1] == 50.0  # ECT repeats between its polls
    ect_reads = [s for s in ecu.sent if s == bytes([0x01, 0x05])]
    assert len(ect_reads) == 2  # sweeps 0 and 4
    assert len([s for s in ecu.sent if s == bytes([0x01, 0x0C])]) == 8


def test_unsplittable_packed_reply_falls_back_to_single_pids():
    ecu = FakeEcu()
    ecu.pack_garbage = True
    p = _poller(ecu, [RPM, LOAD])
    res = p.sweep()
    assert res.values[0] == 1750.0
    assert p.packing is False
    ecu.sent.clear()
    p.sweep()
    assert ecu.sent == [bytes([0x01, 0x0C]), bytes([0x01, 0x43])]


def test_unknown_pid_in_a_pack_is_dropped_alone():
    ecu = FakeEcu()
    bogus = _ch("BOGUS", MODE_OBD, 0x5C)  # packable, but this ECU lacks it
    p = _poller(ecu, [RPM, bogus])
    res = p.sweep()
    assert res.values[0] == 1750.0
    assert "BOGUS" in p.unsupported and "RPM" not in p.unsupported
    ecu.sent.clear()
    p.sweep()
    assert bytes([0x01, 0x5C]) not in ecu.sent  # never asked again


def test_mode_23_on_a_stock_rom_explains_the_patch():
    ecu = FakeEcu()
    mem = _ch("VOLEFF", MODE_MEM, 0xFFFFAC18, 4, kind="float")
    p = _poller(ecu, [RPM, mem])
    res = p.sweep()
    assert res.values[1] is None
    assert "patched ROM" in p.unsupported["VOLEFF"]


def test_mode_23_on_a_patched_rom():
    ecu = FakeEcu(memory={0xFFFF7116: bytes([0x01, 0x02])})
    brake = _ch("BRAKE", MODE_MEM, 0xFFFF7116, 2)
    p = _poller(ecu, [brake])
    assert p.sweep().values == [258.0]
    assert ecu.sent[0] == bytes.fromhex("23FFFF71160002")


def test_late_reply_to_an_earlier_request_is_not_taken_for_the_next():
    ecu = FakeEcu()
    p = _poller(ecu, [KNOCKR, FUEL_PW])
    ecu.inject(bytes.fromhex("62141099FF"))  # stale FUEL_PW reply in the buffer
    res = p.sweep()
    assert res.values[0] == pytest.approx(3.52, abs=0.01)  # KNOCKR, not 0x99
    assert res.values[1] == pytest.approx(2.048)


def test_timeouts_leave_empty_cells_then_ecu_lost():
    ecu = FakeEcu()
    now = [0.0]
    p = _poller(ecu, [RPM], clock=lambda: now[0])
    assert p.sweep().values == [1750.0]
    ecu.silent = True
    res = p.sweep()
    assert res.values == [None] and res.failed == 1
    assert p.failures["RPM"] == 1
    assert ecu.flushes == 1  # a late reply is dropped before the next request
    now[0] += ECU_LOST_AFTER_S + 0.1
    with pytest.raises(DatalogEcuLost):
        p.sweep()


def test_every_channel_refused_stops_the_run():
    ecu = FakeEcu(pids={}, dids={})
    p = _poller(ecu, [_ch("A", MODE_DID, 0x1111)])
    p.sweep()
    with pytest.raises(DatalogNothingToPoll):
        p.sweep()


def test_nothing_selected():
    off = _ch("A", MODE_OBD, 0x0C, enabled=False)
    with pytest.raises(DatalogNothingToPoll):
        _poller(FakeEcu(), [off])


def test_only_read_services_are_ever_sent():
    ecu = FakeEcu(memory={0xFFFF0010: bytes(4)})
    chans = [RPM, LOAD, ECT, KNOCKR, FUEL_PW, _ch("M", MODE_MEM, 0xFFFF0010, 2)]
    p = _poller(ecu, chans)
    for _ in range(10):
        p.sweep()
    assert {s[0] for s in ecu.sent} <= {0x01, 0x22, 0x23}


def test_format_row():
    assert format_row(1.23456, [1750.0, None, 0.000123456]) == "1.235,1750,,0.000123456"
    # Raw 32-bit reads stay exact (no scientific notation).
    assert format_row(0, [4294967295.0, 1234567.5]) == "0.000,4294967295,1234567.5"


def test_sweeps_with_nothing_due_are_skipped():
    # Only slow channels: every sweep() must still ask the ECU something,
    # never return a duplicate row for free (that floods the CSV).
    ecu = FakeEcu()
    slow = [
        _ch("ECT", MODE_OBD, 0x05, every=8),
        _ch("KNOCKR", MODE_DID, 0x1746, every=8),
    ]
    p = _poller(ecu, slow)
    for _ in range(5):
        assert p.sweep().requests > 0


def test_flood_guard_after_the_only_fast_channel_is_dropped():
    ecu = FakeEcu()
    chans = [_ch("NOPE", MODE_DID, 0x9999), _ch("ECT", MODE_OBD, 0x05, every=8)]
    p = _poller(ecu, chans)
    for _ in range(5):
        assert p.sweep().requests > 0
    assert "NOPE" in p.unsupported


def test_late_refusal_of_another_service_never_drops_a_channel():
    ecu = FakeEcu()
    p = _poller(ecu, [RPM, LOAD])
    ecu.inject(bytes([0x7F, 0x23, 0x31]))  # left over from an earlier 0x23 read
    res = p.sweep()
    assert p.unsupported == {}
    assert p.packing is True  # a stale refusal must not turn packing off
    assert res.failed == 1
    assert p.sweep().values[0] == 1750.0
