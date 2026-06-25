"""
Tests for src/ecu/wican_transport.py — the WiCAN (SLCAN-over-TCP) transport.

This transport is the full WiCAN wire stack: UDS payload -> ISO-TP framing ->
SLCAN ASCII -> TCP. A framing or buffering bug here corrupts every WiCAN ECU
exchange (download blocks, seeds, ROM IDs) and could brick hardware, so these
tests stand up a real in-process SLCAN TCP server on loopback and exercise
full ``open``/``send_message``/``receive_message``/``close`` round trips,
including the multi-frame paths that force ISO-TP Flow Control in BOTH
directions, plus the timeout and error contracts.

Everything runs headless over 127.0.0.1 loopback — no hardware, no PySide6.
"""

import socket
import socketserver
import threading
import time
from typing import List, Optional, Tuple

import pytest

from src.ecu.constants import CAN_REQUEST_ID, CAN_RESPONSE_ID
from src.ecu.exceptions import ECUError
from src.ecu.slcan import (
    BEL,
    CR,
    SlcanFrameStream,
    decode_frame,
    encode_data_frame,
)
from src.ecu.wican_transport import (
    _FAST_READ_CHUNK,
    _FAST_READ_PING_ADDR,
    _FAST_READ_SYNC,
    WiCANError,
    WiCANTransport,
)

TESTER_ID = CAN_REQUEST_ID  # 0x7E0 — frames from us
ECU_ID = CAN_RESPONSE_ID  # 0x7E8 — frames from the mock ECU


# ---------------------------------------------------------------------------
# Mock SLCAN server: speaks the ECU side of ISO-TP over a real TCP socket.
# ---------------------------------------------------------------------------


