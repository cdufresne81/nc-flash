"""
Pure-Python ISO-TP (ISO 15765-2) transport engine.

Implements the ISO-TP segmentation/reassembly layer on top of an injected
raw-CAN-frame interface, so it is completely hardware-free and unit-testable.
The engine never touches a real bus directly: it sends and receives 8-byte CAN
frames through two callables supplied by the caller (e.g. an SLCAN or WiCAN
adapter), and exposes message-level :meth:`IsoTpSession.send` /
:meth:`IsoTpSession.receive` that segment and reassemble multi-frame payloads.

Frame format (classical CAN, single-byte PCI):

    Single Frame (SF):       0x0L  + up to 7 data bytes      (L = length, 1..7)
    First Frame (FF):        0x1L LL + 6 data bytes          (12-bit length)
    Consecutive Frame (CF):  0x2N  + up to 7 data bytes      (N = seq 0..15)
    Flow Control (FC):       0x3S  BS STmin                  (S = flow status)

Flow status (FC low nibble): 0 = ContinueToSend (CTS), 1 = Wait, 2 = Overflow.

STmin encoding (ISO 15765-2):
    0x00..0x7F  -> N milliseconds
    0xF1..0xF9  -> 100..900 microseconds
    everything else -> treated as the maximum (0x7F ms) per the standard.

This module is a core transport module and MUST remain headless: it imports
only the standard library and must never import PySide6.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)


# --- ISO-TP protocol constants ---

# PCI frame types (high nibble of byte 0).
PCI_SINGLE_FRAME = 0x0
PCI_FIRST_FRAME = 0x1
PCI_CONSECUTIVE_FRAME = 0x2
PCI_FLOW_CONTROL = 0x3

# Flow-control status (low nibble of an FC frame's byte 0).
FC_CONTINUE_TO_SEND = 0x0
FC_WAIT = 0x1
FC_OVERFLOW = 0x2

# Single Frame can carry at most 7 payload bytes (classical addressing).
SF_MAX_DATA = 7
# First Frame carries 6 payload bytes (2 PCI bytes + 6 data).
FF_DATA_LEN = 6
# Consecutive Frame carries at most 7 payload bytes (1 PCI byte + 7 data).
CF_MAX_DATA = 7
# Maximum payload expressible by a 12-bit First Frame length field.
MAX_ISOTP_PAYLOAD = 0xFFF

# Default flow-control parameters we advertise when *receiving*.
DEFAULT_BLOCK_SIZE = 0  # 0 = no further FC needed, send all CFs at once.
DEFAULT_STMIN = 0  # 0 ms minimum separation time.

# Guard against a peer flooding us with WAIT frames forever.
DEFAULT_MAX_WAIT_FRAMES = 100


class IsoTpError(Exception):
    """Raised on ISO-TP protocol violations, timeouts, or sequence gaps."""

    pass


class IsoTpTimeout(IsoTpError):
    """Raised specifically when an ISO-TP deadline expires with no answer.

    A dedicated subclass of :class:`IsoTpError` so callers can distinguish a
    genuine "no response in time" (retryable / treat as no-answer) from a
    protocol violation such as a sequence gap, malformed PCI, overflow, or a
    short frame (which must surface loudly, never be silently retried).
    """

    pass


class FrameTransport(Protocol):
    """Raw-frame interface the ISO-TP engine drives.

    Any object providing these two methods can back an :class:`IsoTpSession`;
    in practice a small adapter object or a pair of callables (see
    :class:`IsoTpSession`) is supplied. All framing above the raw CAN frame is
    handled by the session, not by this transport.
    """

    def send_frame(self, can_id: int, data: bytes) -> None:
        """Transmit a single CAN frame (``data`` is up to 8 bytes)."""
        ...

    def recv_frame(self, timeout_ms: int) -> Optional[Tuple[int, bytes]]:
        """Receive one CAN frame as ``(can_id, data)``, or ``None`` on timeout."""
        ...


def decode_stmin(stmin: int) -> float:
    """Convert an ISO-TP STmin byte into a sleep duration in seconds.

    Args:
        stmin: Raw STmin byte from a Flow Control frame.

    Returns:
        Separation time in seconds (0.0 .. 0.127).
    """
    if 0x00 <= stmin <= 0x7F:
        return stmin / 1000.0
    if 0xF1 <= stmin <= 0xF9:
        return (stmin - 0xF0) / 10000.0
    # Reserved / invalid -> use the maximum millisecond value per ISO 15765-2.
    return 0x7F / 1000.0


class IsoTpSession:
    """ISO-TP segmentation/reassembly over an injected raw-frame interface.

    The session is configured with the TX/RX CAN identifiers and the padding
    byte, plus our default Flow Control parameters (advertised when we are the
    receiver). On transmit it honours the peer's Flow Control BS/STmin.

    Construct it with either a ``transport`` object implementing
    :class:`FrameTransport`, or with explicit ``send_frame`` / ``recv_frame``
    callables.
    """

    def __init__(
        self,
        tx_id: int,
        rx_id: int,
        send_frame: Optional[Callable[[int, bytes], None]] = None,
        recv_frame: Optional[Callable[[int], Optional[Tuple[int, bytes]]]] = None,
        transport: Optional[FrameTransport] = None,
        padding: int = 0x00,
        rx_block_size: int = DEFAULT_BLOCK_SIZE,
        rx_stmin: int = DEFAULT_STMIN,
        honor_peer_fc: bool = True,
        max_wait_frames: int = DEFAULT_MAX_WAIT_FRAMES,
        n_cr_ms: Optional[int] = None,
        tx_stmin: int = DEFAULT_STMIN,
    ):
        """
        Args:
            tx_id: CAN arbitration ID we transmit on (e.g. ``0x7E0``).
            rx_id: CAN arbitration ID we accept frames from (e.g. ``0x7E8``).
                Frames with any other ID are ignored.
            send_frame: Callable ``(can_id, data) -> None`` to transmit a frame.
            recv_frame: Callable ``(timeout_ms) -> (can_id, data) | None``.
            transport: Alternative to the two callables: an object exposing
                ``send_frame`` / ``recv_frame`` (see :class:`FrameTransport`).
            padding: Byte used to pad frames to 8 bytes (default ``0x00``).
            rx_block_size: Block Size we advertise in Flow Control when we are
                receiving (0 = send all consecutive frames without further FC).
            rx_stmin: STmin we advertise in Flow Control when receiving.
            honor_peer_fc: When transmitting, obey the peer's FC BS/STmin. When
                ``False``, send all consecutive frames back-to-back ignoring the
                peer's requested separation (the FC status is still honoured).
            max_wait_frames: Maximum consecutive FC WAIT frames tolerated before
                raising, to avoid hanging on a misbehaving peer.
            n_cr_ms: ISO-TP N_Cr — the maximum time to wait for the *next*
                Consecutive Frame of a multi-frame response once reassembly has
                started. ``None`` (default) disables it: each frame may take the
                whole overall ``timeout_ms`` (the historical behaviour, kept for
                the reliable J2534 link). When set, a gap longer than this while
                the overall budget still has time left means a frame was dropped
                on a lossy link, and the receive fails *fast and definitively*
                (a non-:class:`IsoTpTimeout` :class:`IsoTpError`) so the caller
                can retry the whole idempotent read immediately instead of
                stalling the full receive budget. Only ever bounds the gap
                *between* consecutive frames — never the initial wait for the
                first frame, and never extends past the overall deadline.
            tx_stmin: Outbound STmin FLOOR (same encoding as STmin) applied when
                *we* transmit a multi-frame message: the tool never sends
                Consecutive Frames closer together than this, even if the peer's
                Flow Control advertises a smaller (or zero) STmin. ``0`` (default)
                = honour the peer exactly (unchanged behaviour for J2534 and
                tests). A non-zero floor paces our outbound CF burst so it cannot
                overflow a lossy multi-hop link's forwarding buffer (WiCAN
                SLCAN-over-WiFi: the ECU typically advertises STmin=0 in a
                programming session, and an unpaced ~146-frame burst overruns the
                gateway's TCP->CAN path — the mirror of the receive-side overflow
                ``rx_stmin`` already guards). It ONLY changes inter-frame timing
                within one message — never the payload, never a resend — so the
                flash no-mid-stream-resend (anti-brick) invariant is preserved.

        Raises:
            ValueError: If neither a transport nor both callables are supplied,
                or if the padding byte is out of range.
        """
        if transport is not None:
            send_frame = transport.send_frame
            recv_frame = transport.recv_frame
        if send_frame is None or recv_frame is None:
            raise ValueError(
                "IsoTpSession requires either a transport or both "
                "send_frame and recv_frame callables"
            )
        if not 0x00 <= padding <= 0xFF:
            raise ValueError(f"padding byte out of range: {padding}")

        self.tx_id = tx_id
        self.rx_id = rx_id
        self._send_frame = send_frame
        self._recv_frame = recv_frame
        self.padding = padding
        self.rx_block_size = rx_block_size
        self.rx_stmin = rx_stmin
        self.honor_peer_fc = honor_peer_fc
        self.max_wait_frames = max_wait_frames
        self.n_cr_ms = n_cr_ms
        self.tx_stmin = tx_stmin
        # Precomputed outbound inter-CF separation floor (seconds).
        self._tx_min_sep_s = decode_stmin(tx_stmin)

    # --- framing helpers ---

    def _pad(self, data: bytes) -> bytes:
        """Pad ``data`` up to 8 bytes with the configured padding byte.

        Raises:
            IsoTpError: If ``data`` is longer than 8 bytes. Silently truncating
                an over-length frame here could drop real payload bytes on the
                flash path, so it is rejected rather than masked.
        """
        if len(data) > 8:
            raise IsoTpError(f"CAN frame too long for padding: {len(data)}")
        if len(data) == 8:
            return data
        return data + bytes([self.padding]) * (8 - len(data))

    def _tx(self, data: bytes) -> None:
        """Transmit one padded frame on our TX id."""
        self._send_frame(self.tx_id, self._pad(data))

    def _remaining_ms(self, deadline: float) -> int:
        """Milliseconds left until ``deadline`` (monotonic seconds), >= 0."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        return int(remaining * 1000)

    def _consecutive_frame_deadline(self, deadline: float) -> float:
        """Deadline for the next Consecutive Frame: ``min(deadline, now+N_Cr)``.

        With no N_Cr configured this is just the overall ``deadline`` (each frame
        may take the whole budget). With N_Cr set it caps the wait for the next
        frame, but never past the overall deadline, so a dropped frame on a lossy
        link is detected in N_Cr rather than after the full receive budget.
        """
        if self.n_cr_ms is None:
            return deadline
        return min(deadline, time.monotonic() + self.n_cr_ms / 1000.0)

    def _recv_matching_frame(self, deadline: float) -> Tuple[int, bytes]:
        """Receive the next frame addressed to us before ``deadline``.

        Frames whose ``can_id`` does not equal :attr:`rx_id` are ignored. Raises
        :class:`IsoTpTimeout` if the deadline passes with no matching frame.
        """
        while True:
            remaining_ms = self._remaining_ms(deadline)
            if remaining_ms <= 0:
                raise IsoTpTimeout("ISO-TP timeout waiting for frame")
            frame = self._recv_frame(remaining_ms)
            if frame is None:
                # Underlying read timed out; let the deadline check decide.
                continue
            can_id, data = frame
            if can_id != self.rx_id:
                # Not our conversation — ignore and keep waiting.
                continue
            return can_id, bytes(data)

    # --- transmit path ---

    def send(self, payload: bytes, timeout_ms: int) -> None:
        """Send a complete ISO-TP message, segmenting as required.

        Args:
            payload: The full message bytes to transmit.
            timeout_ms: Overall budget for the whole send (including waiting for
                Flow Control frames).

        Raises:
            IsoTpTimeout: If the overall deadline expires waiting for a Flow
                Control frame (a subclass of :class:`IsoTpError`).
            IsoTpError: On overflow or an unexpected Flow Control status from
                the peer.
        """
        if len(payload) > MAX_ISOTP_PAYLOAD:
            raise IsoTpError(
                f"payload too large for ISO-TP: {len(payload)} > {MAX_ISOTP_PAYLOAD}"
            )

        deadline = time.monotonic() + timeout_ms / 1000.0

        if len(payload) <= SF_MAX_DATA:
            self._send_single_frame(payload)
            return

        self._send_multi_frame(payload, deadline)

    def _send_single_frame(self, payload: bytes) -> None:
        """Send a Single Frame (PCI 0x0L, length in low nibble)."""
        pci = (PCI_SINGLE_FRAME << 4) | len(payload)
        self._tx(bytes([pci]) + payload)

    def _send_multi_frame(self, payload: bytes, deadline: float) -> None:
        """Send a First Frame, then Consecutive Frames honouring peer FC."""
        length = len(payload)
        # First Frame: 0x1 + 12-bit length, then 6 data bytes.
        ff_pci0 = (PCI_FIRST_FRAME << 4) | ((length >> 8) & 0x0F)
        ff_pci1 = length & 0xFF
        first = bytes([ff_pci0, ff_pci1]) + payload[:FF_DATA_LEN]
        self._tx(first)

        offset = FF_DATA_LEN
        seq = 1

        block_size, stmin = self._await_flow_control(deadline)
        frames_in_block = 0

        while offset < length:
            chunk = payload[offset : offset + CF_MAX_DATA]
            pci = (PCI_CONSECUTIVE_FRAME << 4) | (seq & 0x0F)
            self._tx(bytes([pci]) + chunk)

            offset += len(chunk)
            seq = (seq + 1) & 0x0F
            frames_in_block += 1

            if offset >= length:
                break

            if block_size and frames_in_block >= block_size:
                # Block exhausted — wait for the next Flow Control.
                block_size, stmin = self._await_flow_control(deadline)
                frames_in_block = 0
            else:
                # Separation between consecutive frames within a block: honour the
                # peer's requested STmin, but never send faster than our outbound
                # floor (tx_stmin). The floor is what paces a lossy multi-hop link
                # (WiCAN) where the peer advertises STmin=0; with the default floor
                # of 0 this is identical to honouring the peer exactly.
                self._pace_consecutive_frame(stmin, deadline)

    def _await_flow_control(self, deadline: float) -> Tuple[int, int]:
        """Wait for a Flow Control frame and return ``(block_size, stmin)``.

        Honours WAIT (keep waiting) and raises on OVERFLOW or unknown status.
        """
        wait_count = 0
        while True:
            _can_id, data = self._recv_matching_frame(deadline)
            if len(data) < 1:
                raise IsoTpError("empty ISO-TP frame while awaiting Flow Control")

            pci_type = (data[0] >> 4) & 0x0F
            if pci_type != PCI_FLOW_CONTROL:
                raise IsoTpError(
                    f"expected Flow Control frame, got PCI type 0x{pci_type:X}"
                )

            status = data[0] & 0x0F
            if status == FC_CONTINUE_TO_SEND:
                block_size = data[1] if len(data) > 1 else 0
                stmin = data[2] if len(data) > 2 else 0
                return block_size, stmin
            if status == FC_WAIT:
                wait_count += 1
                if wait_count > self.max_wait_frames:
                    raise IsoTpError("too many ISO-TP Flow Control WAIT frames")
                # Loop and wait for the next FC (CTS or another WAIT).
                continue
            if status == FC_OVERFLOW:
                raise IsoTpError("ISO-TP Flow Control reported buffer overflow")
            raise IsoTpError(f"unknown ISO-TP Flow Control status: 0x{status:X}")

    def _pace_consecutive_frame(self, peer_stmin: int, deadline: float) -> None:
        """Sleep the inter-Consecutive-Frame separation, but never past ``deadline``.

        The effective separation is ``max(peer_request, tx_floor)``:
          * ``peer_request`` = the peer's advertised STmin, honoured only when
            :attr:`honor_peer_fc` is set (otherwise 0);
          * ``tx_floor`` = our own outbound :attr:`tx_stmin` floor, applied
            ALWAYS — it is the host-side pacing that keeps a CF burst from
            overflowing a lossy multi-hop link even when the peer asks for 0.

        With the default ``tx_stmin=0`` this collapses to the previous behaviour
        (sleep the peer's STmin, nothing when it is 0).
        """
        peer = decode_stmin(peer_stmin) if self.honor_peer_fc else 0.0
        delay = max(peer, self._tx_min_sep_s)
        if delay <= 0:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IsoTpTimeout("ISO-TP timeout during separation time")
        time.sleep(min(delay, remaining))

    # --- receive path ---

    def receive(self, timeout_ms: int) -> bytes:
        """Receive and reassemble a complete ISO-TP message.

        Args:
            timeout_ms: Overall budget for receiving the whole message.

        Returns:
            The reassembled message bytes.

        Raises:
            IsoTpTimeout: If the overall deadline expires before a complete
                message arrives (a subclass of :class:`IsoTpError`).
            IsoTpError: On an unexpected/malformed frame, a sequence gap, a
                short frame, or a declared length that is never satisfied.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0

        _can_id, data = self._recv_matching_frame(deadline)
        if len(data) < 1:
            raise IsoTpError("empty ISO-TP frame")

        pci_type = (data[0] >> 4) & 0x0F
        if pci_type == PCI_SINGLE_FRAME:
            return self._recv_single_frame(data)
        if pci_type == PCI_FIRST_FRAME:
            return self._recv_multi_frame(data, deadline)
        raise IsoTpError(f"unexpected ISO-TP PCI type at message start: 0x{pci_type:X}")

    def _recv_single_frame(self, data: bytes) -> bytes:
        """Extract the payload from a Single Frame."""
        length = data[0] & 0x0F
        if length == 0:
            raise IsoTpError("ISO-TP Single Frame with zero length")
        if length > SF_MAX_DATA:
            raise IsoTpError(f"ISO-TP Single Frame length out of range: {length}")
        if len(data) < 1 + length:
            raise IsoTpError("ISO-TP Single Frame shorter than declared length")
        return data[1 : 1 + length]

    def _recv_multi_frame(self, first: bytes, deadline: float) -> bytes:
        """Reassemble a First Frame + Consecutive Frames into the full payload."""
        if len(first) < 2:
            raise IsoTpError("ISO-TP First Frame too short")

        length = ((first[0] & 0x0F) << 8) | first[1]
        if length <= SF_MAX_DATA:
            raise IsoTpError(
                f"ISO-TP First Frame declares non-multi-frame length: {length}"
            )

        # A First Frame always carries a full 6 payload bytes (a complete 8-byte
        # CAN frame). A short FF would silently under-fill the buffer and shift
        # every subsequent Consecutive Frame's bytes — a misaligned reassembly
        # that could still pass the final length check. Reject it outright.
        if len(first) < 2 + FF_DATA_LEN:
            raise IsoTpError("ISO-TP First Frame shorter than 8 bytes")

        # FF carries the first 6 payload bytes (after the 2 PCI bytes).
        buf = bytearray(first[2 : 2 + FF_DATA_LEN])

        # Send Flow Control CTS advertising our BS/STmin for the first block.
        self._send_flow_control(FC_CONTINUE_TO_SEND)

        expected_seq = 1
        frames_in_block = 0
        while len(buf) < length:
            frame_deadline = self._consecutive_frame_deadline(deadline)
            try:
                _can_id, data = self._recv_matching_frame(frame_deadline)
            except IsoTpTimeout:
                # An N_Cr gap (the per-frame deadline expired while the overall
                # budget still had time) means a Consecutive Frame was dropped
                # on a lossy link. Surface it as a definitive error, NOT a benign
                # IsoTpTimeout, so the transport seam re-raises it (instead of
                # mapping to None and letting the UDS pending loop silently wait
                # out the whole budget) and the idempotent read retries at once.
                if self.n_cr_ms is not None and time.monotonic() < deadline:
                    raise IsoTpError(
                        "ISO-TP consecutive-frame timeout "
                        f"(N_Cr {self.n_cr_ms} ms exceeded — frame dropped)"
                    )
                raise
            if len(data) < 1:
                raise IsoTpError("empty ISO-TP Consecutive Frame")

            pci_type = (data[0] >> 4) & 0x0F
            if pci_type != PCI_CONSECUTIVE_FRAME:
                raise IsoTpError(
                    f"expected Consecutive Frame, got PCI type 0x{pci_type:X}"
                )

            seq = data[0] & 0x0F
            if seq != expected_seq:
                raise IsoTpError(
                    f"ISO-TP sequence gap: expected {expected_seq}, got {seq}"
                )

            remaining = length - len(buf)
            take = min(remaining, CF_MAX_DATA)
            # Any non-final Consecutive Frame must carry a full CF_MAX_DATA
            # payload (a complete 8-byte CAN frame). A short/garbled non-final
            # CF would under-fill the buffer and byte-misalign the rest of the
            # reassembly while still possibly passing the final length check —
            # silent corruption on the flash path. Demand the full frame.
            if len(data) < 1 + take:
                raise IsoTpError(
                    "ISO-TP Consecutive Frame shorter than expected "
                    f"(got {len(data) - 1} data bytes, need {take})"
                )
            buf.extend(data[1 : 1 + take])
            expected_seq = (expected_seq + 1) & 0x0F
            frames_in_block += 1

            # When we advertised a non-zero block size, the sender stops after
            # each block and waits for a fresh Flow Control. Send the next CTS
            # so transmission resumes (unless the message is already complete).
            if (
                self.rx_block_size
                and frames_in_block >= self.rx_block_size
                and len(buf) < length
            ):
                self._send_flow_control(FC_CONTINUE_TO_SEND)
                frames_in_block = 0

        return bytes(buf[:length])

    def _send_flow_control(self, status: int) -> None:
        """Send a Flow Control frame with the given status and our BS/STmin."""
        pci = (PCI_FLOW_CONTROL << 4) | (status & 0x0F)
        self._tx(bytes([pci, self.rx_block_size & 0xFF, self.rx_stmin & 0xFF]))
