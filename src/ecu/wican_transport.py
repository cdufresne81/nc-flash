"""
WiCAN (SLCAN-over-TCP) ECU transport.

Implements :class:`~src.ecu.transport.EcuTransport` on top of a WiCAN
adapter exposed as an SLCAN TCP socket. The wire stack, top to bottom:

    UDS payload (SID + data)            <- the EcuTransport seam
      ISO-TP segmentation/reassembly    <- src.ecu.isotp.IsoTpSession
        SLCAN ASCII frames              <- src.ecu.slcan codec
          TCP socket                    <- this module

This transport owns the TCP socket lifecycle (:meth:`open` / :meth:`close`)
and drives a persistent :class:`IsoTpSession` whose raw-frame I/O is wired to
the socket: :meth:`send_frame` SLCAN-encodes a CAN frame and ``sendall``s it,
and :meth:`recv_frame` reads bytes (honouring ``timeout_ms`` via a selectable
socket) and feeds them to an :class:`SlcanFrameStream` parser until a complete
CAN frame is available.

DEVICE-SIDE PREREQUISITE (wican-fw issue #476):
    The WiCAN firmware does NOT forward CAN traffic over the SLCAN TCP socket
    unless it is explicitly configured to. Before this transport can exchange
    any frames, the user MUST, in the WiCAN web UI:

        1. Enable "monitoring" (a.k.a. the CAN/automation pass-through), and
        2. Configure the SLCAN protocol and TCP port on the same socket this
           transport connects to.

    Without that, the channel opens cleanly (``O`` is acked) but NO frames ever
    flow — sends are silently dropped and every receive times out. If you see
    clean ``open()`` followed by universal receive timeouts, this firmware-side
    configuration is the first thing to check.

This module is a core transport module and MUST remain headless: it imports
only the standard library and sibling core modules — never PySide6.
"""

from __future__ import annotations

import logging
import select
import socket
import time
from typing import Optional, Tuple

from .constants import CAN_REQUEST_ID, CAN_RESPONSE_ID
from .exceptions import ECUError
from .isotp import IsoTpError, IsoTpSession, IsoTpTimeout
from .slcan import (
    BEL,
    BITRATE_500K,
    CLOSE,
    OPEN,
    SlcanError,
    SlcanFrameStream,
    encode_data_frame,
)
from .transport import EcuTransport

logger = logging.getLogger(__name__)


#: Default TCP connect timeout (ms) when none is supplied.
DEFAULT_CONNECT_TIMEOUT_MS = 5000

#: Size of each socket ``recv`` chunk while waiting for a frame.
_RECV_CHUNK = 4096

#: Throwaway warm-up payload sent right after the SLCAN channel opens. A
#: TesterPresent (0x3E) with the suppress-positive-response sub-function (0x80).
#: The 0x80 sub-function makes this frame easy to tell apart from a real
#: TesterPresent (0x3E 0x00) request. Whether the ECU honours the suppress bit
#: (no reply) or rejects it (the NC ECU answers ``7F 3E 12``), any reply is
#: consumed by the post-prime drain. See :meth:`WiCANTransport._prime_channel`.
_PRIME_PAYLOAD = b"\x3e\x80"

#: Overall budget (ms) for emitting the one-frame warm-up.
_PRIME_TIMEOUT_MS = 1000

#: Silence window (ms) that marks the channel as drained after the warm-up: once
#: no frame arrives for this long, the warm-up's reply (if any) has been consumed.
_PRIME_QUIET_MS = 200

#: Hard cap on frames discarded while draining, so a chatty bus can't loop forever.
_PRIME_MAX_DRAIN_FRAMES = 64

#: ISO-TP Block Size advertised when receiving a multi-frame response over the
#: WiCAN. 0 means "send every Consecutive Frame in one block" — fine here because
#: STmin (below) already paces the burst, so we avoid the extra Flow-Control
#: round-trips a small block size would add on every 1 KB read.
DEFAULT_RX_BLOCK_SIZE = 0

#: ISO-TP STmin advertised when receiving — minimum separation between
#: Consecutive Frames (0x00..0x7F = milliseconds). The gateway must forward every
#: CAN frame over WiFi/TCP, and an unpaced burst can overflow its CAN->TCP buffer
#: and silently drop frames. The ORIGINAL fix paced this down (STmin=3): a 2026-06-20
#: sweep had STmin=0 drop at block 8/64, 1 at 40/64, 2 clean. But that overflow was
#: caused by Nagle batching on our socket — enabling :data:`TCP_NODELAY` (the default,
#: added 2026-06-21) eliminates it: a 2026-06-21 hardware sweep read STmin=0 with only
#: 1/48 drops, and a full 1 MB read completed in 338.7 s (vs ~948 s at STmin=3) — a 3x
#: win. So the default is now **0** (fastest), relying on TCP_NODELAY + the N_Cr
#: fast-fail (:data:`DEFAULT_N_CR_MS`) + the idempotent per-block read retry to recover
#: the rare residual drop. Raise it only for a gateway/link where STmin=0 proves lossy
#: (reads are idempotent, so the cost of a drop is a re-read, never corruption).
DEFAULT_RX_STMIN = 0

#: ISO-TP N_Cr (ms) we apply when receiving a multi-frame response: the max wait
#: for the *next* Consecutive Frame once reassembly has begun. On this lossy
#: WiFi/gateway link a mid-message gap this long means a frame was dropped, so we
#: fail the block fast and let the idempotent read-retry re-request it — instead
#: of stalling the full per-block receive budget (was ~4 s) on every dropped
#: block. A clean read never hits this: at any usable STmin the ECU's frames
#: arrive milliseconds apart, far inside this window, so it only ever fires on a
#: genuine drop. Generous by default; tune down from a bench per-block trace.
#: Reads are idempotent, so even a false trigger only costs one harmless re-read.
DEFAULT_N_CR_MS = 500