class _EcuIsoTp:
    """Minimal ECU-side ISO-TP responder over an SLCAN socket connection.

    Reassembles a single inbound UDS request (single OR multi-frame, honouring
    Flow Control as the receiver) then transmits one scripted UDS response
    (single OR multi-frame, honouring the tester's Flow Control as the sender).

    It speaks raw SLCAN on the supplied socket via :class:`SlcanFrameStream`
    for decode and :func:`encode_data_frame` for encode.
    """

    def __init__(
        self,
        conn: socket.socket,
        response_payload: bytes,
        rx_block_size: int = 0,
        rx_stmin: int = 0,
        silent: bool = False,
    ):
        self._conn = conn
        self._response = response_payload
        self._rx_block_size = rx_block_size
        self._rx_stmin = rx_stmin
        self._silent = silent
        self._stream = SlcanFrameStream()
        self._pending: List[Tuple[int, bytes]] = []

    # -- low-level frame I/O --

    def _send_frame(self, can_id: int, data: bytes) -> None:
        self._conn.sendall(encode_data_frame(can_id, bytes(data)))

    def _recv_frame(self, timeout_s: float) -> Optional[Tuple[int, bytes]]:
        if self._pending:
            return self._pending.pop(0)
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._conn.settimeout(remaining)
            try:
                chunk = self._conn.recv(4096)
            except socket.timeout:
                return None
            if chunk == b"":
                return None
            for frame in self._stream.feed(chunk):
                self._pending.append(frame)
            if self._pending:
                return self._pending.pop(0)

    def _recv_tester_frame(self, timeout_s: float) -> Optional[Tuple[int, bytes]]:
        """Receive the next frame addressed FROM the tester (TESTER_ID)."""
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            frame = self._recv_frame(remaining)
            if frame is None:
                return None
            if frame[0] == TESTER_ID:
                return frame

    # -- handshake --

    def handle_handshake(self) -> None:
        """Drain SLCAN control commands (C/S6/O) and ack each with a CR."""
        # The client sends C, S6, O each terminated by CR. We don't need to
        # parse them precisely; we just ack every CR-terminated command line
        # that is a control command (not a 't'/'T' data frame).
        buf = bytearray()
        # Expect up to 3 control commands; stop once we've acked them or the
        # first data frame appears.
        acked = 0
        deadline = time.monotonic() + 2.0
        while acked < 3 and time.monotonic() < deadline:
            self._conn.settimeout(0.5)
            try:
                chunk = self._conn.recv(4096)
            except socket.timeout:
                break
            if chunk == b"":
                break
            buf.extend(chunk)
            while b"\r" in buf:
                idx = buf.index(b"\r")
                line = bytes(buf[: idx + 1])
                del buf[: idx + 1]
                if line[:1] in (b"t", b"T"):
                    # A data frame slipped in — push it to the stream parser.
                    for frame in self._stream.feed(line):
                        self._pending.append(frame)
                else:
                    # Control command: ack with a bare CR.
                    self._conn.sendall(CR)
                    acked += 1
        # Any leftover (a partial data frame) goes to the stream parser.
        if buf:
            for frame in self._stream.feed(bytes(buf)):
                self._pending.append(frame)

    # -- ISO-TP receive (ECU as receiver) --

    def receive_request(self, timeout_s: float = 3.0) -> bytes:
        # Skip the transport's warm-up frame (TesterPresent + suppress-positive-
        # response, 3E 80). A real ECU consumes it silently; the mock does too,
        # so the priming frame is never mistaken for the request under test.
        while True:
            frame = self._recv_tester_frame(timeout_s)
            if frame is None:
                raise AssertionError("mock ECU: no request frame from tester")
            _cid, data = frame
            if len(data) >= 3 and data[0] == 0x02 and data[1:3] == b"\x3e\x80":
                continue
            break
        pci_type = (data[0] >> 4) & 0x0F
        if pci_type == 0x0:  # Single Frame
            length = data[0] & 0x0F
            return data[1 : 1 + length]
        if pci_type == 0x1:  # First Frame -> multi-frame reassembly
            return self._receive_multi(data, timeout_s)
        raise AssertionError(f"mock ECU: unexpected PCI 0x{pci_type:X}")

    def _receive_multi(self, first: bytes, timeout_s: float) -> bytes:
        length = ((first[0] & 0x0F) << 8) | first[1]
        buf = bytearray(first[2:8])
        # Send Flow Control CTS with our advertised BS/STmin.
        self._send_fc(self._rx_block_size, self._rx_stmin)
        expected_seq = 1
        frames_in_block = 0
        while len(buf) < length:
            frame = self._recv_tester_frame(timeout_s)
            if frame is None:
                raise AssertionError("mock ECU: timed out mid multi-frame request")
            _cid, data = frame
            seq = data[0] & 0x0F
            assert seq == expected_seq, f"seq gap: want {expected_seq} got {seq}"
            remaining = length - len(buf)
            buf.extend(data[1 : 1 + min(remaining, 7)])
            expected_seq = (expected_seq + 1) & 0x0F
            frames_in_block += 1
            if (
                self._rx_block_size
                and frames_in_block >= self._rx_block_size
                and len(buf) < length
            ):
                self._send_fc(self._rx_block_size, self._rx_stmin)
                frames_in_block = 0
        return bytes(buf[:length])

    def _send_fc(self, bs: int, stmin: int) -> None:
        self._send_frame(ECU_ID, bytes([0x30, bs & 0xFF, stmin & 0xFF]))

    # -- ISO-TP send (ECU as sender) --

    def send_response(self, timeout_s: float = 3.0) -> None:
        payload = self._response
        if len(payload) <= 7:
            pci = (0x0 << 4) | len(payload)
            self._send_frame(ECU_ID, bytes([pci]) + payload)
            return
        # Multi-frame: First Frame, await tester FC, then Consecutive Frames.
        length = len(payload)
        ff = bytes([(0x1 << 4) | ((length >> 8) & 0x0F), length & 0xFF]) + payload[:6]
        self._send_frame(ECU_ID, ff)
        bs, stmin = self._await_tester_fc(timeout_s)
        offset = 6
        seq = 1
        frames_in_block = 0
        while offset < length:
            chunk = payload[offset : offset + 7]
            self._send_frame(ECU_ID, bytes([(0x2 << 4) | (seq & 0x0F)]) + chunk)
            offset += len(chunk)
            seq = (seq + 1) & 0x0F
            frames_in_block += 1
            if offset >= length:
                break
            if bs and frames_in_block >= bs:
                bs, stmin = self._await_tester_fc(timeout_s)
                frames_in_block = 0
            elif stmin:
                time.sleep(min(stmin / 1000.0, 0.01))

    def _await_tester_fc(self, timeout_s: float) -> Tuple[int, int]:
        frame = self._recv_tester_frame(timeout_s)
        if frame is None:
            raise AssertionError("mock ECU: no Flow Control from tester")
        _cid, data = frame
        assert (data[0] >> 4) == 0x3, "expected Flow Control from tester"
        assert (data[0] & 0x0F) == 0x0, "expected CTS from tester"
        return data[1], data[2]

    # -- full exchange --

    def serve_one_exchange(self) -> bytes:
        """Handshake, receive one request, send the response. Returns request."""
        self.handle_handshake()
        if self._silent:
            # Read (and discard) the request so the client's send completes,
            # but never reply — exercises the receive timeout path. Hold the
            # connection OPEN for the silence window so the client sees a real
            # timeout (no data), not an EOF.
            try:
                self.receive_request(timeout_s=2.0)
            except AssertionError:
                pass
            time.sleep(1.5)  # keep socket open past the client's receive window
            return b""
        request = self.receive_request(timeout_s=3.0)
        self.send_response(timeout_s=3.0)
        return request


