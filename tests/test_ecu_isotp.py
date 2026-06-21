"""
Tests for src/ecu/isotp.py — the pure-Python ISO-TP (ISO 15765-2) engine.

ISO-TP segmentation/reassembly sits directly under UDS on the wire, so a
framing bug here would corrupt every multi-frame ECU exchange (download blocks,
seeds, ROM IDs) and could brick hardware. These tests pin every frame the
engine emits and every malformed/edge frame it must reject, using an in-memory
two-queue CAN bus that wires a sender session to a receiver session.
"""

import threading
import time
from collections import deque
from typing import Optional, Tuple

import pytest

from src.ecu.isotp import (
    IsoTpError,
    IsoTpSession,
    IsoTpTimeout,
    decode_stmin,
    FC_CONTINUE_TO_SEND,
    FC_WAIT,
    FC_OVERFLOW,
    PCI_FLOW_CONTROL,
)

TX = 0x7E0
RX = 0x7E8


# ---------------------------------------------------------------------------
# Fakes: scripted single-direction frame list, and a bidirectional bus.
# ---------------------------------------------------------------------------


class FrameRecorder:
    """Records frames the session sends and serves scripted recv frames.

    ``sent`` is a list of ``(can_id, data)`` tuples (data is the full padded
    8-byte frame). ``script`` is a deque of ``(can_id, data)`` or ``None``
    (timeout) entries returned in order from ``recv_frame``.
    """

    def __init__(self, script=None):
        self.sent: list[Tuple[int, bytes]] = []
        self.script = deque(script or [])

    def send_frame(self, can_id: int, data: bytes) -> None:
        self.sent.append((can_id, bytes(data)))

    def recv_frame(self, timeout_ms: int) -> Optional[Tuple[int, bytes]]:
        if not self.script:
            return None
        item = self.script.popleft()
        if item is None:
            return None
        can_id, data = item
        return (can_id, bytes(data))


class FrameBus:
    """In-memory CAN bus connecting two endpoints by queue.

    Each endpoint reads from its own inbound queue and writes to the peer's.
    ``endpoint(rx_id)`` returns ``(send_frame, recv_frame)`` callables. Frames
    are delivered to *every* endpoint queue (broadcast), and the receiving
    session is responsible for filtering by ``rx_id`` — this lets us verify
    rx_id filtering with real cross-talk.
    """

    def __init__(self):
        self._queues: dict[int, deque] = {}
        self._lock = threading.Lock()

    def _queue_for(self, key: int) -> deque:
        if key not in self._queues:
            self._queues[key] = deque()
        return self._queues[key]

    def endpoint(self, key: int):
        # Ensure this endpoint's queue exists.
        with self._lock:
            self._queue_for(key)

        def send_frame(can_id: int, data: bytes) -> None:
            with self._lock:
                # Broadcast to all other endpoints.
                for other_key, q in self._queues.items():
                    if other_key != key:
                        q.append((can_id, bytes(data)))

        def recv_frame(timeout_ms: int) -> Optional[Tuple[int, bytes]]:
            deadline = time.monotonic() + timeout_ms / 1000.0
            while True:
                with self._lock:
                    q = self._queue_for(key)
                    if q:
                        return q.popleft()
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.001)

        return send_frame, recv_frame


def make_session(recorder: FrameRecorder, **kwargs) -> IsoTpSession:
    return IsoTpSession(
        tx_id=TX,
        rx_id=RX,
        send_frame=recorder.send_frame,
        recv_frame=recorder.recv_frame,
        **kwargs,
    )


def fc_frame(status=FC_CONTINUE_TO_SEND, bs=0, stmin=0, can_id=RX):
    """Build a Flow Control frame tuple for scripting recv."""
    pci = (PCI_FLOW_CONTROL << 4) | (status & 0x0F)
    return (can_id, bytes([pci, bs, stmin, 0, 0, 0, 0, 0]))


# ---------------------------------------------------------------------------
# STmin decoding
# ---------------------------------------------------------------------------


