"""Wire-transcript tests: what a marker-only probe is allowed to put on the wire.

These exist because of a real defect that shipped past a full mocked test suite.
Settings > Test Connection was built on the claim "it only reads the firmware
version marker, so it cannot disturb a running datalog trip" — but it called
``WiCANTransport.open()``, which sends the SLCAN ``C`` (close channel), ``S6``,
``O`` bring-up and then ``_prime_channel()``'s real TesterPresent frame, and
``close()`` sent ``C`` again. On the firmware, ``C`` maps to ``can_disable()``
on the SHARED CAN peripheral, and a bus left disabled with no owner costs the
datalogger a ~10 second withhold. So the one button whose entire design rationale
was "cannot interrupt logging" punched a hole in the user's trip data.

Every mock-based test passed with that bug fully present: they asserted that no
``WiCANDatalogClient`` was constructed and that ``send_message`` was not called,
while the offending bytes went out through the transport's own internals against
a MagicMock that never ran the real ``open()``.

The bug WAS extra bytes on the wire, so the fence has to be the wire. These tests
stand up a real loopback TCP server, record every byte the client sends, and
assert on the transcript. They fail loudly if the channel bring-up, the prime
frame, or the closing ``C`` ever comes back to a probe path.
"""

import socket
import threading
import time
from typing import Optional

import pytest

from src.ecu.wican_transport import (
    _FAST_READ_PING_ADDR,
    _FAST_READ_SYNC,
    WiCANTransport,
)

# The SLCAN channel-control commands. None of these may appear in a probe
# transcript: on this firmware `C` disables the shared CAN peripheral outright.
CLOSE_CMD = b"C\r"
BITRATE_CMD = b"S6\r"
OPEN_CMD = b"O\r"
CHANNEL_CMDS = (CLOSE_CMD, BITRATE_CMD, OPEN_CMD)


class _TranscriptServer:
    """Loopback TCP server that records EVERY byte the client sends.

    ``reply_with`` is sent once the first CR-terminated command arrives, so a
    probe can complete its ``version_ping`` and close cleanly; the handler then
    lingers briefly so any trailing bytes (the thing we care about) are captured
    before the connection goes away.
    """

    def __init__(self, reply_with: bytes = b"", linger_s: float = 0.4):
        self._reply = reply_with
        self._linger = linger_s
        self.transcript = bytearray()
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
                conn.settimeout(0.2)
                replied = False
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        if replied:
                            # Give the client its window to send anything else
                            # (a stray close command) before we stop listening.
                            deadline = min(deadline, time.monotonic() + self._linger)
                        continue
                    if not chunk:
                        break  # client sent FIN
                    self.transcript.extend(chunk)
                    if not replied and b"\r" in chunk and self._reply:
                        conn.sendall(self._reply)
                        replied = True
        except BaseException as exc:  # recorded; tests assert on client side
            self._error = exc

    def wait_closed(self, timeout=2.0):
        """Block until the handler thread has finished with the connection.

        The handler loop breaks the moment the client's FIN arrives, so this
        returns in ~1 ms — and unlike a fixed sleep it waits for the actual
        event, so it cannot flake on a loaded machine or waste time on a fast
        one. Every transcript assertion must go through this: reading the
        transcript while the handler is still appending is a race.
        """
        self._thread.join(timeout)

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


@pytest.fixture
def marker_server():
    srv = _TranscriptServer(reply_with=b"NCFRv9\n")
    yield srv
    srv.close()