class MockSlcanServer:
    """A real loopback TCP server speaking the ECU side of one SLCAN exchange.

    Binds 127.0.0.1:0 (ephemeral port). On the single accepted connection it
    runs one :class:`_EcuIsoTp` exchange in a background thread, recording the
    reassembled request the tester sent for later assertions.
    """

    def __init__(
        self,
        response_payload: bytes,
        rx_block_size: int = 0,
        rx_stmin: int = 0,
        silent: bool = False,
    ):
        self._response = response_payload
        self._rx_block_size = rx_block_size
        self._rx_stmin = rx_stmin
        self._silent = silent
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.host, self.port = self._srv.getsockname()
        self.received_request: Optional[bytes] = None
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            self._srv.settimeout(5.0)
            conn, _addr = self._srv.accept()
            with conn:
                ecu = _EcuIsoTp(
                    conn,
                    self._response,
                    rx_block_size=self._rx_block_size,
                    rx_stmin=self._rx_stmin,
                    silent=self._silent,
                )
                self.received_request = ecu.serve_one_exchange()
        except BaseException as exc:  # record so the test can surface it
            self._error = exc

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass

    def raise_if_error(self) -> None:
        if self._error is not None:
            raise AssertionError(f"mock server error: {self._error!r}")


@pytest.fixture
def make_server():
    """Factory fixture that starts a MockSlcanServer and tears it down."""
    servers: List[MockSlcanServer] = []

    def _factory(response_payload: bytes, **kwargs) -> MockSlcanServer:
        srv = MockSlcanServer(response_payload, **kwargs)
        srv.start()
        servers.append(srv)
        return srv

    yield _factory

    for srv in servers:
        srv.close()
        srv.join(timeout=2.0)


def _connected_transport(srv: MockSlcanServer) -> WiCANTransport:
    t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
    t.open()
    return t


# ---------------------------------------------------------------------------
# Construction / metadata / lifecycle
# ---------------------------------------------------------------------------


class TestConstructionAndMetadata:
    def test_description_includes_host_and_port(self):
        t = WiCANTransport("192.168.4.1", 3333)
        assert t.description == "WiCAN (192.168.4.1:3333)"

    def test_defaults_use_can_ids_from_constants(self):
        t = WiCANTransport("h", 1)
        assert t._tx_id == CAN_REQUEST_ID
        assert t._rx_id == CAN_RESPONSE_ID

    def test_wican_error_is_an_ecu_error(self):
        assert issubclass(WiCANError, ECUError)

    def test_send_before_open_raises(self):
        t = WiCANTransport("h", 1)
        with pytest.raises(WiCANError):
            t.send_message(b"\x3e\x00", 1000)

    def test_receive_before_open_raises(self):
        t = WiCANTransport("h", 1)
        with pytest.raises(WiCANError):
            t.receive_message(1000)

    def test_connect_failure_raises_wican_error(self):
        # Port 1 on loopback is essentially always refused.
        t = WiCANTransport("127.0.0.1", 1, connect_timeout_ms=500)
        with pytest.raises(WiCANError):
            t.open()

    def test_close_before_open_is_safe(self):
        t = WiCANTransport("h", 1)
        t.close()  # no raise
        t.close()  # idempotent


# ---------------------------------------------------------------------------
# Open / handshake
# ---------------------------------------------------------------------------