#: ISO-TP outbound STmin FLOOR for multi-frame messages WE transmit (writes /
#: flash TransferData). Unlike :data:`DEFAULT_RX_STMIN` (which paces the ECU's
#: Consecutive Frames when we RECEIVE), this paces OUR Consecutive Frames when we
#: SEND. The ECU advertises STmin=0 in a programming session, so without a floor
#: the tool blasts a 1 KB TransferData block as ~146 back-to-back CFs and overruns
#: the WiCAN gateway's TCP->CAN forwarding buffer — frames are dropped inside the
#: gateway, the ECU's reassembly never completes, and it never answers (the
#: hardware-observed FULL FLASH failure: a 60 s timeout on SID 0x36 after the whole
#: block was sent). This is the exact mirror of the receive-side overflow
#: :data:`DEFAULT_RX_STMIN` already guards. A whole-millisecond floor is used (not
#: a 0xF1-0xF9 microsecond code) because Windows ``time.sleep`` granularity makes
#: sub-millisecond pacing unreliable. Pacing ONLY changes inter-frame timing within
#: one message — never the payload, never a resend — so the flash
#: no-mid-stream-resend (anti-brick) invariant holds. Reads are single-frame
#: requests, so this never affects them. Tune from a bench TX sweep.
DEFAULT_TX_STMIN = 3

#: NC Flash fast-read protocol (custom WiCAN firmware only). The command is one
#: line ``X<8 hex start><8 hex length>\r`` on the SLCAN socket; the firmware then
#: streams the raw ROM bytes straight back. See :meth:`WiCANTransport.fast_read`.
_FAST_READ_CMD = "X"

#: Sync preamble the firmware streams right before the first ROM byte (after it
#: suspends CAN forwarding). CAN frames already queued/in-flight toward the host
#: precede it, so the host discards everything up to and including this marker
#: before collecting ROM. Must match ``NCFLASH_FASTREAD_SYNC`` in the firmware.
#: The token cannot appear in SLCAN frames (hex 0-9A-F; types t/T/r/R).
_FAST_READ_SYNC = b"NCFRDATA"

#: Version ping: a fast-read at this sentinel start address makes the firmware
#: stream a fixed ``NCFRv<rev>`` build marker WITHOUT touching CAN, so the host
#: can confirm which fast-read build is live before a read. Must match
#: ``NCFLASH_FASTREAD_PING_ADDR`` / ``NCFLASH_FASTREAD_VERSION`` in the firmware.
#: Like the sync token, ``NCFRv`` cannot occur in SLCAN frames, so the host locks
#: onto it even amid live CAN traffic (the ping path streams no sync preamble).
_FAST_READ_PING_ADDR = 0xFFFFFFFE
_FAST_READ_VERSION_PREFIX = b"NCFRv"

#: Cap on buffered pre-sync bytes kept while hunting for the marker, so live CAN
#: traffic before the preamble can't grow memory without bound.
_FAST_READ_MAX_PRESTREAM = 1 << 20

#: Max bytes per single fast-read command. The firmware's TX/WiFi path degrades
#: under very long single-command streaming (a sustained 1 MB stream stalls
#: ~880 KB in), so the host splits large reads into chunks this size. Each chunk
#: is a fresh command (fresh CAN-forwarding suspend/resume) that resets firmware
#: state; 128 KB is well under the observed failure threshold and verified
#: byte-perfect, while the per-chunk command overhead stays negligible.
_FAST_READ_CHUNK = 128 * 1024

#: Default overall budget (ms) for a streamed fast read (a full 1 MB lands in
#: ~60 s; this leaves generous margin for a slower link before declaring a stall).
FAST_READ_TIMEOUT_MS = 180000

#: SD-staged flash (Option B WRITE). The firmware drives the ECU program sequence
#: locally over CAN and streams newline-delimited ASCII progress markers back over
#: the SAME socket the fast-read uses. Command: ``W<mode><staged_name>\r`` where
#: mode is 'L' (live flash) or 'D' (dry-run, no ECU write). Must match
#: ``NCFLASH_FASTWRITE_CMD`` and the markers in the firmware (ncflash_fastwrite.c).
_FAST_WRITE_CMD = "W"
_FAST_WRITE_SYNC = b"NCFWSYNC"  #: streamed once after the firmware takes the bus
_FAST_WRITE_DONE = b"NCFWDONE"  #: terminal success
_FAST_WRITE_PROG = b"NCFWPROG"  #: per-N-blocks: ``NCFWPROG <done>/<total>``
_FAST_WRITE_ERR = b"FWERR"  #: terminal failure: ``FWERR a=.. st=.. nrc=..``

#: Overall budget (ms) for a streamed flash. A full ~1 MB program at the ECU's
#: per-block rate runs a few minutes; this leaves wide margin (the firmware owns
#: the flash regardless, so this only bounds how long the host observes).
FAST_WRITE_TIMEOUT_MS = 600000

#: Idle/heartbeat (ms): no bytes for this long means the firmware is dead, not
#: merely slow. Generous because an ECU erase can sit silent for several seconds.
_FAST_WRITE_IDLE_MS = 30000


class WiCANError(ECUError):
    """Raised on WiCAN socket or SLCAN-channel failures.

    Subclasses :class:`~src.ecu.exceptions.ECUError` so it is caught by the
    same unified handlers as J2534 and UDS errors.
    """

    pass