class TestMarkerOnlyProbeWire:
    """``open_socket_only`` + ``version_ping`` + ``close`` — the Test Connection path."""

    def test_sends_only_the_version_command(self, marker_server):
        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        try:
            assert t.version_ping(window_ms=1500) == b"NCFRv9"
        finally:
            t.close()
        marker_server.wait_closed()  # any trailing byte has landed by now

        sent = bytes(marker_server.transcript)
        # Exactly one command line, and it is the version sentinel read.
        assert sent.count(b"\r") == 1, f"expected 1 command, transcript={sent!r}"
        assert sent.startswith(b"X%08X" % _FAST_READ_PING_ADDR), sent

    def test_no_channel_control_commands(self, marker_server):
        """THE regression fence. `C` disables the shared CAN peripheral; `S6`/`O`
        reinstall it. A probe that emits any of them breaks a live datalog trip,
        which is precisely the defect this path exists to avoid."""
        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        try:
            t.version_ping(window_ms=1500)
        finally:
            t.close()
        marker_server.wait_closed()

        sent = bytes(marker_server.transcript)
        for cmd in CHANNEL_CMDS:
            assert cmd not in sent, f"probe emitted {cmd!r}; transcript={sent!r}"

    def test_no_can_data_frame(self, marker_server):
        """No prime frame, no TesterPresent — nothing addressed to the ECU.

        An SLCAN data frame is ``t<id><len><data>``; the prime is ``t7E02 3E80``.
        """
        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        try:
            t.version_ping(window_ms=1500)
        finally:
            t.close()
        marker_server.wait_closed()

        sent = bytes(marker_server.transcript)
        assert b"t7E0" not in sent, f"probe put a frame on the bus: {sent!r}"
        assert b"3E80" not in sent.upper(), f"probe sent a TesterPresent: {sent!r}"

    def test_close_sends_nothing_after_the_version_command(self, marker_server):
        """The trailing `C` is the half that leaves the bus disabled with NO
        owner, so the firmware waits out its full withhold before self-healing.
        A socket-only probe must hang up with a bare FIN."""
        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        t.version_ping(window_ms=1500)
        before_close = bytes(marker_server.transcript)
        t.close()
        marker_server.wait_closed()

        assert bytes(marker_server.transcript) == before_close, (
            "close() transmitted bytes on a socket-only transport: "
            f"{bytes(marker_server.transcript)[len(before_close):]!r}"
        )

    def test_open_socket_only_is_idempotent(self, marker_server):
        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        try:
            t.open_socket_only()  # second call must be a no-op, not a reconnect
            assert t.version_ping(window_ms=1500) == b"NCFRv9"
        finally:
            t.close()

    def test_close_without_open_is_safe(self):
        WiCANTransport("127.0.0.1", 1).close()  # never opened — must not raise


class TestFullOpenStillBringsUpTheChannel:
    """The session path must be UNCHANGED by the split. If these fail, the
    refactor broke the real ECU link rather than the probe."""

    def test_open_emits_the_bring_up_sequence(self):
        srv = _TranscriptServer(reply_with=b"\r\r\r")
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            try:
                t.open()
            except Exception:
                # The stub server does not ack precisely enough to guarantee a
                # clean bring-up on every platform; the transcript is the point.
                pass
            finally:
                t.close()
            srv.wait_closed()

            sent = bytes(srv.transcript)
            assert CLOSE_CMD in sent, f"open() lost its clean-slate C: {sent!r}"
        finally:
            srv.close()

    def test_channel_flag_is_false_until_open_succeeds(self, marker_server):
        """`close()` decides whether to send `C` from this flag, so a probe
        transport must never look like it owns a channel."""
        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        assert t._channel_open is False
        t.open_socket_only()
        try:
            assert t._channel_open is False
        finally:
            t.close()
        assert t._channel_open is False