class TestOpenHandshake:
    def test_open_brings_up_channel_and_is_idempotent(self, make_server):
        srv = make_server(b"\x7e\x00")  # response unused here
        t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
        t.open()
        sock_after_first = t._sock
        assert sock_after_first is not None
        t.open()  # idempotent — must not reconnect
        assert t._sock is sock_after_first
        t.close()

    def test_open_rejected_bitrate_raises(self):
        """A BEL ack to the S6 bitrate command must raise WiCANError."""
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("127.0.0.1", 0))
        srv_sock.listen(1)
        host, port = srv_sock.getsockname()

        def _run():
            srv_sock.settimeout(5.0)
            conn, _ = srv_sock.accept()
            with conn:
                buf = bytearray()
                # Ack the first control command (C) with CR, NAK the next (S6).
                acked_close = False
                conn.settimeout(3.0)
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while b"\r" in buf:
                        idx = buf.index(b"\r")
                        del buf[: idx + 1]
                        if not acked_close:
                            conn.sendall(CR)  # ack the close
                            acked_close = True
                        else:
                            conn.sendall(BEL)  # NAK the bitrate
                            return

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        try:
            t = WiCANTransport(host, port, connect_timeout_ms=2000)
            with pytest.raises(WiCANError):
                t.open()
            assert t._sock is None  # socket not leaked on failed bring-up
        finally:
            srv_sock.close()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Round-trip exchanges over real loopback TCP
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_single_frame_request_and_response(self, make_server):
        # TesterPresent-style: request 3E 00, ECU replies 7E 00.
        srv = make_server(b"\x7e\x00")
        t = _connected_transport(srv)
        try:
            t.send_message(b"\x3e\x00", 2000)
            resp = t.receive_message(2000)
            assert resp == b"\x7e\x00"
        finally:
            t.close()
        srv.join()
        srv.raise_if_error()
        assert srv.received_request == b"\x3e\x00"

    def test_multi_frame_response_forces_our_flow_control(self, make_server):
        # ECU replies with a >7-byte payload: First Frame + our FC + CFs.
        response = bytes(range(1, 51))  # 50 bytes -> multi-frame
        srv = make_server(response)
        t = _connected_transport(srv)
        try:
            t.send_message(b"\x22\xf1\x90", 2000)  # ReadDataByIdentifier-ish
            resp = t.receive_message(2000)
            assert resp == response
        finally:
            t.close()
        srv.join()
        srv.raise_if_error()
        assert srv.received_request == b"\x22\xf1\x90"

    def test_multi_frame_request_forces_ecu_flow_control(self, make_server):
        # ~1KB request: First Frame + ECU FC + many Consecutive Frames.
        request = bytes((i & 0xFF) for i in range(1000))
        srv = make_server(b"\x76\x01")  # short positive response
        t = _connected_transport(srv)
        try:
            t.send_message(request, 4000)
            resp = t.receive_message(2000)
            assert resp == b"\x76\x01"
        finally:
            t.close()
        srv.join()
        srv.raise_if_error()
        assert srv.received_request == request

    def test_multi_frame_request_with_ecu_block_size(self, make_server):
        # ECU advertises BS=4: tester must re-await FC every 4 CFs.
        request = bytes((i & 0xFF) for i in range(1000))
        srv = make_server(b"\x76\x02", rx_block_size=4)
        t = _connected_transport(srv)
        try:
            t.send_message(request, 4000)
            resp = t.receive_message(2000)
            assert resp == b"\x76\x02"
        finally:
            t.close()
        srv.join()
        srv.raise_if_error()
        assert srv.received_request == request

    def test_multi_frame_both_directions_1kb(self, make_server):
        # Big request AND big response in one exchange.
        request = bytes((i * 3 & 0xFF) for i in range(1000))
        response = bytes((i * 7 & 0xFF) for i in range(800))
        srv = make_server(response)
        t = _connected_transport(srv)
        try:
            t.send_message(request, 5000)
            resp = t.receive_message(5000)
            assert resp == response
        finally:
            t.close()
        srv.join()
        srv.raise_if_error()
        assert srv.received_request == request


# ---------------------------------------------------------------------------
# Timeout contract
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_receive_returns_none_on_server_silence(self, make_server):
        # Server consumes the request but never replies.
        srv = make_server(b"", silent=True)
        t = _connected_transport(srv)
        try:
            t.send_message(b"\x3e\x00", 2000)
            start = time.monotonic()
            resp = t.receive_message(500)
            elapsed = time.monotonic() - start
            assert resp is None
            # It actually waited (roughly) the requested window, not instant.
            assert elapsed >= 0.4
        finally:
            t.close()

    def test_receive_none_does_not_raise(self, make_server):
        srv = make_server(b"", silent=True)
        t = _connected_transport(srv)
        try:
            # No send at all — pure receive timeout must be None, not an error.
            assert t.receive_message(300) is None
        finally:
            t.close()


# ---------------------------------------------------------------------------
# Socket-error / peer-close contract
# ---------------------------------------------------------------------------


class TestSocketErrors:
    def test_peer_close_during_receive_raises_wican_error(self):
        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("127.0.0.1", 0))
        srv_sock.listen(1)
        host, port = srv_sock.getsockname()

        ready = threading.Event()

        def _run():
            srv_sock.settimeout(5.0)
            conn, _ = srv_sock.accept()
            # Ack the three handshake commands, then drop the connection.
            buf = bytearray()
            acked = 0
            conn.settimeout(3.0)
            while acked < 3:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\r" in buf:
                    idx = buf.index(b"\r")
                    del buf[: idx + 1]
                    conn.sendall(CR)
                    acked += 1
            ready.set()
            conn.close()  # hard close -> recv on client returns b""

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        try:
            t = WiCANTransport(host, port, connect_timeout_ms=2000)
            t.open()
            ready.wait(2.0)
            # Peer has closed; a receive should detect EOF and raise.
            with pytest.raises(WiCANError):
                t.receive_message(1000)
            t.close()
        finally:
            srv_sock.close()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# SLCAN wire-level sanity: the bytes we put on the socket are real SLCAN.
# ---------------------------------------------------------------------------


class TestWireBytes:
    def test_sent_request_frame_is_slcan_encoded(self, make_server):
        # The mock reassembles via SlcanFrameStream, so a correct request
        # proves we emitted valid 't7E0...' SLCAN lines on the socket.
        srv = make_server(b"\x7e\x00")
        t = _connected_transport(srv)
        try:
            t.send_message(b"\x10\x85", 2000)
            assert t.receive_message(2000) == b"\x7e\x00"
        finally:
            t.close()
        srv.join()
        srv.raise_if_error()
        assert srv.received_request == b"\x10\x85"

    def test_encode_data_frame_uses_tester_id(self):
        # Guards the tx id the session is wired with.
        line = encode_data_frame(TESTER_ID, b"\x3e\x00")
        assert line.startswith(b"t7E0")


