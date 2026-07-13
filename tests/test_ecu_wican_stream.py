"""Tests for src/ecu/wican_stream.py — the WiCAN live-datalog stream receiver.

This is THE NCDLv1 line-read pipeline: a framing or state-machine bug here
mis-attributes CSV rows to the wrong session or silently drops them. The tests
stand up a real in-process TCP server on loopback (``MockStreamServer``) that
speaks the device side of NCDLv1 — banner, control lines, header/rows — and
exercise the full ``connect``/``run``/``stop`` contract, including the banner
capability gate, mid-session join, ``#drop``/``#close`` handling, a prompt
``stop()`` during a blocking read, and an unexpected server disconnect.

Everything runs headless over 127.0.0.1 loopback — no hardware, no PySide6.
"""

import socket
import threading
import time

import pytest

from src.ecu.wican_stream import (
    KIND_CLOSE,
    KIND_DROP,
    KIND_HEADER,
    KIND_HELLO,
    KIND_IDLE,
    KIND_NOHDR,
    KIND_ROW,
    KIND_SESSION,
    WiCANLiveStreamClient,
    WiCANStreamError,
    WiCANStreamUnsupported,
)

DEFAULT_BANNER = b"#hello NCDLv1 fw=test1.0\n"


# ---------------------------------------------------------------------------
# Mock NCDLv1 device: a real loopback TCP server speaking the device side.
# ---------------------------------------------------------------------------


class MockStreamServer:
    """A real loopback TCP server speaking the device side of one NCDLv1 stream.

    Binds 127.0.0.1:0 (ephemeral port). On the single accepted connection it
    sends ``banner`` then every line in ``initial`` (each must already include
    its ``\\n``). ``feed()`` pushes more lines over the live connection so a test
    can drive rows after the client is streaming. With ``close_after_initial``
    the server drops the connection right after ``initial`` (simulates an
    unexpected disconnect); otherwise it lingers so the client can ``stop()``.
    """

    def __init__(
        self,
        *,
        banner=DEFAULT_BANNER,
        initial=(),
        close_after_initial=False,
        linger_s=10.0,
    ):
        self._banner = banner
        self._initial = list(initial)
        self._close_after_initial = close_after_initial
        self._linger_s = linger_s
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._conn = None
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.host, self.port = self._srv.getsockname()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        self._srv.settimeout(5.0)
        try:
            conn, _addr = self._srv.accept()
        except OSError:
            return
        with self._lock:
            self._conn = conn
        self._connected.set()
        try:
            if self._banner is not None:
                conn.sendall(self._banner)
            for line in self._initial:
                conn.sendall(line)
            if self._close_after_initial:
                return  # drop the connection -> client sees an unexpected EOF
            self._stop.wait(self._linger_s)
        except OSError:
            pass
        finally:
            with self._lock:
                self._conn = None
            try:
                conn.close()
            except OSError:
                pass

    def feed(self, data: bytes):
        """Send more bytes over the accepted connection (test-driven rows)."""
        assert self._connected.wait(5.0), "client never connected"
        with self._lock:
            conn = self._conn
        if conn is not None:
            conn.sendall(data)

    def wait_connected(self, timeout=5.0) -> bool:
        """Block until the client's TCP connection has been accepted."""
        return self._connected.wait(timeout)

    def stop(self):
        self._stop.set()
        with self._lock:
            conn = self._conn
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


class _Runner:
    """Runs ``client.run`` on a background thread, collecting events + error."""

    def __init__(self, client):
        self._client = client
        self._events = []
        self.error = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            self._client.run(self._on_event)
        except Exception as e:  # captured for the test to assert on
            self.error = e

    def _on_event(self, ev):
        with self._lock:
            self._events.append(ev)

    def start(self):
        self._thread.start()

    def wait_for(self, n, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._events) >= n:
                    return True
            time.sleep(0.01)
        return False

    def join(self, timeout=5.0):
        self._thread.join(timeout)

    @property
    def events(self):
        with self._lock:
            return list(self._events)

    def kinds(self):
        return [e.kind for e in self.events]

    def by_kind(self, kind):
        return [e for e in self.events if e.kind == kind]