class TestSocketOnlyTransportIsFenced:
    """A socket-only transport must not be usable as if it had a channel.

    Both of these were false-assurance docstrings caught in re-review: the code
    claimed idempotence it did not deliver, and claimed sends "will fail" when
    they silently succeeded. That is the same shape as the original defect —
    a stated safety property nothing enforced — so they are enforced here.
    """

    def test_send_message_is_refused_without_a_channel(self, marker_server):
        """Without this guard the frame really would reach the bus: the host
        checks only the socket, and the firmware gates transmission on its own
        CAN-enable bit (which the datalogger keeps set), not on whether an ``O``
        was ever issued."""
        from src.ecu.wican_transport import WiCANError

        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        try:
            with pytest.raises(WiCANError, match="socket-only"):
                t.send_message(b"\x3e\x80", timeout_ms=200)
        finally:
            t.close()
        marker_server.wait_closed()

        # And nothing reached the wire.
        assert b"t7E0" not in bytes(marker_server.transcript)

    def test_receive_message_is_refused_without_a_channel(self, marker_server):
        from src.ecu.wican_transport import WiCANError

        t = WiCANTransport(
            marker_server.host, marker_server.port, connect_timeout_ms=2000
        )
        t.open_socket_only()
        try:
            with pytest.raises(WiCANError, match="socket-only"):
                t.receive_message(timeout_ms=200)
        finally:
            t.close()

    def test_fast_read_is_refused_without_a_channel(self):
        """Every CAN-moving entry point must carry the guard, not just the two
        that were remembered when it was added. This one WAS missed: the
        invariant was enforced by enumerating call sites, and the enumeration
        was incomplete."""
        from src.ecu.wican_transport import WiCANError

        srv = _TranscriptServer()
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open_socket_only()
            try:
                with pytest.raises(WiCANError, match="socket-only"):
                    t.fast_read(0, 16, timeout_ms=200)
            finally:
                t.close()
        finally:
            srv.close()

    def test_every_can_moving_method_carries_the_guard(self):
        """Tripwire for the NEXT one. If a method moves CAN traffic it must call
        _require_channel; socket-level methods must not. Listed explicitly so
        adding a method forces a decision here rather than being forgotten —
        which is exactly how fast_read came to be missing the guard."""
        import inspect

        from src.ecu import wican_transport

        split_token = chr(10) + "    def "
        src = inspect.getsource(wican_transport.WiCANTransport)

        def body_of(name):
            return src.split("def " + name + "(", 1)[1].split(split_token, 1)[0]

        for name in ("send_message", "receive_message", "fast_read"):
            assert "_require_channel()" in body_of(name), (
                name + " lost its channel guard: a socket-only transport could "
                "put real frames on the shared CAN bus"
            )
        assert "_require_channel()" not in body_of("version_ping"), (
            "version_ping must stay socket-level — requiring a channel there "
            "would defeat the whole marker-only probe path"
        )

    def test_open_after_open_socket_only_brings_the_channel_up(self):
        """It used to return silently, handing back a transport the caller
        believed had a channel and did not."""
        srv = _TranscriptServer(reply_with=b"\r\r\r")
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open_socket_only()
            assert t._channel_open is False
            try:
                t.open()
            except Exception:
                pass  # stub server cannot ack cleanly; the transcript is the point
            finally:
                t.close()
            srv.wait_closed()

            assert CLOSE_CMD in bytes(srv.transcript), (
                "open() after open_socket_only() sent no bring-up: "
                f"{bytes(srv.transcript)!r}"
            )
        finally:
            srv.close()


class TestVersionPingNeedsNoChannel:
    def test_version_ping_requires_an_open_socket(self):
        """It needs a SOCKET, not a channel — that is what makes Option B work."""
        from src.ecu.wican_transport import WiCANError

        t = WiCANTransport("127.0.0.1", 1)
        with pytest.raises(WiCANError):
            t.version_ping(window_ms=100)

    def test_marker_is_found_amid_live_can_traffic(self):
        """The port still carries forwarded CAN frames; the marker must be picked
        out of them rather than confused by them."""
        srv = _TranscriptServer(
            reply_with=b"t2008A7102710271020FF\rNCFRv7\nt201800007D00FF\r"
        )
        try:
            t = WiCANTransport(srv.host, srv.port, connect_timeout_ms=2000)
            t.open_socket_only()
            try:
                assert t.version_ping(window_ms=1500) == b"NCFRv7"
            finally:
                t.close()
        finally:
            srv.close()


def test_sync_marker_constant_is_not_a_channel_command():
    """Guard against a future constant change making the assertions above vacuous."""
    for cmd in CHANNEL_CMDS:
        assert cmd not in _FAST_READ_SYNC