class WiCANTransport(EcuTransport):
    """:class:`EcuTransport` backed by a WiCAN SLCAN-over-TCP socket.

    Owns the TCP socket and an :class:`IsoTpSession` bound to it. Callers work
    purely in UDS-payload bytes; this class adds ISO-TP framing, SLCAN ASCII
    encoding, and TCP I/O underneath.

    Lifecycle: unlike :class:`~src.ecu.transport.J2534Transport`, this transport
    DOES own its link. :meth:`open` connects the socket and brings up the SLCAN
    channel (close-then-open + 500 kbps); :meth:`close` tears it down. Both are
    safe to call more than once.
    """

    def __init__(
        self,
        host: str,
        port: int,
        tx_id: int = CAN_REQUEST_ID,
        rx_id: int = CAN_RESPONSE_ID,
        connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
        padding: int = 0x00,
        rx_block_size: int = DEFAULT_RX_BLOCK_SIZE,
        rx_stmin: int = DEFAULT_RX_STMIN,
        n_cr_ms: Optional[int] = DEFAULT_N_CR_MS,
        tcp_nodelay: bool = True,
        so_rcvbuf: Optional[int] = None,
        tx_stmin: int = DEFAULT_TX_STMIN,
    ):
        """
        Args:
            host: WiCAN adapter hostname/IP.
            port: TCP port of the WiCAN SLCAN socket.
            tx_id: CAN arbitration ID we transmit on (tester, default 0x7E0).
            rx_id: CAN arbitration ID we accept frames from (ECU, default 0x7E8).
            connect_timeout_ms: TCP connect timeout in milliseconds.
            padding: Byte used to pad CAN frames to 8 bytes (default 0x00).
            rx_block_size: ISO-TP Block Size we advertise when *receiving* a
                multi-frame response — how many Consecutive Frames the ECU may
                send before it must wait for our next Flow Control. Unlike a
                wired J2534 link (which can take the whole burst at BS=0), the
                WiCAN must forward every CAN frame over WiFi/TCP; an unbounded
                burst overflows its CAN->TCP buffer and silently drops frames
                (observed on hardware: ROM-read block responses lost mid-stream).
                A bounded block caps the burst so the gateway keeps up. See
                :data:`DEFAULT_RX_BLOCK_SIZE`.
            rx_stmin: ISO-TP STmin we advertise when receiving — the minimum
                separation the ECU must leave between Consecutive Frames. A small
                value paces the burst down to the gateway's forwarding rate
                without extra Flow-Control round-trips. See
                :data:`DEFAULT_RX_STMIN`.
            n_cr_ms: ISO-TP N_Cr (ms) — the max wait for the next Consecutive
                Frame mid-message before declaring a dropped frame and failing
                fast (so the read-retry re-requests immediately rather than
                stalling the receive budget). ``None`` disables it. See
                :data:`DEFAULT_N_CR_MS`.
            tcp_nodelay: Disable Nagle on the socket so small ISO-TP frames are
                sent immediately (lower request/response latency). Default True.
            so_rcvbuf: If set, request this socket receive-buffer size (bytes) so
                a fast Consecutive-Frame burst is less likely to be dropped
                before we read it. ``None`` leaves the OS default.
            tx_stmin: Outbound STmin floor (ms) for multi-frame messages WE send
                (write/flash TransferData) — the tool never sends Consecutive
                Frames closer together than this even when the ECU's Flow Control
                advertises STmin=0, capping the TCP->CAN burst the gateway must
                absorb. See :data:`DEFAULT_TX_STMIN`. Does not affect reads
                (single-frame requests) or the J2534 path.
        """
        self._host = host
        self._port = port
        self._tx_id = tx_id
        self._rx_id = rx_id
        self._connect_timeout_ms = connect_timeout_ms
        self._padding = padding
        self._rx_block_size = rx_block_size
        self._rx_stmin = rx_stmin
        self._n_cr_ms = n_cr_ms
        self._tcp_nodelay = tcp_nodelay
        self._so_rcvbuf = so_rcvbuf
        self._tx_stmin = tx_stmin

        self._sock: Optional[socket.socket] = None
        self._stream = SlcanFrameStream()
        # Decoded frames parsed ahead of what the current recv consumed are
        # buffered here so no frame is lost across recv_frame calls.
        self._frame_buffer: list[Tuple[int, bytes]] = []
        self._session = IsoTpSession(
            tx_id=tx_id,
            rx_id=rx_id,
            send_frame=self._send_frame,
            recv_frame=self._recv_frame,
            padding=padding,
            rx_block_size=rx_block_size,
            rx_stmin=rx_stmin,
            n_cr_ms=n_cr_ms,
            tx_stmin=tx_stmin,
        )

    # --- lifecycle ---

    def open(self) -> None:
        """Connect the TCP socket and bring up the SLCAN CAN channel.

        Performs, in order: TCP connect, close any stale channel (``C``), set
        the bus to 500 kbps (``S6``), then open the channel (``O``). Adapter
        acks (bare CR ok / BEL error) are drained between commands; a BEL ack
        for the bitrate or open command raises :class:`WiCANError`.

        Idempotent: calling :meth:`open` on an already-open transport is a
        no-op.

        Raises:
            WiCANError: If the TCP connection or SLCAN bring-up fails.
        """
        if self._sock is not None:
            return

        try:
            sock = socket.create_connection(
                (self._host, self._port),
                timeout=self._connect_timeout_ms / 1000.0,
            )
        except OSError as exc:
            raise WiCANError(
                f"Failed to connect to WiCAN at {self._host}:{self._port}: {exc}"
            ) from exc

        # Latency/throughput tuning for the request/response ISO-TP traffic:
        # disable Nagle so a small frame isn't held back waiting to coalesce,
        # and (optionally) enlarge the receive buffer so a fast Consecutive-Frame
        # burst is less likely to be dropped before we read it. Best-effort — a
        # platform that rejects an option must not fail an otherwise-good open().
        if self._tcp_nodelay:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:  # pragma: no cover - platform-dependent
                logger.debug("WiCAN TCP_NODELAY not set (ignored): %s", exc)
        if self._so_rcvbuf is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._so_rcvbuf)
            except OSError as exc:  # pragma: no cover - platform-dependent
                logger.debug("WiCAN SO_RCVBUF not set (ignored): %s", exc)

        # Non-blocking I/O so receive can honour per-call timeouts via select.
        sock.setblocking(False)
        self._sock = sock
        self._stream.reset()
        self._frame_buffer.clear()

        try:
            # Close any channel left open by a previous session (tolerate a
            # BEL here: closing an already-closed channel is harmless).
            self._send_raw(CLOSE)
            self._drain_acks(timeout_ms=200)

            # Set 500 kbps (NC ECU bus speed) and open the channel.
            self._send_command_checked(BITRATE_500K, "set bitrate (S6)")
            self._send_command_checked(OPEN, "open channel (O)")
        except Exception:
            # Bring-up failed — do not leak the socket.
            self._close_socket()
            raise

        # The adapter drops the very first frame after open() — send a throwaway
        # so the caller's first real request is not the casualty (see method).
        self._prime_channel()

        logger.info(
            "WiCAN SLCAN channel up on %s:%d (tx=0x%X rx=0x%X)",
            self._host,
            self._port,
            self._tx_id,
            self._rx_id,
        )

    def _prime_channel(self) -> None:
        """Emit one throwaway CAN frame to wake the freshly-opened channel.

        HARDWARE-OBSERVED (WiCAN PRO, 2026-06-20): the very first CAN frame sent
        immediately after the SLCAN ``O`` (open) ack is reliably lost — the
        adapter's CAN peripheral has not finished coming up, so that first frame
        races the bring-up and never reaches the bus. Under ISO-TP this makes the
        first *real* request hang for its entire receive timeout (~60 s) with no
        answer.

        Sending one disposable frame here absorbs that cold-start loss: this is
        the frame that gets dropped, so by the time the caller issues a real
        request the channel is hot. The payload is a TesterPresent with
        suppress-positive-response (``3E 80``) — a harmless keep-alive a real ECU
        consumes silently, leaving nothing to receive. We therefore do NOT read
        anything back and do NOT touch the RX buffers, so a frame that
        legitimately raced the handshake ack is preserved.

        After sending, the ECU's reply to the warm-up (a positive TesterPresent
        ack, or on the NC ECU a ``7F 3E 12`` rejection of the suppress-positive-
        response sub-function) is drained here so it cannot masquerade as the
        answer to the caller's first real request.

        Best-effort: any error is logged and swallowed. A socket that is genuinely
        dead will surface on the first real send; priming must never fail an
        otherwise-good :meth:`open`.
        """
        try:
            self._session.send(_PRIME_PAYLOAD, _PRIME_TIMEOUT_MS)
        except (WiCANError, IsoTpError, SlcanError, OSError) as exc:  # pragma: no cover
            logger.debug("WiCAN channel prime send failed (ignored): %s", exc)
            return
        self._drain_frames()
        logger.debug("WiCAN channel primed and drained")

    def _drain_frames(self, quiet_ms: int = _PRIME_QUIET_MS) -> None:
        """Discard any pending RX frames until the channel is quiet (best-effort).

        Clears the frame buffer and reads the socket, throwing every frame away,
        until no frame arrives for ``quiet_ms`` (or the discard cap is hit). Used
        right after the warm-up frame so the ECU's reply to it is consumed here
        instead of leaking into the caller's first real receive. Errors (a peer
        close mid-drain, etc.) just end the drain — it must never fail open().
        """
        self._frame_buffer.clear()
        for _ in range(_PRIME_MAX_DRAIN_FRAMES):
            try:
                if self._recv_frame(quiet_ms) is None:
                    return
            except (WiCANError, SlcanError, IsoTpError, OSError) as exc:
                logger.debug("WiCAN drain ended: %s", exc)
                return

    def close(self) -> None:
        """Close the SLCAN channel and the TCP socket.

        Error-tolerant: best-effort sends the SLCAN ``C`` close command, then
        closes the socket regardless of any failure. Safe to call when never
        opened or already closed.
        """
        if self._sock is None:
            return
        try:
            self._send_raw(CLOSE)
        except Exception as exc:  # pragma: no cover - best-effort teardown
            logger.debug("WiCAN close command failed (ignored): %s", exc)
        finally:
            self._close_socket()

    def _close_socket(self) -> None:
        """Close and forget the socket, swallowing errors."""
        sock = self._sock
        self._sock = None
        self._stream.reset()
        self._frame_buffer.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover - nothing actionable on close
                pass

    # --- EcuTransport message-level API ---

    def send_message(self, payload: bytes, timeout_ms: int) -> None:
        """Send one UDS payload, segmenting via ISO-TP over SLCAN/TCP.

        Args:
            payload: Complete UDS request (SID + data), no transport framing.
            timeout_ms: Overall budget for the whole (possibly multi-frame) send.

        Raises:
            WiCANError: If the transport is not open, or on socket/SLCAN/ISO-TP
                failure.
        """
        self._require_open()
        try:
            self._session.send(payload, timeout_ms)
        except (IsoTpError, SlcanError, OSError) as exc:
            raise WiCANError(f"WiCAN send failed: {exc}") from exc

    def receive_message(self, timeout_ms: int) -> Optional[bytes]:
        """Receive one reassembled UDS payload, or ``None`` on timeout.

        Args:
            timeout_ms: Overall budget for receiving the whole message.

        Returns:
            The reassembled UDS response bytes, or ``None`` if no complete
            message arrived within the timeout.

        Raises:
            WiCANError: If the transport is not open, or on a socket/SLCAN
                failure, OR on any ISO-TP protocol violation (sequence gap,
                malformed/short frame, overflow). A plain *timeout* returns
                ``None``; a corrupted response raises so it is never silently
                retried by the caller's response-pending loop.
        """
        self._require_open()
        try:
            return self._session.receive(timeout_ms)
        except IsoTpTimeout:
            # A genuine "no complete message in time". At the EcuTransport seam
            # a timeout is None, not an error. ONLY this subclass maps to None.
            return None
        except (IsoTpError, SlcanError, OSError) as exc:
            # Any non-timeout ISO-TP error means the bytes on the wire were
            # corrupt or out of sequence. Surface it loudly instead of letting
            # it masquerade as a benign timeout (which the UDS response-pending
            # loop would silently retry, hiding real corruption on the flash
            # path).
            raise WiCANError(f"WiCAN receive failed: {exc}") from exc

    def flush(self) -> None:
        """Drain any buffered/in-flight RX frames so the next receive is clean.

        Used by the read-retry path: after a block read times out (a frame the
        lossy WiFi/gateway dropped), stale frames from the failed attempt could
        otherwise corrupt the re-requested block's response. A no-op if the
        transport is not open.
        """
        if self._sock is None:
            return
        self._drain_frames()

    def fast_read(
        self,
        start: int,
        length: int,
        progress_cb=None,
        timeout_ms: int = FAST_READ_TIMEOUT_MS,
        chunk: Optional[int] = None,
    ) -> bytes:
        """Autonomous in-firmware ROM read (custom WiCAN firmware only).

        Sends one or more ``X<8 hex start><8 hex length>`` commands and reads the
        raw ROM bytes streamed straight back over the socket. The firmware issues
        every ``ReadMemoryByAddress(0x400)`` to the ECU locally over CAN (sub-ms
        round-trips) instead of one WiFi round-trip per block, so a 1 MB read
        approaches the CAN-bus ceiling (~60 s) instead of ~5 min.

        Reads longer than ``chunk`` (default :data:`_FAST_READ_CHUNK`) are split
        into back-to-back commands: the firmware's TX/WiFi path degrades under a
        very long single-command stream, so each chunk is a fresh command that
        resets firmware state. This is transparent to callers.

        The ECU MUST already be in an authenticated programming session (the
        caller does seed/key over the normal SLCAN path first); the firmware
        only replays reads, never authenticates or writes. On any firmware-side
        block failure the stream simply stops short, so this raises
        :class:`WiCANError` and the caller falls back to the per-block path.

        Args:
            start: ROM start address.
            length: number of bytes to read.
            progress_cb: optional ``cb(bytes_done, total)``.
            timeout_ms: per-chunk budget for the streamed read.
            chunk: max bytes per command (default :data:`_FAST_READ_CHUNK`).

        Returns:
            ``length`` bytes of ROM data.

        Raises:
            WiCANError: transport not open, socket error, or a short/stalled
                stream (a firmware-side read failure or a real drop).
        """
        self._require_open()
        chunk = chunk or _FAST_READ_CHUNK
        if length <= chunk:
            return self._fast_read_one(start, length, progress_cb, timeout_ms)

        out = bytearray()
        while len(out) < length:
            base = len(out)
            n = min(chunk, length - base)

            def sub_cb(done, _total, _base=base):
                if progress_cb:
                    progress_cb(_base + done, length)

            out.extend(self._fast_read_one(start + base, n, sub_cb, timeout_ms))
            if progress_cb:
                progress_cb(len(out), length)
        return bytes(out)

    def _fast_read_one(
        self,
        start: int,
        length: int,
        progress_cb=None,
        timeout_ms: int = FAST_READ_TIMEOUT_MS,
    ) -> bytes:
        """Run ONE fast-read command for ``length`` bytes and return them.

        The single-command primitive behind :meth:`fast_read`: sends the command,
        resyncs past leading CAN traffic onto the firmware's sync preamble, then
        collects exactly ``length`` ROM bytes.
        """
        # Clear any buffered SLCAN frames so the raw byte stream starts clean.
        self._drain_frames()
        cmd = f"{_FAST_READ_CMD}{start:08X}{length:08X}\r".encode("ascii")
        self._send_raw(cmd)

        sock = self._sock
        deadline = time.monotonic() + timeout_ms / 1000.0

        # Phase 1 — resync. The firmware suspends CAN forwarding and emits the
        # _FAST_READ_SYNC preamble before the first ROM byte, but CAN frames
        # already queued/in-flight toward us (TX queue + TCP send buffer) arrive
        # first. Read until the marker, then the ROM bytes are whatever follows.
        pre = bytearray()
        leftover = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WiCANError(
                    "fast read got no sync marker (firmware too old or no "
                    f"response); read {len(pre)} bytes of pre-stream"
                )
            try:
                readable, _, _ = select.select([sock], [], [], remaining)
            except OSError as exc:
                raise WiCANError(f"fast read select failed: {exc}") from exc
            if not readable:
                continue
            try:
                chunk = sock.recv(_RECV_CHUNK * 16)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WiCANError(f"fast read recv failed: {exc}") from exc
            if chunk == b"":
                raise WiCANError("socket closed by peer during fast read")
            # Scan only the newly-arrived bytes (plus a marker-1 overlap for a
            # marker split across recvs) so the hunt stays linear in total bytes
            # even if live CAN traffic runs long before the preamble.
            scan_from = max(0, len(pre) - (len(_FAST_READ_SYNC) - 1))
            pre.extend(chunk)
            idx = pre.find(_FAST_READ_SYNC, scan_from)
            if idx >= 0:
                leftover = bytes(pre[idx + len(_FAST_READ_SYNC) :])
                break
            # Bound the pre-stream buffer so live CAN traffic can't grow it
            # unbounded; keep only a tail long enough to catch a split marker.
            if len(pre) > _FAST_READ_MAX_PRESTREAM:
                del pre[: -len(_FAST_READ_SYNC)]

        # Phase 2 — collect exactly `length` ROM bytes (some already in leftover).
        buf = bytearray(leftover[:length])
        if progress_cb and buf:
            progress_cb(len(buf), length)
        while len(buf) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WiCANError(
                    f"fast read stalled at {len(buf)}/{length} bytes "
                    f"(firmware block failure or link drop){self._frerr_suffix(buf)}"
                )
            try:
                readable, _, _ = select.select([sock], [], [], remaining)
            except OSError as exc:
                raise WiCANError(f"fast read select failed: {exc}") from exc
            if not readable:
                # No data for a beat — the firmware may have aborted and streamed
                # a FRERR diagnostic instead of more ROM. Surface it immediately
                # rather than waiting out the whole budget.
                tail = self._frerr_suffix(buf)
                if tail:
                    raise WiCANError(
                        f"fast read aborted at {len(buf)}/{length} bytes{tail}"
                    )
                continue
            try:
                chunk = sock.recv(min(_RECV_CHUNK * 16, length - len(buf)))
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WiCANError(f"fast read recv failed: {exc}") from exc
            if chunk == b"":
                raise WiCANError(
                    f"socket closed by peer during fast read{self._frerr_suffix(buf)}"
                )
            buf.extend(chunk)
            if progress_cb:
                progress_cb(len(buf), length)
        return bytes(buf)

    #: Longest possible firmware FRERR line + slack; the line is the LAST thing
    #: the firmware streams before stopping, so only the buffer tail can hold it.
    _FRERR_TAIL = 256

    @classmethod
    def _frerr_suffix(cls, buf: bytearray) -> str:
        """Return the firmware's on-abort ``FRERR ...`` diagnostic if present.

        The fast-read firmware streams an ASCII ``FRERR a=... st=... ...`` line
        into the stream when it gives up on a block, always as the last bytes
        before it stops. Scanning just the tail (not the whole multi-hundred-KB
        buffer, which this is called on every idle beat) surfaces the cause.
        """
        tail = buf[-cls._FRERR_TAIL :]
        idx = tail.rfind(b"FRERR")
        if idx < 0:
            return ""
        end = tail.find(b"\n", idx)
        line = bytes(tail[idx : end if end >= 0 else len(tail)])
        return " | firmware: " + line.decode("ascii", "replace").strip()

    @classmethod
    def _fwerr_suffix(cls, buf: bytearray) -> str:
        """Return the firmware's on-abort ``FWERR ...`` diagnostic if present.

        The fastwrite firmware streams an ASCII ``FWERR a=.. st=.. nrc=..`` line
        as the last thing before it stops on a flash abort. Mirror of
        :meth:`_frerr_suffix` for the WRITE path.
        """
        tail = buf[-cls._FRERR_TAIL :]
        idx = tail.rfind(_FAST_WRITE_ERR)
        if idx < 0:
            return ""
        end = tail.find(b"\n", idx)
        line = bytes(tail[idx : end if end >= 0 else len(tail)])
        return " | firmware: " + line.decode("ascii", "replace").strip()

    def fast_write(
        self,
        staged_name: str,
        *,
        mode: str = "L",
        progress_cb=None,
        timeout_ms: int = FAST_WRITE_TIMEOUT_MS,
        idle_ms: int = _FAST_WRITE_IDLE_MS,
    ) -> None:
        """Trigger the firmware SD-staged flash and stream its progress markers.

        Sends ``W<mode><staged_name>\\r`` (mode ``'L'`` live flash, ``'D'`` dry-run
        — no ECU write), resyncs onto the ``NCFWSYNC`` marker (discarding any
        leading CAN frames, like :meth:`_fast_read_one`), then parses the
        newline-delimited progress lines the firmware streams back:

          * ``NCFWPROG <done>/<total>`` → ``progress_cb(done, total)``
          * ``NCFWDONE``               → returns normally (flash sequence done)
          * ``FWERR a=.. st=.. nrc=..``→ raises :class:`WiCANError`

        This only **observes** — the firmware owns the flash, so killing the
        socket does NOT stop it (there is deliberately no host-side abort). The
        WRITE path has no mid-stream resend; on ``FWERR`` the recovery is a whole
        restart-from-scratch by the caller. Raises :class:`WiCANError` on
        ``FWERR``, a stall (no bytes for ``idle_ms``), socket close, or the
        overall ``timeout_ms`` deadline.

        Args:
            staged_name: the SD leaf filename uploaded via ``/upload/sd/<name>``.
            mode: ``'L'`` to flash the ECU, ``'D'`` to dry-run (verify+walk only).
            progress_cb: called ``progress_cb(done, total)`` per ``NCFWPROG``.
        """
        if mode not in ("L", "D"):
            raise WiCANError(f"fast_write mode must be 'L' or 'D', got {mode!r}")

        self._drain_frames()
        cmd = f"{_FAST_WRITE_CMD}{mode}{staged_name}\r".encode("ascii")
        self._send_raw(cmd)

        sock = self._sock
        deadline = time.monotonic() + timeout_ms / 1000.0
        buf = bytearray()
        synced = False
        last_data = time.monotonic()

        while True:
            now = time.monotonic()
            if now > deadline:
                raise WiCANError(f"fast write timed out{self._fwerr_suffix(buf)}")
            if now - last_data > idle_ms / 1000.0:
                raise WiCANError(
                    f"fast write stalled (no data for {idle_ms} ms)"
                    f"{self._fwerr_suffix(buf)}"
                )
            try:
                readable, _, _ = select.select([sock], [], [], min(1.0, deadline - now))
            except OSError as exc:
                raise WiCANError(f"fast write select failed: {exc}") from exc
            if not readable:
                tail = self._fwerr_suffix(buf)
                if tail:
                    raise WiCANError(f"fast write aborted{tail}")
                continue
            try:
                chunk = sock.recv(_RECV_CHUNK * 16)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WiCANError(f"fast write recv failed: {exc}") from exc
            if chunk == b"":
                raise WiCANError(
                    f"socket closed by peer during fast write{self._fwerr_suffix(buf)}"
                )
            buf.extend(chunk)
            last_data = time.monotonic()

            # Resync onto NCFWSYNC, discarding leading CAN traffic.
            if not synced:
                idx = buf.find(_FAST_WRITE_SYNC)
                if idx < 0:
                    if len(buf) > _FAST_READ_MAX_PRESTREAM:
                        del buf[: -len(_FAST_WRITE_SYNC)]
                    continue
                del buf[: idx + len(_FAST_WRITE_SYNC)]
                synced = True

            # Parse complete newline-delimited marker lines.
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl]).strip()
                del buf[: nl + 1]
                if not line:
                    continue
                if line.startswith(_FAST_WRITE_ERR):
                    raise WiCANError(
                        "fast write failed | firmware: "
                        + line.decode("ascii", "replace")
                    )
                if line.startswith(_FAST_WRITE_DONE):
                    return
                if line.startswith(_FAST_WRITE_PROG) and progress_cb:
                    try:
                        done, total = line.split()[1].split(b"/")
                        progress_cb(int(done), int(total))
                    except (IndexError, ValueError):
                        pass  # malformed progress line — ignore, keep streaming

    def version_ping(self, window_ms: int = 3000) -> Optional[bytes]:
        """Probe which fast-read firmware is live (or that none is).

        Sends the fast-read command at the version sentinel address; the
        firmware answers with a fixed ``NCFRv<rev>`` build marker streamed
        WITHOUT touching CAN — so this confirms the running build (e.g. before a
        flash) with no side effects. Live CAN frames interleave with the marker,
        but ``NCFRv`` can't appear in an SLCAN frame, so we scan for it.

        Returns the marker line (e.g. ``b"NCFRv4"``) or ``None`` if none appears
        within ``window_ms`` (stock/old firmware with no fast-read support).
        """
        self._require_open()
        self._drain_frames()
        cmd = (
            f"{_FAST_READ_CMD}{_FAST_READ_PING_ADDR:08X}"
            f"{len(_FAST_READ_VERSION_PREFIX) + 2:08X}\r"
        ).encode("ascii")
        self._send_raw(cmd)

        sock = self._sock
        buf = bytearray()
        deadline = time.monotonic() + window_ms / 1000.0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                readable, _, _ = select.select([sock], [], [], remaining)
            except OSError as exc:
                raise WiCANError(f"version ping select failed: {exc}") from exc
            if not readable:
                continue
            try:
                chunk = sock.recv(_RECV_CHUNK)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WiCANError(f"version ping recv failed: {exc}") from exc
            if chunk == b"":
                break
            buf.extend(chunk)
            idx = buf.find(_FAST_READ_VERSION_PREFIX)
            end = buf.find(b"\n", idx) if idx >= 0 else -1
            if idx >= 0 and end >= 0:
                return bytes(buf[idx:end])
        idx = buf.find(_FAST_READ_VERSION_PREFIX)
        if idx >= 0:
            end = buf.find(b"\n", idx)
            return bytes(buf[idx : end if end >= 0 else len(buf)])
        return None

    @property
    def description(self) -> str:
        return f"WiCAN ({self._host}:{self._port})"

    @property
    def host(self) -> str:
        """The device IP/hostname (e.g. for the HTTP SD-upload endpoint)."""
        return self._host

    @property
    def port(self) -> int:
        """The SLCAN TCP port."""
        return self._port

    # --- raw frame I/O wired into the ISO-TP session ---

    def _send_frame(self, can_id: int, data: bytes) -> None:
        """ISO-TP TX hook: SLCAN-encode one CAN frame and send it."""
        line = encode_data_frame(can_id, bytes(data))
        self._send_raw(line)

    def _recv_frame(self, timeout_ms: int) -> Optional[Tuple[int, bytes]]:
        """ISO-TP RX hook: return the next CAN frame, or ``None`` on timeout.

        Reads from the socket (bounded by ``timeout_ms``), feeds bytes to the
        SLCAN stream parser, and returns the next decoded ``(can_id, data)``.
        Frames already parsed ahead are served from an internal buffer first.
        Frames whose id is not our ``rx_id`` are dropped early (the ISO-TP
        layer also filters, but dropping here avoids buffering cross-talk).
        """
        if self._frame_buffer:
            return self._frame_buffer.pop(0)

        sock = self._sock
        if sock is None:
            raise WiCANError("WiCAN socket is not open")

        deadline = time.monotonic() + timeout_ms / 1000.0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            try:
                readable, _, _ = select.select([sock], [], [], remaining)
            except OSError as exc:
                raise WiCANError(f"WiCAN socket select failed: {exc}") from exc
            if not readable:
                return None

            try:
                chunk = sock.recv(_RECV_CHUNK)
            except (BlockingIOError, InterruptedError):
                # Spurious wakeup — nothing to read yet, keep waiting.
                continue
            except OSError as exc:
                raise WiCANError(f"WiCAN socket recv failed: {exc}") from exc

            if chunk == b"":
                raise WiCANError("WiCAN socket closed by peer")

            frames = self._stream.feed(chunk)
            # Drop frames not addressed to us; buffer the rest.
            for frame in frames:
                if frame[0] == self._rx_id:
                    self._frame_buffer.append(frame)
            if self._frame_buffer:
                return self._frame_buffer.pop(0)
            # Got bytes but no complete in-scope frame yet — loop and read more.

    # --- low-level socket helpers ---

    def _send_raw(self, data: bytes) -> None:
        """Send raw bytes on the socket, translating socket errors."""
        sock = self._sock
        if sock is None:
            raise WiCANError("WiCAN socket is not open")
        try:
            sock.sendall(data)
        except OSError as exc:
            raise WiCANError(f"WiCAN socket send failed: {exc}") from exc

    def _send_command_checked(self, command: bytes, what: str) -> None:
        """Send an SLCAN control command and raise if it is NAK'd (BEL).

        Waits (bounded) for the adapter's control ack. A bare CR is success; a
        BEL (0x07) anywhere in the response — including a BEL coalesced with a
        following data frame, e.g. ``b"\\x07t7E8..."`` or ``b"\\x07\\r"`` — is a
        NAK and raises :class:`WiCANError`. No ack at all is tolerated (some
        firmware stays silent on success). Any CAN data frames that interleave
        with the ack are NOT discarded: they are routed into the frame buffer so
        the first post-handshake receive still sees them.
        """
        self._send_raw(command)
        if self._wait_for_control_ack(timeout_ms=1000):
            raise WiCANError(f"WiCAN rejected command ({what}): adapter returned BEL")

    def _wait_for_control_ack(self, timeout_ms: int) -> bool:
        """Wait for a control-command ack; return True iff a BEL (NAK) was seen.

        Accumulates raw socket bytes (bounded by ``timeout_ms``) until a bare
        control ack — a CR line or a BEL — is observed, then stops. The WHOLE
        accumulated buffer is scanned for BEL (0x07): a BEL anywhere is a NAK
        (handles the coalesced ``BEL`` + data-frame case that an endswith-only
        check misses). Data frames (``t``/``T`` lines) that arrive interleaved
        with the ack are fed to the SLCAN stream parser and buffered (filtered
        by ``rx_id``) rather than discarded, so no early ECU frame is lost.

        Returns:
            True if a BEL NAK was seen; False on a clean CR ack, silence, or
            only data frames (success / nothing to reject).
        """
        sock = self._sock
        if sock is None:
            raise WiCANError("WiCAN socket is not open")

        buf = bytearray()
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Timed out waiting for a bare control ack. Anything buffered so
                # far had no BEL (we would have returned already) — treat as a
                # silent-success adapter.
                return False
            try:
                readable, _, _ = select.select([sock], [], [], remaining)
            except OSError as exc:
                raise WiCANError(f"WiCAN socket select failed: {exc}") from exc
            if not readable:
                return False
            try:
                chunk = sock.recv(_RECV_CHUNK)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                raise WiCANError(f"WiCAN socket recv failed: {exc}") from exc
            if chunk == b"":
                raise WiCANError("WiCAN socket closed by peer during handshake")

            buf.extend(chunk)
            # A BEL anywhere in everything we have seen is a NAK.
            if BEL[0] in buf:
                return True
            # Split on CR; route data frames to the buffer and look for a bare
            # control ack (an empty line == a lone CR the adapter sent as "ok").
            if self._consume_handshake_lines(buf):
                # Preserve any trailing partial line (the start of a data frame
                # that raced the ack) in the stream parser; it has no CR yet so
                # feed() just buffers it for the next recv to complete.
                if buf:
                    self._stream.feed(bytes(buf))
                    buf.clear()
                return False
            # No complete control ack yet — keep reading until one or timeout.

    def _consume_handshake_lines(self, buf: bytearray) -> bool:
        """Split ``buf`` on CR; buffer data frames, detect a bare control ack.

        Mutates ``buf`` in place, removing every complete CR-terminated line.
        ``t``/``T`` data-frame lines are decoded via the SLCAN stream parser and
        appended to :attr:`_frame_buffer` (filtered by ``rx_id``) so a frame
        that races the handshake ack is not lost. Any other complete line (an
        empty line / bare CR ack, or other control reply) counts as the control
        ack we were waiting for.

        Returns:
            True if a bare control ack line was consumed (stop waiting); False
            if only data frames (or no complete line) were present.
        """
        saw_control_ack = False
        while True:
            idx = buf.find(b"\r")
            if idx == -1:
                break
            line = bytes(buf[: idx + 1])
            del buf[: idx + 1]
            if line[:1] in (b"t", b"T"):
                # A CAN data frame that arrived during bring-up — do not lose it.
                for frame in self._stream.feed(line):
                    if frame[0] == self._rx_id:
                        self._frame_buffer.append(frame)
            else:
                # Bare CR ack (or other non-data control reply) — handshake ack.
                saw_control_ack = True
        return saw_control_ack

    def _drain_acks(self, timeout_ms: int) -> None:
        """Read and discard any pending ack/noise bytes (best-effort).

        Used after the ``C`` close where a BEL (closing an already-closed
        channel) is harmless, so a NAK here is intentionally ignored. Data
        frames are still routed to the buffer by :meth:`_wait_for_control_ack`.
        """
        try:
            self._wait_for_control_ack(timeout_ms=timeout_ms)
        except WiCANError as exc:  # pragma: no cover - drain is advisory only
            logger.debug("WiCAN drain ignored error: %s", exc)

    def _require_open(self) -> None:
        """Raise if the transport has not been opened."""
        if self._sock is None:
            raise WiCANError("WiCAN transport is not open; call open() first")