class TestDecodeStmin:
    def test_milliseconds(self):
        assert decode_stmin(0x00) == 0.0
        assert decode_stmin(0x0A) == pytest.approx(0.010)
        assert decode_stmin(0x7F) == pytest.approx(0.127)

    def test_microseconds(self):
        assert decode_stmin(0xF1) == pytest.approx(0.0001)
        assert decode_stmin(0xF9) == pytest.approx(0.0009)

    def test_reserved_treated_as_max(self):
        # 0x80..0xF0 and 0xFA..0xFF are reserved -> max ms value.
        assert decode_stmin(0x80) == pytest.approx(0.127)
        assert decode_stmin(0xFF) == pytest.approx(0.127)


# ---------------------------------------------------------------------------
# Single Frame TX
# ---------------------------------------------------------------------------


class TestSingleFrameSend:
    def test_short_payload_emits_single_frame_padded(self):
        rec = FrameRecorder()
        sess = make_session(rec)

        sess.send(b"\x10\x02", timeout_ms=100)

        assert len(rec.sent) == 1
        can_id, data = rec.sent[0]
        assert can_id == TX
        # PCI 0x02 (SF, len 2) + payload, padded to 8 with 0x00.
        assert data == bytes([0x02, 0x10, 0x02, 0, 0, 0, 0, 0])

    def test_seven_byte_payload_is_single_frame(self):
        rec = FrameRecorder()
        sess = make_session(rec)
        payload = bytes(range(7))

        sess.send(payload, timeout_ms=100)

        assert len(rec.sent) == 1
        _can_id, data = rec.sent[0]
        assert data[0] == 0x07
        assert data[1:8] == payload

    def test_custom_padding_byte(self):
        rec = FrameRecorder()
        sess = make_session(rec, padding=0xAA)

        sess.send(b"\x3e", timeout_ms=100)

        _can_id, data = rec.sent[0]
        assert data == bytes([0x01, 0x3E, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA])


# ---------------------------------------------------------------------------
# Single Frame RX
# ---------------------------------------------------------------------------


class TestSingleFrameReceive:
    def test_receive_single_frame(self):
        rec = FrameRecorder(script=[(RX, bytes([0x03, 0x50, 0x02, 0x01, 0, 0, 0, 0]))])
        sess = make_session(rec)

        assert sess.receive(timeout_ms=100) == b"\x50\x02\x01"

    def test_zero_length_single_frame_raises(self):
        rec = FrameRecorder(script=[(RX, bytes([0x00, 0, 0, 0, 0, 0, 0, 0]))])
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="zero length"):
            sess.receive(timeout_ms=100)

    def test_truncated_single_frame_raises(self):
        # Declares 5 bytes but frame only has 3 data bytes present.
        rec = FrameRecorder(script=[(RX, bytes([0x05, 0x01, 0x02, 0x03]))])
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="shorter than declared"):
            sess.receive(timeout_ms=100)


# ---------------------------------------------------------------------------
# Round-trip over a real two-session bus
# ---------------------------------------------------------------------------


def _bus_round_trip(payload: bytes, **rx_kwargs) -> bytes:
    """Send ``payload`` from a TX session, receive it on an RX session.

    Sender transmits on TX (0x7E0) and listens on RX (0x7E8); receiver mirrors.
    Runs the receiver in a thread so FF->FC->CF handshakes interleave.
    """
    bus = FrameBus()
    tx_send, tx_recv = bus.endpoint(key=1)
    rx_send, rx_recv = bus.endpoint(key=2)

    sender = IsoTpSession(tx_id=TX, rx_id=RX, send_frame=tx_send, recv_frame=tx_recv)
    receiver = IsoTpSession(
        tx_id=RX, rx_id=TX, send_frame=rx_send, recv_frame=rx_recv, **rx_kwargs
    )

    result_box: list = []
    err_box: list = []

    def rx_thread():
        try:
            result_box.append(receiver.receive(timeout_ms=2000))
        except Exception as exc:  # noqa: BLE001 — surfaced via err_box
            err_box.append(exc)

    t = threading.Thread(target=rx_thread)
    t.start()
    sender.send(payload, timeout_ms=2000)
    t.join(timeout=5)

    if err_box:
        raise err_box[0]
    assert result_box, "receiver produced no result"
    return result_box[0]