@pytest.fixture
def server():
    srv = None

    def _make(**kwargs):
        nonlocal srv
        srv = MockStreamServer(**kwargs)
        srv.start()
        return srv

    yield _make
    if srv is not None:
        srv.stop()


def _connect(srv):
    client = WiCANLiveStreamClient(srv.host, port=srv.port, connect_timeout_s=2.0)
    hello = client.connect()
    return client, hello


class TestConnectBanner:
    def test_banner_ok_returns_hello(self, server):
        srv = server(initial=[b"#idle\n"])
        client, hello = _connect(srv)
        try:
            assert hello.kind == KIND_HELLO
            assert hello.fw == "test1.0"
        finally:
            client.stop()

    def test_wrong_banner_is_unsupported(self, server):
        srv = server(banner=b"WELCOME totally-not-ncdl\n")
        client = WiCANLiveStreamClient(srv.host, port=srv.port, connect_timeout_s=2.0)
        with pytest.raises(WiCANStreamUnsupported):
            client.connect()

    def test_connection_refused_is_unsupported(self):
        # A port nobody is listening on -> connect refused -> unsupported.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        client = WiCANLiveStreamClient(
            "127.0.0.1", port=closed_port, connect_timeout_s=1.0
        )
        with pytest.raises(WiCANStreamUnsupported):
            client.connect()

    def test_silent_accept_is_unsupported_within_timeout(self, server):
        # A TCP peer that accepts but never sends the banner (some other service
        # on the port, or a wedged accept task): connect() must give up with
        # WiCANStreamUnsupported within roughly the connect timeout, not hang.
        srv = server(banner=None)
        client = WiCANLiveStreamClient(srv.host, port=srv.port, connect_timeout_s=0.5)
        t0 = time.monotonic()
        with pytest.raises(WiCANStreamUnsupported):
            client.connect()
        assert time.monotonic() - t0 < 2.0