# ---------------------------------------------------------------------------
# Raw-server helper for adversarial handshake / corruption tests.
# ---------------------------------------------------------------------------


class _RawServer:
    """A loopback TCP server driven by an arbitrary handler(conn) callable.

    Lets a test script the exact bytes the WiCAN adapter puts on the wire
    (coalesced BEL acks, data frames racing the handshake, corrupt CFs) without
    the cooperative-ISO-TP behaviour of :class:`MockSlcanServer`.
    """

    def __init__(self, handler):
        self._handler = handler
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.host, self.port = self._srv.getsockname()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._srv.settimeout(5.0)
            conn, _ = self._srv.accept()
            with conn:
                self._handler(conn)
        except BaseException as exc:  # recorded; tests assert on client side
            self._error = exc

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def _read_one_command(conn: socket.socket, timeout: float = 3.0) -> bytes:
    """Block until one CR-terminated command line is read; return it (with CR)."""
    conn.settimeout(timeout)
    buf = bytearray()
    while b"\r" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    idx = buf.index(b"\r")
    return bytes(buf[: idx + 1])


class _LineReader:
    """Buffered CR-line reader over a socket — no byte loss across reads.

    Unlike :func:`_read_one_command` (which discards anything past the first CR
    in a recv chunk), this keeps a persistent buffer, so it is safe when the
    client coalesces several lines into one TCP segment — e.g. the warm-up frame
    and the first real request, which are now sent back-to-back after open().
    """

    def __init__(self, conn: socket.socket, timeout: float = 3.0):
        self._conn = conn
        self._timeout = timeout
        self._buf = bytearray()

    def next_line(self) -> bytes:
        """Return the next CR-terminated line (with the CR), buffering the rest."""
        while b"\r" not in self._buf:
            self._conn.settimeout(self._timeout)
            chunk = self._conn.recv(4096)
            if not chunk:
                break
            self._buf.extend(chunk)
        idx = self._buf.index(b"\r")
        line = bytes(self._buf[: idx + 1])
        del self._buf[: idx + 1]
        return line


# ---------------------------------------------------------------------------
# Handshake robustness: coalesced BEL NAK + data frame racing the handshake.
# ---------------------------------------------------------------------------


class TestHandshakeRobustness:
    def test_coalesced_bel_with_data_frame_on_s6_raises(self):
        """A BEL NAK on S6 that is coalesced with a trailing data frame in the
        SAME recv chunk must still be detected (endswith-only would miss it)."""

        def handler(conn):
            _read_one_command(conn)  # C
            conn.sendall(CR)  # ack the close
            _read_one_command(conn)  # S6
            # NAK coalesced with a stray data frame in one write: BEL is NOT the
            # last byte, so an endswith check would wrongly pass it.
            conn.sendall(BEL + encode_data_frame(ECU_ID, b"\x7e\x00"))
            time.sleep(0.5)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            with pytest.raises(WiCANError):
                t.open()
            assert t._sock is None  # socket not leaked on failed bring-up
        finally:
            srv.close()

    def test_bare_bel_cr_on_s6_raises(self):
        """A BEL immediately followed by a CR (BEL not last only if endswith CR
        misreads) must be treated as a NAK."""

        def handler(conn):
            _read_one_command(conn)  # C
            conn.sendall(CR)
            _read_one_command(conn)  # S6
            conn.sendall(b"\x07\r")  # BEL then CR
            time.sleep(0.5)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            with pytest.raises(WiCANError):
                t.open()
        finally:
            srv.close()

    def test_data_frame_racing_handshake_does_not_break_open(self):
        """A CAN data frame interleaved with the O (open) ack must be parsed
        gracefully by the handshake logic (not misread as a control ack, no
        crash). The post-open warm-up then drains it as part of establishing a
        clean slate, so it does NOT leak into the caller's first real receive.
        """

        def handler(conn):
            _read_one_command(conn)  # C
            conn.sendall(CR)
            _read_one_command(conn)  # S6
            conn.sendall(CR)
            _read_one_command(conn)  # O
            # Open ack CR coalesced with an early single-frame ECU response.
            sf = encode_data_frame(ECU_ID, bytes([0x02, 0x7E, 0x00]))
            conn.sendall(CR + sf)
            _read_one_command(conn)  # absorb the warm-up frame the client sends
            time.sleep(1.5)  # hold the socket open through the client's receive

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()  # the coalesced frame must not have broken bring-up
            # The frame that raced the handshake was drained by the warm-up, so
            # the channel is a clean slate — the first receive sees nothing.
            assert t.receive_message(400) is None
            t.close()
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# Corruption-vs-timeout contract at the receive seam.
# ---------------------------------------------------------------------------