class TestBusRoundTrip:
    def test_single_frame_round_trip(self):
        payload = b"\x67\x01\x02\x03"
        assert _bus_round_trip(payload) == payload

    def test_multi_frame_round_trip(self):
        payload = bytes((i * 7) & 0xFF for i in range(20))  # 20 bytes -> FF + CFs
        assert _bus_round_trip(payload) == payload

    def test_multi_frame_round_trip_with_block_size(self):
        # Receiver advertises BS=2 -> sender must wait for FC every 2 CFs.
        payload = bytes(range(50))
        assert _bus_round_trip(payload, rx_block_size=2) == payload

    def test_multi_frame_round_trip_seq_wrap_past_15(self):
        # >15 consecutive frames forces sequence number wrap 15 -> 0.
        # FF holds 6 bytes; each CF holds 7. 6 + 16*7 = 118 bytes needs 17 CFs.
        payload = bytes((i * 3) & 0xFF for i in range(118))
        assert _bus_round_trip(payload) == payload

    def test_multi_frame_round_trip_with_stmin(self):
        payload = bytes(range(30))
        # STmin 0x01 = 1ms; round trip still succeeds.
        assert _bus_round_trip(payload, rx_stmin=0x01) == payload

    def test_1k_block_under_max_block_size(self):
        # 1KB (0x400) TransferData/ROM block under BS=0x0F (max block size):
        # pins the multi-block FC-resume path against an over-strict short-CF
        # check regressing. Varied pattern catches byte-misalignment.
        payload = bytes((i * 7) & 0xFF for i in range(0x400))
        assert _bus_round_trip(payload, rx_block_size=0x0F) == payload

    def test_max_isotp_payload_round_trip(self):
        # 0xFFF (4095) bytes = the 12-bit First Frame length maximum. The final
        # CF is legitimately short and must still be accepted.
        payload = bytes((i * 13) & 0xFF for i in range(0xFFF))
        assert _bus_round_trip(payload) == payload
        # And again under max block size, exercising both edges together.
        assert _bus_round_trip(payload, rx_block_size=0x0F) == payload


# ---------------------------------------------------------------------------
# Multi-frame TX with scripted Flow Control
# ---------------------------------------------------------------------------


class TestMultiFrameSendScripted:
    def test_ff_then_cts_then_cfs(self):
        # Script a single CTS so the whole payload flows in one block.
        rec = FrameRecorder(script=[fc_frame(FC_CONTINUE_TO_SEND, bs=0, stmin=0)])
        sess = make_session(rec)
        payload = bytes(range(20))  # 6 in FF + 7 + 7 = 3 frames, 2 CFs

        sess.send(payload, timeout_ms=500)

        # Frame 0: First Frame. 0x1 + 12-bit length (20 = 0x014).
        ff = rec.sent[0][1]
        assert ff[0] == 0x10  # high nibble 1, length high nibble 0
        assert ff[1] == 0x14  # length low byte = 20
        assert ff[2:8] == payload[:6]

        # Frame 1: CF seq 1.
        cf1 = rec.sent[1][1]
        assert cf1[0] == 0x21
        assert cf1[1:8] == payload[6:13]

        # Frame 2: CF seq 2 (last, exactly 7 bytes -> fills the frame, no pad).
        cf2 = rec.sent[2][1]
        assert cf2[0] == 0x22
        assert cf2[1:8] == payload[13:20]
        assert len(rec.sent) == 3

    def test_block_size_requires_multiple_fcs(self):
        # BS=2: after 2 CFs the sender must wait for another FC.
        rec = FrameRecorder(
            script=[
                fc_frame(FC_CONTINUE_TO_SEND, bs=2, stmin=0),
                fc_frame(FC_CONTINUE_TO_SEND, bs=2, stmin=0),
            ]
        )
        sess = make_session(rec)
        # 6 (FF) + 7*4 = 34 bytes -> 4 CFs, so a 2nd FC is needed after 2 CFs.
        payload = bytes(range(34))

        sess.send(payload, timeout_ms=500)

        # 1 FF + 4 CFs = 5 frames sent.
        assert len(rec.sent) == 5
        seqs = [rec.sent[i][1][0] for i in range(1, 5)]
        assert seqs == [0x21, 0x22, 0x23, 0x24]
        # Both scripted FCs must have been consumed.
        assert len(rec.script) == 0

    def test_wait_then_cts(self):
        # A WAIT frame must not abort: the sender waits for the following CTS.
        rec = FrameRecorder(
            script=[
                fc_frame(FC_WAIT),
                fc_frame(FC_WAIT),
                fc_frame(FC_CONTINUE_TO_SEND, bs=0, stmin=0),
            ]
        )
        sess = make_session(rec)
        payload = bytes(range(15))  # FF + 2 CFs

        sess.send(payload, timeout_ms=500)

        assert rec.sent[0][1][0] == 0x10  # FF
        assert rec.sent[1][1][0] == 0x21  # CF1
        assert rec.sent[2][1][0] == 0x22  # CF2
        assert len(rec.script) == 0  # both WAITs + CTS consumed

    def test_overflow_raises(self):
        rec = FrameRecorder(script=[fc_frame(FC_OVERFLOW)])
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="overflow"):
            sess.send(bytes(range(20)), timeout_ms=500)

    def test_non_fc_frame_after_ff_raises(self):
        # Peer sends a Single Frame where Flow Control was expected.
        rec = FrameRecorder(script=[(RX, bytes([0x03, 0x50, 0x02, 0x01, 0, 0, 0, 0]))])
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="expected Flow Control"):
            sess.send(bytes(range(20)), timeout_ms=500)

    def test_too_many_waits_raises(self):
        rec = FrameRecorder(script=[fc_frame(FC_WAIT) for _ in range(5)])
        sess = make_session(rec, max_wait_frames=3)

        with pytest.raises(IsoTpError, match="too many"):
            sess.send(bytes(range(20)), timeout_ms=500)

    def test_payload_too_large_raises(self):
        rec = FrameRecorder()
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="too large"):
            sess.send(bytes(0x1000), timeout_ms=100)