class TestEventFlow:
    def test_idle_session_header_rows(self, server):
        srv = server(
            initial=[
                b"#idle\n",
                b"#session file=trip.csv cols=3\n",
                b"time,rpm,map\n",
                b"1,800,20\n",
                b"2,850,22\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            # hello + idle + session + header + 2 rows = 6 events
            assert runner.wait_for(6), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()

        assert runner.kinds()[:6] == [
            KIND_HELLO,
            KIND_IDLE,
            KIND_SESSION,
            KIND_HEADER,
            KIND_ROW,
            KIND_ROW,
        ]
        session = runner.by_kind(KIND_SESSION)[0]
        assert session.file == "trip.csv"
        assert session.cols == 3
        assert runner.by_kind(KIND_HEADER)[0].line == "time,rpm,map"
        assert [e.line for e in runner.by_kind(KIND_ROW)] == ["1,800,20", "2,850,22"]

    def test_mid_session_join_replays_session_and_header(self, server):
        # A joiner gets #session + header on connect (no #idle first).
        srv = server(
            initial=[
                b"#session file=live.csv cols=2\n",
                b"t,v\n",
                b"0,1\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(4), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        assert runner.kinds()[:4] == [KIND_HELLO, KIND_SESSION, KIND_HEADER, KIND_ROW]
        assert runner.by_kind(KIND_HEADER)[0].line == "t,v"

    def test_nohdr_join_delivers_rows_without_header(self, server):
        srv = server(
            initial=[
                b"#session file=live.csv cols=2\n",
                b"#nohdr\n",
                b"9,9\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(4), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        # After #nohdr the next non-# line is a ROW, not a header.
        assert runner.kinds()[:4] == [KIND_HELLO, KIND_SESSION, KIND_NOHDR, KIND_ROW]
        assert runner.by_kind(KIND_HEADER) == []
        assert runner.by_kind(KIND_ROW)[0].line == "9,9"

    def test_drop_and_close_events(self, server):
        srv = server(
            initial=[
                b"#session file=t.csv cols=1\n",
                b"x\n",
                b"1\n",
                b"#drop 3\n",
                b"5\n",
                b"#close\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(7), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        assert runner.by_kind(KIND_DROP)[0].count == 3
        assert len(runner.by_kind(KIND_CLOSE)) == 1
        # The row after #drop is still a row (not treated as a header).
        assert [e.line for e in runner.by_kind(KIND_ROW)] == ["1", "5"]

    def test_unknown_control_line_is_ignored(self, server):
        srv = server(
            initial=[
                b"#session file=t.csv cols=1\n",
                b"x\n",
                b"#wibble whatever\n",
                b"1\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(4), f"only saw {runner.kinds()}"
            time.sleep(0.1)  # give any stray event a chance to arrive
        finally:
            client.stop()
            runner.join()
        # hello, session, header, row — the unknown #line produced no event and
        # did not disturb the header/row state machine.
        assert runner.kinds() == [KIND_HELLO, KIND_SESSION, KIND_HEADER, KIND_ROW]
        assert runner.by_kind(KIND_ROW)[0].line == "1"

    def test_session_rotation_after_close(self, server):
        # Rotation via the documented path: #close then a fresh #session+header
        # on the SAME socket. The header must re-arm across #close (session 2's
        # header is a HEADER, not a ROW).
        srv = server(
            initial=[
                b"#idle\n",
                b"#session file=a.csv cols=1\n",
                b"x\n",
                b"1\n",
                b"#close\n",
                b"#session file=b.csv cols=1\n",
                b"y\n",
                b"2\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(8), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        assert runner.kinds()[:8] == [
            KIND_HELLO,
            KIND_IDLE,
            KIND_SESSION,
            KIND_HEADER,
            KIND_ROW,
            KIND_CLOSE,
            KIND_SESSION,
            KIND_HEADER,
        ]
        headers = runner.by_kind(KIND_HEADER)
        assert [h.line for h in headers] == ["x", "y"]
        assert [e.line for e in runner.by_kind(KIND_ROW)] == ["1", "2"]

    def test_session_rotation_without_close(self, server):
        # Firmware may rotate with a bare #session (no #close guaranteed in
        # between). The header expectation must re-arm on the new #session.
        srv = server(
            initial=[
                b"#session file=a.csv cols=1\n",
                b"x\n",
                b"1\n",
                b"#session file=b.csv cols=1\n",
                b"y\n",
                b"2\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(7), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        assert runner.kinds()[:7] == [
            KIND_HELLO,
            KIND_SESSION,
            KIND_HEADER,
            KIND_ROW,
            KIND_SESSION,
            KIND_HEADER,
            KIND_ROW,
        ]
        assert [h.line for h in runner.by_kind(KIND_HEADER)] == ["x", "y"]
        assert [e.line for e in runner.by_kind(KIND_ROW)] == ["1", "2"]

    def test_drop_before_session(self, server):
        # A #drop can precede a #session (ring filled on a control line). It must
        # not disturb the following session's header/row framing.
        srv = server(
            initial=[
                b"#drop 2\n",
                b"#session file=t.csv cols=1\n",
                b"h\n",
                b"1\n",
            ]
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        try:
            assert runner.wait_for(5), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        assert runner.kinds()[:5] == [
            KIND_HELLO,
            KIND_DROP,
            KIND_SESSION,
            KIND_HEADER,
            KIND_ROW,
        ]
        assert runner.by_kind(KIND_DROP)[0].count == 2
        assert runner.by_kind(KIND_HEADER)[0].line == "h"
        assert runner.by_kind(KIND_ROW)[0].line == "1"

    def test_line_reassembly_across_recv_boundaries(self, server):
        # The real device (and TCP) split lines at arbitrary byte boundaries.
        # A row delivered in fragments, single bytes, and coalesced with the
        # next line must all reassemble to the correct whole rows.
        srv = server(initial=[b"#session file=t.csv cols=2\n", b"t,v\n"])
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        assert runner.wait_for(3), f"only saw {runner.kinds()}"  # hello,session,header
        try:
            # Fragment observed alone (pause > one stop-poll so the client takes
            # the partial-line branch), then the tail completes the row.
            srv.feed(b"1,8")
            time.sleep(0.7)
            srv.feed(b"00\n")
            # Single-byte drips.
            for b in b"2,850\n":
                srv.feed(bytes([b]))
                time.sleep(0.01)
            # Two rows coalesced into one segment, plus a carried-over tail.
            srv.feed(b"3,900\n4,9")
            time.sleep(0.05)
            srv.feed(b"50\n")
            assert runner.wait_for(3 + 4), f"only saw {runner.kinds()}"
        finally:
            client.stop()
            runner.join()
        assert [e.line for e in runner.by_kind(KIND_ROW)] == [
            "1,800",
            "2,850",
            "3,900",
            "4,950",
        ]


class TestStopAndDisconnect:
    def test_stop_during_blocking_read_returns_promptly(self, server):
        # After #idle the server is silent; run() blocks in recv. stop() must
        # interrupt it and let run() return within roughly the poll interval.
        srv = server(initial=[b"#idle\n"])
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        assert runner.wait_for(2), f"only saw {runner.kinds()}"  # hello + idle
        t0 = time.monotonic()
        client.stop()
        runner.join(timeout=3)
        elapsed = time.monotonic() - t0
        assert not runner._thread.is_alive(), "run() did not return after stop()"
        assert runner.error is None, f"clean stop should not raise: {runner.error!r}"
        assert elapsed < 2.0, f"stop() took too long: {elapsed:.2f}s"

    def test_read_line_on_closed_socket_returns_eof(self, server):
        # Regression for the settimeout-outside-try race: a stop() closing the
        # socket between recvs must surface as _EOF (a clean stop), never a raw
        # OSError (WinError 10038) that a normal stop would mis-report as error.
        from src.ecu.wican_stream import _EOF

        srv = server()  # banner only — no buffered lines to drain first
        client, _hello = _connect(srv)
        assert client._rbuf == b""  # precondition: forces the recv path
        sock = client._sock
        client._sock = None
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        # settimeout on the now-closed socket used to raise before the try.
        assert client._read_line(sock) is _EOF
        client.stop()

    def test_unexpected_disconnect_raises(self, server):
        # Server drops the connection right after the rows -> run() must raise.
        srv = server(
            initial=[
                b"#session file=t.csv cols=1\n",
                b"x\n",
                b"1\n",
            ],
            close_after_initial=True,
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        runner.join(timeout=3)
        assert not runner._thread.is_alive(), "run() never returned"
        assert isinstance(
            runner.error, WiCANStreamError
        ), f"expected WiCANStreamError, got {runner.error!r}"
        client.stop()

    def test_torn_final_line_discarded_at_eof(self, server):
        # The device tears the last line (reset / WiFi drop mid-send). The
        # partial row must NOT be delivered (a truncated CSV row must never
        # reach the MLV-tailed file); complete lines before the tear still are;
        # run() raises WiCANStreamError.
        srv = server(
            initial=[
                b"#session file=t.csv cols=2\n",
                b"t,v\n",
                b"1,80",  # torn: no newline, then the server closes
            ],
            close_after_initial=True,
        )
        client, _hello = _connect(srv)
        runner = _Runner(client)
        runner.start()
        runner.join(timeout=3)
        assert not runner._thread.is_alive(), "run() never returned"
        assert isinstance(
            runner.error, WiCANStreamError
        ), f"expected WiCANStreamError, got {runner.error!r}"
        assert runner.by_kind(KIND_HEADER)[0].line == "t,v"  # complete line kept
        assert runner.by_kind(KIND_ROW) == []  # torn "1,80" never delivered
        client.stop()
