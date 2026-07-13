"""WiCAN live-datalog stream receiver — THE NCDLv1 pipeline (fw issue #3).

NC Flash live-tails the WiCAN's active wide-CSV datalog over WiFi
(MegaLogViewerHD-style): the device streams exactly the rows it writes to SD,
NC Flash receives them and hands them to a caller that appends to a growing
local ``.csv``. **NC Flash initiates**; the device only ever serves — the host
sends nothing on the wire.

Per the architecture rule "ONE pipeline copy" this module is the single copy of
the NCDLv1 line-read loop. It is deliberately isolated from the ECU/flash wire
stack: it MUST NOT import or touch :class:`~src.ecu.wican_transport.WiCANTransport`,
the SLCAN coexist port 35001, or any ``/datalog`` op. The live-trip lease
choreography around a stream (un-park, ``op=start&rotate&lease_ms``, re-park)
belongs to :class:`~src.ecu.wican_config.WiCANDatalogClient`, driven by the Qt
owner — nothing here reaches into that machinery: a 35002 connection is not
host-presence and never touches park/claim/leases.

Wire protocol ``NCDLv1`` (device -> host; host sends nothing), all lines
``\\n``-terminated, control lines start with ``#`` and unknown ``#`` lines are
ignored:

- ``#hello NCDLv1 fw=<git_version>`` — once, immediately on accept (validated by
  :meth:`WiCANLiveStreamClient.connect`, delivered as the first ``hello`` event).
- ``#idle`` — no CSV session is active yet.
- ``#session file=<basename> cols=<n>`` — a session is (already) active; the
  **next** non-``#`` line is the CSV header, and subsequent non-``#`` lines are
  rows.
- ``#nohdr`` — mid-session joiner whose header copy was unavailable; rows still
  flow, no header line precedes them (a later ``#session`` brings a full header).
- ``#drop <n>`` — n rows were dropped (device ring full); a gap, not an error.
- ``#close`` — the SD session closed; the socket stays open and ``#session``
  re-announces when the next one opens.

Headless: standard library only (``socket``/``threading``/``logging``). No
PySide6, no third-party imports — the Qt owner lives in
:mod:`src.ui.wican_live_datalog`.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .constants import NCDL_BANNER_PREFIX, WICAN_DATALOG_STREAM_PORT
from .exceptions import ECUError

logger = logging.getLogger(__name__)

# --- Event kinds delivered to the run() callback ---------------------------
KIND_HELLO = "hello"  # fw=<git version>
KIND_IDLE = "idle"  # no session active
KIND_SESSION = "session"  # file=<basename>, cols=<n>; header line follows
KIND_HEADER = "header"  # line=<CSV header>
KIND_NOHDR = "nohdr"  # mid-session join without a header copy
KIND_DROP = "drop"  # count=<rows dropped>
KIND_CLOSE = "close"  # SD session closed
KIND_ROW = "row"  # line=<raw CSV row>

#: Socket recv timeout for the run() loop. Bounds how long a blocking read can
#: hold before :meth:`WiCANLiveStreamClient.stop` (which shuts the socket down)
#: is observed — so stop() returns within roughly this window even when the
#: device is silent (a parked logger streams nothing, which is normal).
_STOP_POLL_INTERVAL_S = 0.5


class WiCANStreamError(ECUError):
    """The NCDLv1 live-datalog stream failed or disconnected unexpectedly.

    Subclasses :class:`~src.ecu.exceptions.ECUError` so failures flow through
    the unified ECU error handlers.
    """


class WiCANStreamUnsupported(WiCANStreamError):
    """The device does not speak NCDLv1 — firmware without the live stream.

    Raised by :meth:`WiCANLiveStreamClient.connect` when the connection is
    refused/times out or the ``#hello NCDLv1`` banner is missing/wrong. Callers
    soft-degrade (surface a "firmware without a live datalog stream" status),
    they do not treat it as a hard error.
    """


@dataclass(frozen=True)
class StreamEvent:
    """One decoded NCDLv1 event. ``kind`` is one of the ``KIND_*`` constants.

    Fields are populated per kind: ``fw`` (hello), ``file``/``cols`` (session),
    ``line`` (header/row, newline stripped), ``count`` (drop). Unused fields
    keep their empty defaults.
    """

    kind: str
    fw: str = ""
    file: str = ""
    cols: int = 0
    line: str = ""
    count: int = 0


class WiCANLiveStreamClient:
    """Blocking socket + line reader for one WiCAN live-datalog stream.

    Usage is single-threaded for :meth:`connect`/:meth:`run` (one owning worker
    thread), with :meth:`stop` safe to call from any thread to interrupt a
    blocking :meth:`run`::

        client = WiCANLiveStreamClient(host)
        client.connect()            # validates the NCDLv1 banner
        client.run(on_event)        # blocks, delivering StreamEvents
        # ... from another thread: client.stop()

    Also usable as a context manager (``__exit__`` calls :meth:`stop`).
    """

    def __init__(
        self,
        host: str,
        port: int = WICAN_DATALOG_STREAM_PORT,
        *,
        connect_timeout_s: float = 3.0,
        read_timeout_s: Optional[float] = None,
    ):
        """``read_timeout_s`` None (default) = never time out on silence — a
        parked logger legitimately streams nothing for long stretches. When set,
        that many seconds with no byte received raises :class:`WiCANStreamError`.
        """
        self.host = host
        self.port = port
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rbuf = bytearray()
        self._hello: Optional[StreamEvent] = None
        self._expect_header = False

    # --- lifecycle -----------------------------------------------------------

    def connect(self) -> StreamEvent:
        """Open the socket and validate the ``#hello NCDLv1`` banner.

        Returns the parsed ``hello`` :class:`StreamEvent` (also re-delivered as
        the first event by :meth:`run`). Raises :class:`WiCANStreamUnsupported`
        when the connection is refused/times out or the banner is missing/wrong;
        the socket is closed before raising.
        """
        with self._lock:
            if self._sock is not None:
                raise WiCANStreamError("already connected")
            if self._stop.is_set():
                raise WiCANStreamError("client stopped")
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self._connect_timeout_s
            )
        except OSError as exc:
            raise WiCANStreamUnsupported(
                f"WiCAN live-datalog stream at {self.host}:{self.port} is "
                f"unavailable ({exc}); firmware without the live stream?"
            ) from exc
        with self._lock:
            if self._stop.is_set():
                _quiet_close(sock)
                raise WiCANStreamError("client stopped")
            self._sock = sock

        banner = self._read_banner(sock)
        if banner is None or not banner.startswith(NCDL_BANNER_PREFIX):
            self.stop()
            raise WiCANStreamUnsupported(
                f"WiCAN at {self.host}:{self.port} did not send the NCDLv1 "
                f"banner (got {banner!r}); firmware without the live stream?"
            )
        self._hello = StreamEvent(KIND_HELLO, fw=_parse_fw(banner))
        return self._hello

    def run(self, on_event: Callable[[StreamEvent], None]) -> None:
        """Blocking read loop: decode lines and deliver :class:`StreamEvent`s.

        Delivers the ``hello`` event first, then events as lines arrive.
        Returns cleanly once :meth:`stop` is requested. Raises
        :class:`WiCANStreamError` if the device closes the stream unexpectedly
        (EOF without a stop request) or the idle ``read_timeout_s`` elapses.
        ``on_event`` runs on the calling (run) thread.
        """
        with self._lock:
            sock = self._sock
        if sock is None:
            raise WiCANStreamError("run() called before connect()")

        if self._hello is not None:
            on_event(self._hello)
            self._hello = None

        idle_deadline = self._new_idle_deadline()
        while not self._stop.is_set():
            line = self._read_line(sock)
            if line is _TIMEOUT:
                if idle_deadline is not None and time.monotonic() > idle_deadline:
                    raise WiCANStreamError(
                        f"no live-datalog data within {self._read_timeout_s}s"
                    )
                continue
            if line is _EOF:
                if self._stop.is_set():
                    return
                raise WiCANStreamError(
                    "WiCAN closed the live-datalog stream unexpectedly"
                )
            idle_deadline = self._new_idle_deadline()
            self._dispatch(line, on_event)

    def stop(self) -> None:
        """Stop the stream (idempotent, thread-safe).

        Sets the stop flag and shuts down + closes the socket, which interrupts
        an in-flight blocking :meth:`run` recv so it returns promptly. Safe to
        call repeatedly and from any thread.
        """
        self._stop.set()
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            _quiet_close(sock)

    def __enter__(self) -> "WiCANLiveStreamClient":
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False

    # --- internals -----------------------------------------------------------

    def _new_idle_deadline(self) -> Optional[float]:
        if self._read_timeout_s is None:
            return None
        return time.monotonic() + self._read_timeout_s

    def _read_banner(self, sock: socket.socket) -> Optional[str]:
        """Read one line within the connect timeout. None on timeout/EOF."""
        deadline = time.monotonic() + self._connect_timeout_s
        while True:
            nl = self._rbuf.find(b"\n")
            if nl != -1:
                return self._take_line(nl)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                # settimeout is inside the try: a concurrent stop() can close
                # the socket between recvs, and settimeout on a closed socket
                # raises OSError (WinError 10038) — treat it as no banner.
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
            except (socket.timeout, OSError):
                return None
            if chunk == b"":
                return None
            self._rbuf.extend(chunk)

    def _read_line(self, sock: socket.socket):
        """Return the next complete line (str), or ``_TIMEOUT`` / ``_EOF``.

        Drains complete lines already buffered before issuing a recv, so a
        multi-line chunk is fully consumed. On a bare recv timeout with no
        complete line yet, returns ``_TIMEOUT`` (the run loop polls stop).
        """
        nl = self._rbuf.find(b"\n")
        if nl != -1:
            return self._take_line(nl)
        try:
            # settimeout is inside the try: a stop() racing between recvs closes
            # the socket, and settimeout on a closed socket raises OSError
            # (WinError 10038) — treat it as EOF so run() sees the stop flag and
            # returns cleanly rather than surfacing a stop as an error.
            sock.settimeout(_STOP_POLL_INTERVAL_S)
            chunk = sock.recv(4096)
        except socket.timeout:
            return _TIMEOUT
        except OSError:
            # Socket closed under us (stop() from another thread) — the run
            # loop re-checks the stop flag and returns cleanly.
            return _EOF
        if chunk == b"":
            return _EOF
        self._rbuf.extend(chunk)
        nl = self._rbuf.find(b"\n")
        if nl != -1:
            return self._take_line(nl)
        return _TIMEOUT  # partial line — come back after the next poll

    def _take_line(self, nl: int) -> str:
        raw = bytes(self._rbuf[:nl])
        del self._rbuf[: nl + 1]
        return raw.decode("utf-8", "replace").rstrip("\r")

    def _dispatch(self, line: str, on_event: Callable[[StreamEvent], None]) -> None:
        if line.startswith("#"):
            self._dispatch_control(line, on_event)
            return
        # A non-# line is the header (first line after #session) else a row.
        if self._expect_header:
            self._expect_header = False
            on_event(StreamEvent(KIND_HEADER, line=line))
        else:
            on_event(StreamEvent(KIND_ROW, line=line))

    def _dispatch_control(
        self, line: str, on_event: Callable[[StreamEvent], None]
    ) -> None:
        parts = line[1:].split()
        if not parts:
            logger.debug("Ignoring empty NCDLv1 control line")
            return
        tag = parts[0]
        if tag == "idle":
            self._expect_header = False
            on_event(StreamEvent(KIND_IDLE))
        elif tag == "session":
            fields = _parse_kv(parts[1:])
            try:
                cols = int(fields.get("cols", "0"))
            except ValueError:
                cols = 0
            self._expect_header = True
            on_event(StreamEvent(KIND_SESSION, file=fields.get("file", ""), cols=cols))
        elif tag == "nohdr":
            self._expect_header = False
            on_event(StreamEvent(KIND_NOHDR))
        elif tag == "drop":
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            on_event(StreamEvent(KIND_DROP, count=count))
        elif tag == "close":
            self._expect_header = False
            on_event(StreamEvent(KIND_CLOSE))
        elif tag == "hello":
            on_event(StreamEvent(KIND_HELLO, fw=_parse_fw(line)))
        else:
            logger.debug("Ignoring unknown NCDLv1 control line: %r", line)


# --- module helpers --------------------------------------------------------

# Unique sentinels for _read_line so a real (possibly empty) line is never
# ambiguous with "no line yet" / "end of stream".
_TIMEOUT = object()
_EOF = object()


def _quiet_close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _parse_kv(tokens) -> dict:
    out = {}
    for tok in tokens:
        key, sep, value = tok.partition("=")
        if sep:
            out[key] = value
    return out


def _parse_fw(banner: str) -> str:
    for tok in banner.split():
        if tok.startswith("fw="):
            return tok[3:]
    return ""