# ---------------------------------------------------------------------------
# Multi-frame RX with scripted Consecutive Frames
# ---------------------------------------------------------------------------


class TestMultiFrameReceiveScripted:
    def test_ff_sends_fc_and_reassembles(self):
        # Declared length 15: FF(6) + CF1(7) + CF2(2).
        payload = bytes(range(15))
        script = [
            (RX, bytes([0x10, 0x0F]) + payload[:6]),
            (RX, bytes([0x21]) + payload[6:13]),
            (RX, bytes([0x22]) + payload[13:15] + b"\x00" * 5),
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec, rx_block_size=0, rx_stmin=0)

        result = sess.receive(timeout_ms=500)

        assert result == payload
        # Exactly one Flow Control CTS was emitted with our BS/STmin.
        assert len(rec.sent) == 1
        can_id, fc = rec.sent[0]
        assert can_id == TX  # our tx_id
        assert fc[0] == 0x30  # FC + CTS
        assert fc[1] == 0x00  # BS
        assert fc[2] == 0x00  # STmin

    def test_fc_advertises_configured_bs_stmin(self):
        payload = bytes(range(10))
        script = [
            (RX, bytes([0x10, 0x0A]) + payload[:6]),
            (RX, bytes([0x21]) + payload[6:10] + b"\x00" * 3),
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec, rx_block_size=8, rx_stmin=0x0A)

        sess.receive(timeout_ms=500)

        fc = rec.sent[0][1]
        assert fc[1] == 0x08  # BS
        assert fc[2] == 0x0A  # STmin

    def test_block_size_emits_fresh_fc_each_block(self):
        # Receiver advertises BS=2 -> must send a new CTS after every 2 CFs so
        # a BS-honouring sender resumes. 6 (FF) + 7*4 = 34 -> 4 CFs, 2 blocks,
        # so the final completing CF must NOT trigger a redundant trailing FC.
        length = 30
        payload = bytes((i * 9) & 0xFF for i in range(length))
        script = [(RX, bytes([0x10, length]) + payload[:6])]
        offset = 6
        seq = 1
        while offset < length:
            chunk = payload[offset : offset + 7]
            frame = bytes([0x20 | (seq & 0x0F)]) + chunk
            frame = frame + b"\x00" * (8 - len(frame))
            script.append((RX, frame))
            offset += len(chunk)
            seq = (seq + 1) & 0x0F
        rec = FrameRecorder(script=script)
        sess = make_session(rec, rx_block_size=2)

        assert sess.receive(timeout_ms=500) == payload

        # 4 CFs deliver the message. Initial FC + one mid-stream FC after the
        # 2nd CF = 2 flow-control frames; the block boundary that coincides with
        # completion must not emit a trailing FC.
        fcs = [s for s in rec.sent if s[1][0] == 0x30]
        assert len(fcs) == 2

    def test_sequence_wrap_past_15(self):
        # 6 + 17*7 = 125 declared; use 120 so last CF is partial. 17 CFs -> wrap.
        length = 120
        payload = bytes((i * 5) & 0xFF for i in range(length))
        script = [(RX, bytes([0x10, length]) + payload[:6])]
        offset = 6
        seq = 1
        while offset < length:
            chunk = payload[offset : offset + 7]
            frame = bytes([0x20 | (seq & 0x0F)]) + chunk
            frame = frame + b"\x00" * (8 - len(frame))
            script.append((RX, frame))
            offset += len(chunk)
            seq = (seq + 1) & 0x0F
        rec = FrameRecorder(script=script)
        sess = make_session(rec)

        assert sess.receive(timeout_ms=1000) == payload

    def test_sequence_gap_raises(self):
        payload = bytes(range(20))
        script = [
            (RX, bytes([0x10, 0x14]) + payload[:6]),
            (RX, bytes([0x21]) + payload[6:13]),
            # Expected seq 2, but send seq 3 -> gap.
            (RX, bytes([0x23]) + payload[13:20]),
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="sequence gap"):
            sess.receive(timeout_ms=500)

    def test_non_cf_during_reassembly_raises(self):
        payload = bytes(range(20))
        script = [
            (RX, bytes([0x10, 0x14]) + payload[:6]),
            # A Single Frame where a Consecutive Frame was expected.
            (RX, bytes([0x03, 0x01, 0x02, 0x03, 0, 0, 0, 0])),
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="expected Consecutive Frame"):
            sess.receive(timeout_ms=500)

    def test_unexpected_pci_at_message_start_raises(self):
        # Start with a Consecutive Frame (no FF/SF) -> invalid.
        rec = FrameRecorder(script=[(RX, bytes([0x21, 1, 2, 3, 4, 5, 6, 7]))])
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="unexpected ISO-TP PCI"):
            sess.receive(timeout_ms=100)