def _ack_handshake(conn: socket.socket) -> None:
    """Ack the three bring-up commands (C/S6/O) each with a bare CR."""
    for _ in range(3):
        _read_one_command(conn)
        conn.sendall(CR)


class TestReceiveCorruptionContract:
    def test_multi_frame_sequence_gap_raises_not_none(self):
        """A multi-frame response with a Consecutive-Frame sequence gap is
        corruption, not silence: receive_message must raise WiCANError (so the
        UDS response-pending loop aborts) and must NOT return None."""

        def handler(conn):
            _ack_handshake(conn)
            r = _LineReader(conn)
            r.next_line()  # the warm-up frame (3E 80), consumed silently
            r.next_line()  # the SF request from the tester
            # First Frame declaring 20 bytes, then a CF with the WRONG seq.
            payload = bytes(range(20))
            ff = bytes([0x10, 0x14]) + payload[:6]
            conn.sendall(encode_data_frame(ECU_ID, ff))
            r.next_line()  # tester Flow Control CTS
            # Expected seq 1, send seq 2 -> sequence gap.
            bad_cf = bytes([0x22]) + payload[6:13]
            conn.sendall(encode_data_frame(ECU_ID, bad_cf))
            time.sleep(1.0)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            t.send_message(b"\x22\xf1\x90", 2000)
            with pytest.raises(WiCANError):
                t.receive_message(2000)
            t.close()
        finally:
            srv.close()

    def test_multi_frame_short_consecutive_frame_raises_not_none(self):
        """A short (truncated) non-final Consecutive Frame is silent corruption;
        receive_message must raise, not swallow it as a timeout (None)."""

        def handler(conn):
            _ack_handshake(conn)
            r = _LineReader(conn)
            r.next_line()  # the warm-up frame (3E 80), consumed silently
            r.next_line()  # request
            payload = bytes(range(20))
            ff = bytes([0x10, 0x14]) + payload[:6]
            conn.sendall(encode_data_frame(ECU_ID, ff))
            r.next_line()  # FC CTS
            # Non-final CF carrying only 4 data bytes (must be 7) -> short frame.
            short_cf = bytes([0x21]) + payload[6:10]
            conn.sendall(encode_data_frame(ECU_ID, short_cf))
            time.sleep(1.0)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            t.send_message(b"\x22\xf1\x90", 2000)
            with pytest.raises(WiCANError):
                t.receive_message(2000)
            t.close()
        finally:
            srv.close()

    def test_benign_silence_still_returns_none(self, make_server):
        """The flip side of the corruption contract: a genuine no-response
        (timeout) must still map to None, not raise."""
        srv = make_server(b"", silent=True)
        t = _connected_transport(srv)
        try:
            t.send_message(b"\x3e\x00", 2000)
            assert t.receive_message(400) is None
        finally:
            t.close()


# ---------------------------------------------------------------------------
# Channel priming: the dropped-first-frame warm-up.
# ---------------------------------------------------------------------------


class TestChannelPriming:
    def test_open_emits_warmup_frame_after_handshake(self):
        """open() must emit exactly one throwaway warm-up frame right after the
        SLCAN bring-up. The WiCAN adapter drops its first post-open frame, so
        without this the caller's first real request hangs for the full receive
        timeout. The frame must be a single-frame TesterPresent with
        suppress-positive-response (3E 80) on the tester id."""
        captured = {}

        def handler(conn):
            _ack_handshake(conn)  # C / S6 / O
            captured["frame"] = _read_one_command(conn)  # the priming frame
            time.sleep(0.3)  # hold the socket so open() completes cleanly

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            t.close()
        finally:
            srv.close()

        assert "frame" in captured, "open() emitted no warm-up frame"
        decoded = decode_frame(captured["frame"])
        assert decoded is not None
        can_id, data = decoded
        assert can_id == TESTER_ID
        # ISO-TP single frame (PCI 0x02) carrying TesterPresent + suppressPosRsp.
        assert data[0] == 0x02
        assert data[1:3] == b"\x3e\x80"

    def test_flush_drains_pending_frames(self):
        """flush() must discard buffered/in-flight frames so a read retry's
        re-requested block is not corrupted by leftovers from the failed try."""

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)  # warm-up frame
            # Push a stray frame the client would otherwise keep buffered.
            conn.sendall(encode_data_frame(ECU_ID, bytes([0x02, 0x7E, 0x00])))
            # Hold the socket open until the client disconnects (t.close()), so
            # the channel stays alive through receive_message() even on a slow
            # runner. A fixed sleep can expire mid-read and surface as a spurious
            # "socket closed by peer" (seen on a slow CI runner).
            conn.settimeout(10.0)
            try:
                while conn.recv(4096):
                    pass
            except OSError:
                pass

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            # Let the stray frame arrive and get buffered, then flush it away.
            time.sleep(0.3)
            t._frame_buffer.append((ECU_ID, b"\x7e\x00"))  # simulate a leftover
            t.flush()
            assert t._frame_buffer == []
            assert t.receive_message(300) is None  # channel is clean
            t.close()
        finally:
            srv.close()

    def test_flush_before_open_is_safe(self):
        t = WiCANTransport("h", 1)
        t.flush()  # no raise when never opened

    def test_warmup_reply_is_drained_not_left_for_first_receive(self):
        """If the ECU answers the warm-up (here a 7F 3E 12 rejection of the
        suppress-positive-response sub-function, as the NC ECU does), that reply
        must be drained during open() — never delivered as the answer to the
        caller's first real request."""

        def handler(conn):
            _ack_handshake(conn)  # C / S6 / O
            _read_one_command(conn)  # the warm-up frame (3E 80)
            # ECU rejects the 0x80 sub-function — this must be drained, not kept.
            conn.sendall(encode_data_frame(ECU_ID, bytes([0x03, 0x7F, 0x3E, 0x12])))
            time.sleep(1.5)  # hold the socket open through the client's receive

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            # The stray 7F 3E 12 was drained; the channel is clean.
            assert t.receive_message(400) is None
            t.close()
        finally:
            srv.close()


