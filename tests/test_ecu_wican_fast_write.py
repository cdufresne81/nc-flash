"""Host parser for the firmware SD-staged flash stream (Option B Phase 6 host half).

Drives :meth:`WiCANTransport.fast_write` against a real socketpair replaying the
firmware's newline-delimited marker stream (``NCFWSYNC`` / ``NCFWPROG`` /
``NCFWDONE`` / ``FWERR``). No hardware, no ECU. Covers the happy path, resync past
leading CAN traffic, the FWERR abort, a stall, a peer close, and the command
framing — exactly the failure modes the live flash must surface cleanly.
"""

import socket

import pytest

from src.ecu.wican_transport import WiCANError, WiCANTransport


def _make_transport():
    a, b = socket.socketpair()
    t = WiCANTransport("127.0.0.1", 35000)
    t._sock = a
    t._drain_frames = lambda *args, **kwargs: None  # no buffered SLCAN frames in test
    t._send_raw = lambda *args, **kwargs: None
    return t, a, b


class TestFastWrite:
    def test_happy_path_with_progress(self):
        t, a, b = _make_transport()
        try:
            # Leading CAN junk before the sync marker must be discarded.
            b.sendall(
                b"t1F00\rNCFWSYNC\nNCFWPROG 16/1022\nNCFWPROG 1022/1022\nNCFWDONE\n"
            )
            prog = []
            t.fast_write(
                "ROM.bin", mode="L", progress_cb=lambda d, tot: prog.append((d, tot))
            )
            assert prog == [(16, 1022), (1022, 1022)]
        finally:
            a.close()
            b.close()

    def test_fwerr_raises(self):
        t, a, b = _make_transport()
        try:
            b.sendall(b"NCFWSYNC\n\r\nFWERR a=101400 st=11 nrc=E5\r\n")
            with pytest.raises(WiCANError, match="FWERR"):
                t.fast_write("ROM.bin")
        finally:
            a.close()
            b.close()

    def test_socket_closed_raises(self):
        t, a, b = _make_transport()
        try:
            b.sendall(b"NCFWSYNC\n")
            b.close()
            with pytest.raises(WiCANError, match="closed"):
                t.fast_write("ROM.bin")
        finally:
            a.close()

    def test_stall_raises(self):
        t, a, b = _make_transport()
        try:
            b.sendall(b"NCFWSYNC\n")  # sync then silence
            with pytest.raises(WiCANError, match="stalled"):
                t.fast_write("ROM.bin", idle_ms=300, timeout_ms=5000)
        finally:
            a.close()
            b.close()

    def test_bad_mode_rejected(self):
        t, a, b = _make_transport()
        try:
            with pytest.raises(WiCANError, match="mode"):
                t.fast_write("ROM.bin", mode="X")
        finally:
            a.close()
            b.close()

    @pytest.mark.parametrize(
        "mode,expected", [("L", b"WLROM_x.bin\r"), ("D", b"WDROM_x.bin\r")]
    )
    def test_command_framing(self, mode, expected):
        t, a, b = _make_transport()
        sent = []
        t._send_raw = lambda data: sent.append(data)
        try:
            b.sendall(b"NCFWSYNC\nNCFWDONE\n")
            t.fast_write("ROM_x.bin", mode=mode)
            assert sent == [expected]
        finally:
            a.close()
            b.close()

    def test_malformed_progress_ignored(self):
        t, a, b = _make_transport()
        try:
            b.sendall(b"NCFWSYNC\nNCFWPROG garbage\nNCFWPROG 5/10\nNCFWDONE\n")
            prog = []
            t.fast_write("ROM.bin", progress_cb=lambda d, tot: prog.append((d, tot)))
            assert prog == [(5, 10)]  # the garbage line was skipped, not fatal
        finally:
            a.close()
            b.close()