# ---------------------------------------------------------------------------
# rx_id filtering
# ---------------------------------------------------------------------------


class TestRxIdFiltering:
    def test_foreign_can_id_ignored_on_receive(self):
        # First frame is on a different CAN id and must be skipped.
        rec = FrameRecorder(
            script=[
                (0x123, bytes([0x03, 0xFF, 0xFF, 0xFF, 0, 0, 0, 0])),
                (RX, bytes([0x03, 0x50, 0x02, 0x01, 0, 0, 0, 0])),
            ]
        )
        sess = make_session(rec)

        assert sess.receive(timeout_ms=200) == b"\x50\x02\x01"

    def test_foreign_can_id_ignored_while_awaiting_fc(self):
        rec = FrameRecorder(
            script=[
                (0x456, bytes([0x30, 0, 0, 0, 0, 0, 0, 0])),  # FC on wrong id
                fc_frame(FC_CONTINUE_TO_SEND),
            ]
        )
        sess = make_session(rec)

        sess.send(bytes(range(15)), timeout_ms=500)

        assert rec.sent[0][1][0] == 0x10  # FF emitted
        assert len(rec.script) == 0


# ---------------------------------------------------------------------------
# Timeouts and configuration
# ---------------------------------------------------------------------------


class TestTimeoutsAndConfig:
    def test_receive_timeout_raises(self):
        rec = FrameRecorder(script=[])  # nothing ever arrives
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="timeout"):
            sess.receive(timeout_ms=10)

    def test_send_waiting_for_fc_times_out(self):
        rec = FrameRecorder(script=[])  # no FC ever arrives
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="timeout"):
            sess.send(bytes(range(20)), timeout_ms=10)

    def test_requires_transport_or_callables(self):
        with pytest.raises(ValueError, match="requires either"):
            IsoTpSession(tx_id=TX, rx_id=RX)

    def test_transport_object_accepted(self):
        rec = FrameRecorder(script=[(RX, bytes([0x03, 0x50, 0x02, 0x01, 0, 0, 0, 0]))])
        sess = IsoTpSession(tx_id=TX, rx_id=RX, transport=rec)

        assert sess.receive(timeout_ms=100) == b"\x50\x02\x01"

    def test_padding_out_of_range_raises(self):
        rec = FrameRecorder()
        with pytest.raises(ValueError, match="padding"):
            IsoTpSession(
                tx_id=TX,
                rx_id=RX,
                send_frame=rec.send_frame,
                recv_frame=rec.recv_frame,
                padding=0x100,
            )

    def test_honor_peer_fc_false_skips_stmin_sleep(self):
        # With honor_peer_fc=False, a large STmin in the FC is ignored.
        rec = FrameRecorder(
            script=[fc_frame(FC_CONTINUE_TO_SEND, bs=0, stmin=0x64)]  # 100ms
        )
        sess = make_session(rec, honor_peer_fc=False)
        payload = bytes(range(20))

        start = time.monotonic()
        sess.send(payload, timeout_ms=500)
        elapsed = time.monotonic() - start

        # Two CFs at 100ms STmin would take >=100ms if honoured; assert it didn't.
        assert elapsed < 0.05
        assert len(rec.sent) == 3