class TestTransportTuning:
    """Read-speed tuning knobs: N_Cr fast-fail, TCP_NODELAY, SO_RCVBUF."""

    def test_n_cr_default_and_override_reach_the_session(self):
        from src.ecu.wican_transport import DEFAULT_N_CR_MS

        # Default is wired into the ISO-TP session so dropped frames fail fast.
        assert WiCANTransport("h", 1)._session.n_cr_ms == DEFAULT_N_CR_MS
        # And can be disabled (restores the wait-the-whole-budget behaviour).
        assert WiCANTransport("h", 1, n_cr_ms=None)._session.n_cr_ms is None

    def test_tx_stmin_floor_default_and_override_reach_the_session(self):
        from src.ecu.wican_transport import DEFAULT_TX_STMIN

        # The outbound CF pacing floor (the write/flash fix) is wired into the
        # ISO-TP session so our TransferData burst can't overrun the gateway.
        assert WiCANTransport("h", 1)._session.tx_stmin == DEFAULT_TX_STMIN
        # And is tunable (0 reproduces the unpaced, drop-prone behaviour).
        assert WiCANTransport("h", 1, tx_stmin=0)._session.tx_stmin == 0

    @staticmethod
    def _hold_open_handler(conn):
        _ack_handshake(conn)  # C / S6 / O
        _read_one_command(conn)  # the warm-up prime (3E 80)
        time.sleep(0.5)  # hold the socket open while the test inspects it

    def test_tcp_nodelay_enabled_by_default_on_open(self):
        srv = _RawServer(self._hold_open_handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                val = t._sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
                assert val != 0  # Nagle disabled
            finally:
                t.close()
        finally:
            srv.close()

    def test_tcp_nodelay_can_be_disabled(self):
        srv = _RawServer(self._hold_open_handler)
        try:
            t = WiCANTransport(
                srv.host, srv.port, connect_timeout_ms=2000, tcp_nodelay=False
            )
            t.open()
            try:
                val = t._sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
                assert val == 0  # Nagle left on
            finally:
                t.close()
        finally:
            srv.close()

    def test_so_rcvbuf_option_is_applied_without_error(self):
        # OSes round/cap SO_RCVBUF differently, so we don't assert an exact
        # value — only that requesting it is honoured on the code path and the
        # socket still opens and reports a positive buffer.
        srv = _RawServer(self._hold_open_handler)
        try:
            t = WiCANTransport(
                srv.host, srv.port, connect_timeout_ms=2000, so_rcvbuf=256 * 1024
            )
            t.open()
            try:
                assert t._sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) > 0
            finally:
                t.close()
        finally:
            srv.close()


def _read_fast_read_cmd(conn) -> bytes:
    """Read one ``X<addr><len>\\r`` command off the raw connection."""
    cmd = b""
    while not cmd.endswith(b"\r"):
        chunk = conn.recv(64)
        if not chunk:
            break
        cmd += chunk
    return cmd