# ---------------------------------------------------------------------------
# Short-frame rejection (silent-corruption guards)
# ---------------------------------------------------------------------------


class TestShortFrameRejection:
    """A multi-frame reassembly must never silently under-fill from a short
    frame; a short FF or short non-final CF byte-misaligns the payload while
    still possibly passing the final length check — corruption on the flash
    path. These pin that every such frame raises instead."""

    def test_short_non_final_consecutive_frame_raises(self):
        # Declared length 20: FF(6) + CF1 must carry a full 7 bytes, but CF1 is
        # truncated to 4 data bytes. It is NOT the final CF (6+4 < 20), so the
        # engine must reject it rather than under-fill the buffer.
        payload = bytes(range(20))
        script = [
            (RX, bytes([0x10, 0x14]) + payload[:6]),
            # CF seq 1 with only 4 data bytes (frame is 5 bytes, not 8).
            (RX, bytes([0x21]) + payload[6:10]),
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="Consecutive Frame shorter than"):
            sess.receive(timeout_ms=500)

    def test_short_first_frame_raises(self):
        # FF declares a multi-frame length (20) but the frame itself carries
        # fewer than the mandatory 6 payload bytes (only 3 here).
        rec = FrameRecorder(script=[(RX, bytes([0x10, 0x14, 0x00, 0x01, 0x02]))])
        sess = make_session(rec)

        with pytest.raises(IsoTpError, match="First Frame shorter than 8 bytes"):
            sess.receive(timeout_ms=500)

    def test_final_consecutive_frame_may_be_short(self):
        # The LAST CF is allowed to be short (it only needs to carry the
        # remaining bytes). 15 bytes: FF(6) + CF1(7) + CF2(2) where CF2 carries
        # exactly the 2 trailing bytes — this must succeed, not raise.
        payload = bytes(range(15))
        script = [
            (RX, bytes([0x10, 0x0F]) + payload[:6]),
            (RX, bytes([0x21]) + payload[6:13]),
            (RX, bytes([0x22]) + payload[13:15]),  # only 2 data bytes, no pad
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec)

        assert sess.receive(timeout_ms=500) == payload


# ---------------------------------------------------------------------------
# _pad over-length rejection
# ---------------------------------------------------------------------------


class TestPadRejectsOverLength:
    def test_pad_passthrough_exactly_eight_bytes(self):
        rec = FrameRecorder()
        sess = make_session(rec)
        full = bytes(range(8))
        assert sess._pad(full) == full

    def test_pad_over_length_raises(self):
        rec = FrameRecorder()
        sess = make_session(rec)
        with pytest.raises(IsoTpError, match="too long for padding"):
            sess._pad(bytes(range(9)))


# ---------------------------------------------------------------------------
# Timeout vs. protocol-violation exception typing
# ---------------------------------------------------------------------------


class TestTimeoutExceptionType:
    """A deadline timeout must raise the IsoTpTimeout subclass so callers can
    treat it as 'no answer' and retry, while a protocol violation (sequence
    gap, short frame, overflow) must stay a plain IsoTpError so it surfaces
    loudly and is never silently retried."""

    def test_receive_deadline_raises_isotp_timeout(self):
        rec = FrameRecorder(script=[])  # nothing ever arrives
        sess = make_session(rec)

        with pytest.raises(IsoTpTimeout):
            sess.receive(timeout_ms=10)

    def test_send_fc_deadline_raises_isotp_timeout(self):
        rec = FrameRecorder(script=[])  # no Flow Control ever arrives
        sess = make_session(rec)

        with pytest.raises(IsoTpTimeout):
            sess.send(bytes(range(20)), timeout_ms=10)

    def test_sequence_gap_is_plain_isotp_error_not_timeout(self):
        payload = bytes(range(20))
        script = [
            (RX, bytes([0x10, 0x14]) + payload[:6]),
            (RX, bytes([0x21]) + payload[6:13]),
            (RX, bytes([0x23]) + payload[13:20]),  # expected seq 2, got 3 -> gap
        ]
        rec = FrameRecorder(script=script)
        sess = make_session(rec)

        with pytest.raises(IsoTpError) as excinfo:
            sess.receive(timeout_ms=500)
        # Must be a protocol violation, NOT a timeout subclass.
        assert "sequence gap" in str(excinfo.value)
        assert not isinstance(excinfo.value, IsoTpTimeout)

    def test_overflow_is_plain_isotp_error_not_timeout(self):
        rec = FrameRecorder(script=[fc_frame(FC_OVERFLOW)])
        sess = make_session(rec)

        with pytest.raises(IsoTpError) as excinfo:
            sess.send(bytes(range(20)), timeout_ms=500)
        assert not isinstance(excinfo.value, IsoTpTimeout)

    def test_isotp_timeout_is_isotp_error_subclass(self):
        assert issubclass(IsoTpTimeout, IsoTpError)


class TestNCrFastFail:
    """N_Cr bounds the wait for the NEXT consecutive frame mid-message.

    On a lossy link a dropped CF should be detected within N_Cr and surfaced as
    a definitive IsoTpError (so the transport seam re-raises and the idempotent
    read retries), not stall the whole receive budget and not be mistaken for a
    benign IsoTpTimeout. None (default) preserves the historical behaviour.
    """

    # First Frame declaring a 20-byte message (carries the first 6 payload bytes).
    _PAYLOAD = bytes(range(20))
    _FF = (RX, bytes([0x10, 0x14]) + _PAYLOAD[:6])
    _CF1 = (RX, bytes([0x21]) + _PAYLOAD[6:13])
    _CF2 = (RX, bytes([0x22]) + _PAYLOAD[13:20])

    def test_gap_after_first_frame_fails_fast_as_error_not_timeout(self):
        # FF arrives, then no CF ever follows -> an N_Cr gap.
        rec = FrameRecorder([self._FF])
        sess = make_session(rec, n_cr_ms=30)

        start = time.monotonic()
        with pytest.raises(IsoTpError) as excinfo:
            # Generous overall budget: the failure must come from N_Cr (~30 ms),
            # not the overall deadline.
            sess.receive(timeout_ms=5000)
        elapsed = time.monotonic() - start

        assert not isinstance(excinfo.value, IsoTpTimeout)
        assert "N_Cr" in str(excinfo.value)
        assert elapsed < 2.0  # nowhere near the 5 s overall budget

    def test_without_n_cr_a_gap_waits_the_full_overall_deadline(self):
        # Same scenario, N_Cr disabled: the historical behaviour is a benign
        # IsoTpTimeout once the OVERALL deadline expires.
        rec = FrameRecorder([self._FF])
        sess = make_session(rec)  # n_cr_ms defaults to None

        with pytest.raises(IsoTpTimeout):
            sess.receive(timeout_ms=80)

    def test_prompt_consecutive_frames_do_not_trip_n_cr(self):
        # Frames arrive back-to-back -> N_Cr never fires; full message returned.
        rec = FrameRecorder([self._FF, self._CF1, self._CF2])
        sess = make_session(rec, n_cr_ms=30)

        assert sess.receive(timeout_ms=5000) == self._PAYLOAD

    def test_n_cr_does_not_bound_the_initial_frame_wait(self):
        # No First Frame ever arrives. The wait for the FIRST frame uses the
        # overall deadline (not N_Cr), so this is a benign timeout, not a gap.
        rec = FrameRecorder([])
        sess = make_session(rec, n_cr_ms=30)

        with pytest.raises(IsoTpTimeout):
            sess.receive(timeout_ms=80)