class TestFastRead:
    """fast_read() sends X<addr><len>, resyncs on the firmware's NCFRDATA sync
    preamble, then reads the raw ROM stream (chunking large reads)."""

    def test_fast_read_sends_command_and_returns_streamed_bytes(self):
        payload = bytes((i * 7) & 0xFF for i in range(300))
        n = len(payload)
        seen = {}

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)  # warm-up prime (3E 80)
            seen["cmd"] = _read_fast_read_cmd(conn)
            # Firmware emits the sync preamble, THEN the ROM bytes.
            conn.sendall(_FAST_READ_SYNC + payload)
            time.sleep(0.5)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                data = t.fast_read(0x1234, n, timeout_ms=3000)
                assert data == payload
            finally:
                t.close()
        finally:
            srv.close()

        # Command framing: 'X' + 8 hex start + 8 hex length + CR.
        assert seen["cmd"] == b"X00001234%08X\r" % n

    def test_fast_read_resyncs_past_leading_can_traffic(self):
        """CAN frames queued before the firmware suspends forwarding precede the
        sync preamble; they must be discarded, not counted as ROM."""
        payload = bytes((i * 3) & 0xFF for i in range(200))
        n = len(payload)
        junk = b"t2008A7102710271020FF\rt201800007D00FFFFFFFF\r"

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)
            _read_fast_read_cmd(conn)
            conn.sendall(junk + _FAST_READ_SYNC + payload)
            time.sleep(0.5)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                assert t.fast_read(0, n, timeout_ms=3000) == payload
            finally:
                t.close()
        finally:
            srv.close()

    def test_fast_read_chunks_large_reads(self):
        """A read longer than the chunk size is split into back-to-back
        commands; the result is the concatenation, in order."""
        chunk = 64
        total = chunk * 3 + 10  # 4 commands (3 full + 1 partial)
        full = bytes(i & 0xFF for i in range(total))
        cmds = []

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)
            sent = 0
            while sent < total:
                cmd = _read_fast_read_cmd(conn)
                if not cmd:
                    return
                cmds.append(cmd)
                # Length requested is the last 8 hex chars before CR.
                n = int(cmd[9:17], 16)
                conn.sendall(_FAST_READ_SYNC + full[sent : sent + n])
                sent += n
            time.sleep(0.3)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                data = t.fast_read(0, total, timeout_ms=4000, chunk=chunk)
                assert data == full
            finally:
                t.close()
        finally:
            srv.close()

        assert len(cmds) == 4  # ceil(202 / 64)
        # Each command's start advances by the previous chunk length.
        starts = [int(c[1:9], 16) for c in cmds]
        assert starts == [0, chunk, chunk * 2, chunk * 3]

    def test_fast_read_default_chunk_constant_is_sane(self):
        # 1 MB read must split into several commands (the firmware can't sustain
        # one very long stream); guard the constant against accidental bloat.
        assert 0 < _FAST_READ_CHUNK <= 256 * 1024
        assert (1 << 20) // _FAST_READ_CHUNK >= 4

    def test_fast_read_raises_on_short_stream(self):
        """A firmware block failure stops the stream short -> WiCANError (the
        caller then falls back to the per-block read)."""

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)  # prime
            _read_fast_read_cmd(conn)
            conn.sendall(_FAST_READ_SYNC + b"\x00" * 100)  # fewer than requested
            time.sleep(0.5)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                with pytest.raises(WiCANError):
                    t.fast_read(0, 256, timeout_ms=1000)
            finally:
                t.close()
        finally:
            srv.close()

    def test_fast_read_surfaces_firmware_frerr(self):
        """When the firmware aborts mid-block it streams an ASCII FRERR line; the
        host raises with that diagnostic so the cause is visible."""

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)
            _read_fast_read_cmd(conn)
            conn.sendall(
                _FAST_READ_SYNC
                + b"\x00" * 50
                + b"\r\nFRERR a=00D8400 st=6 pnd=0 hf=0 f=0000000000000000\r\n"
            )
            time.sleep(0.5)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                with pytest.raises(WiCANError, match="FRERR"):
                    t.fast_read(0, 4096, timeout_ms=1500)
            finally:
                t.close()
        finally:
            srv.close()


class TestVersionPing:
    """version_ping() identifies the live fast-read firmware build."""

    def test_returns_marker_amid_can_traffic(self):
        seen = {}

        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)  # prime
            seen["cmd"] = _read_fast_read_cmd(conn)
            # Live CAN frames bracket the marker; only NCFRv... is the answer.
            conn.sendall(b"t2008A7102710271020FF\rNCFRv4\nt201800007D00FF\r")
            time.sleep(0.3)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                assert t.version_ping(window_ms=2000) == b"NCFRv4"
            finally:
                t.close()
        finally:
            srv.close()

        # Command targets the version sentinel address.
        assert seen["cmd"][:9] == b"X%08X" % _FAST_READ_PING_ADDR

    def test_returns_none_when_no_marker(self):
        def handler(conn):
            _ack_handshake(conn)
            _read_one_command(conn)
            _read_fast_read_cmd(conn)
            conn.sendall(b"t2008A7102710271020FF\rt201800007D00FFFFFFFF\r")
            time.sleep(0.3)

        srv = _RawServer(handler)
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open()
            try:
                assert t.version_ping(window_ms=400) is None
            finally:
                t.close()
        finally:
            srv.close()
