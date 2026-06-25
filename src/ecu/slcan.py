"""
LAWICEL / SLCAN ASCII protocol codec.

Pure, headless encode/decode helpers for the SLCAN serial-line CAN ASCII
protocol used by WiCAN (and many USB-CAN adapters). This module performs
NO I/O: it converts CAN frames to/from their ASCII line representation and
provides the control-command byte strings. The TCP/serial transport layer
builds on top of these helpers.

Frame format (LAWICEL):
    Standard (11-bit):  ``t`` + 3 hex ID digits + 1 DLC digit
                        + DLC*2 data hex digits + ``\\r``
    Extended (29-bit):  ``T`` + 8 hex ID digits + 1 DLC digit
                        + DLC*2 data hex digits + ``\\r``

All ID and data hex digits are uppercase. Every command and frame is
terminated by a single carriage return (``\\r``, 0x0D). The adapter
acknowledges commands with a bare CR (ok) or a BEL (0x07, error).

This module MUST remain importable without PySide6 so it can be unit
tested headless.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional, Union

from .exceptions import ECUError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SlcanError(ECUError):
    """Raised when an SLCAN line cannot be parsed or encoded."""

    pass


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

CR = b"\r"  # 0x0D — frame/command terminator and "ok" ack
BEL = b"\x07"  # 0x07 — adapter error ack

#: Maximum classic-CAN payload length (bytes) for a single frame.
MAX_DLC = 8

#: Largest valid standard (11-bit) arbitration ID.
STD_ID_MAX = 0x7FF
#: Largest valid extended (29-bit) arbitration ID.
EXT_ID_MAX = 0x1FFFFFFF

# Number of hex digits used for each ID width.
_STD_ID_HEX_LEN = 3
_EXT_ID_HEX_LEN = 8

# --- Control commands (each terminated by CR) ---
OPEN = b"O\r"  # Open the CAN channel
CLOSE = b"C\r"  # Close the CAN channel
LISTEN = b"L\r"  # Open in listen-only (silent) mode
VERSION = b"V\r"  # Request firmware/hardware version
SERIAL = b"N\r"  # Request adapter serial number

# --- Bitrate codes S0..S8 ---
#: Standard LAWICEL bitrate code -> nominal bus speed (for reference/logging).
BITRATE_CODES = {
    0: 10_000,
    1: 20_000,
    2: 50_000,
    3: 100_000,
    4: 125_000,
    5: 250_000,
    6: 500_000,
    7: 800_000,
    8: 1_000_000,
}

#: Pre-built 500 kbps bitrate command (the NC ECU bus speed, S6).
BITRATE_500K = b"S6\r"


def bitrate_command(code: int) -> bytes:
    """Build an ``S<code>\\r`` set-bitrate command.

    Args:
        code: LAWICEL bitrate code 0..8 (see :data:`BITRATE_CODES`). For
            500 kbps (the NC ECU bus speed) use 6.

    Returns:
        The command bytes, e.g. ``b"S6\\r"`` for ``code=6``.

    Raises:
        SlcanError: If ``code`` is outside the valid 0..8 range.
    """
    if code not in BITRATE_CODES:
        raise SlcanError(
            f"Invalid SLCAN bitrate code {code!r}; must be one of "
            f"{sorted(BITRATE_CODES)}"
        )
    return f"S{code}\r".encode("ascii")


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_data_frame(can_id: int, data: bytes, extended: bool = False) -> bytes:
    """Encode a CAN data frame into its SLCAN ASCII representation.

    Standard frames use the ``t`` command with a 3-hex-digit ID; extended
    (29-bit) frames use the ``T`` command with an 8-hex-digit ID. The DLC
    is a single hex digit and each data byte is two uppercase hex digits.
    The line is terminated with a single carriage return.

    Args:
        can_id: CAN arbitration ID. Must fit in 11 bits unless
            ``extended`` is set, in which case it must fit in 29 bits.
        data: Frame payload, 0..8 bytes.
        extended: When True, emit a 29-bit extended frame (``T``).

    Returns:
        The encoded line including the trailing ``\\r``.

    Raises:
        SlcanError: On out-of-range ID or oversized payload.
    """
    if len(data) > MAX_DLC:
        raise SlcanError(f"CAN payload too long: {len(data)} bytes (max {MAX_DLC})")
    if can_id < 0:
        raise SlcanError(f"CAN ID must be non-negative, got {can_id}")

    if extended:
        if can_id > EXT_ID_MAX:
            raise SlcanError(
                f"Extended CAN ID 0x{can_id:X} exceeds 29-bit max " f"0x{EXT_ID_MAX:X}"
            )
        prefix = "T"
        id_str = f"{can_id:0{_EXT_ID_HEX_LEN}X}"
    else:
        if can_id > STD_ID_MAX:
            raise SlcanError(
                f"Standard CAN ID 0x{can_id:X} exceeds 11-bit max "
                f"0x{STD_ID_MAX:X} (use extended=True for 29-bit)"
            )
        prefix = "t"
        id_str = f"{can_id:0{_STD_ID_HEX_LEN}X}"

    line = f"{prefix}{id_str}{len(data):X}{data.hex().upper()}\r"
    return line.encode("ascii")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def decode_frame(line: Union[bytes, bytearray, str]) -> Optional[tuple[int, bytes]]:
    """Decode a single SLCAN data-frame line into ``(can_id, data)``.

    Accepts standard (``t``) and extended (``T``) data frames. A trailing
    carriage return is optional (the streaming feeder strips it). Empty
    input and a bare CR/BEL ack return ``None``; anything that looks like a
    data frame but is malformed raises :class:`SlcanError`.

    Args:
        line: The ASCII line, with or without a trailing ``\\r``. May be
            ``bytes`` or ``str``.

    Returns:
        ``(can_id, data)`` for a valid data frame, or ``None`` for an
        empty line / lone ack byte.

    Raises:
        SlcanError: If the line is a malformed data frame.
    """
    if isinstance(line, (bytes, bytearray)):
        try:
            text = bytes(line).decode("ascii")
        except UnicodeDecodeError as exc:
            raise SlcanError(f"Non-ASCII SLCAN line: {bytes(line)!r}") from exc
    else:
        text = line

    # Drop a single trailing CR if present, then ignore surrounding noise.
    if text.endswith("\r"):
        text = text[:-1]

    if text == "" or text == "\x07":
        # Empty line or bare BEL/ack — not a data frame.
        return None

    kind = text[0]
    if kind == "t":
        id_len = _STD_ID_HEX_LEN
        max_id = STD_ID_MAX
    elif kind == "T":
        id_len = _EXT_ID_HEX_LEN
        max_id = EXT_ID_MAX
    else:
        # 'r'/'R' (RTR) and status/reply lines are not data frames; the
        # codec only handles data frames. Treat the rest as malformed so
        # callers do not silently mis-handle them.
        raise SlcanError(f"Unsupported SLCAN frame type {kind!r} in {text!r}")

    # Minimum length: type + id digits + 1 DLC digit.
    if len(text) < 1 + id_len + 1:
        raise SlcanError(f"Truncated SLCAN frame: {text!r}")

    id_hex = text[1 : 1 + id_len]
    dlc_hex = text[1 + id_len]
    data_hex = text[1 + id_len + 1 :]

    try:
        can_id = int(id_hex, 16)
    except ValueError as exc:
        raise SlcanError(f"Invalid SLCAN ID {id_hex!r} in {text!r}") from exc
    if can_id > max_id:
        raise SlcanError(
            f"SLCAN ID 0x{can_id:X} exceeds max 0x{max_id:X} for type {kind!r}"
        )

    try:
        dlc = int(dlc_hex, 16)
    except ValueError as exc:
        raise SlcanError(f"Invalid SLCAN DLC {dlc_hex!r} in {text!r}") from exc
    if dlc > MAX_DLC:
        raise SlcanError(f"SLCAN DLC {dlc} exceeds max {MAX_DLC} in {text!r}")

    if len(data_hex) != dlc * 2:
        raise SlcanError(
            f"SLCAN payload length mismatch: DLC={dlc} expects "
            f"{dlc * 2} hex chars, got {len(data_hex)} in {text!r}"
        )

    try:
        data = bytes.fromhex(data_hex)
    except ValueError as exc:
        raise SlcanError(f"Invalid SLCAN payload {data_hex!r} in {text!r}") from exc

    return can_id, data


# ---------------------------------------------------------------------------
# Acknowledgement handling
# ---------------------------------------------------------------------------


def is_error_ack(byte_or_line: Union[bytes, bytearray, int]) -> bool:
    """Return True if the adapter response indicates an error (BEL, 0x07).

    A BEL **anywhere** in a byte string is treated as a NAK — not just a
    trailing one. Over TCP the BEL can be coalesced with a CR or a following
    data frame (e.g. ``b"\\x07\\r"`` or ``b"\\x07t7E8...\\r"``); an ``endswith``
    check would miss those. Membership is the coalescing-proof test, matching
    the WiCAN handshake ack logic.

    Args:
        byte_or_line: A single byte value, or a byte string to scan for BEL.

    Returns:
        True if the response is/contains a BEL error byte.
    """
    if isinstance(byte_or_line, int):
        return byte_or_line == 0x07
    return BEL[0] in bytes(byte_or_line)


# ---------------------------------------------------------------------------
# Incremental streaming parser
# ---------------------------------------------------------------------------


class SlcanFrameStream:
    """Incremental SLCAN line splitter for byte streams (e.g. TCP).

    TCP may coalesce several frames into one ``recv`` or split a single
    frame across reads. This feeder buffers partial input and yields each
    complete CR-terminated SLCAN data frame as a decoded ``(can_id, data)``
    tuple. Non-data lines (bare CR/BEL acks, blank lines) are skipped.

    The buffer only retains the trailing partial line between calls, so a
    long-running stream does not accumulate memory.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: Union[bytes, bytearray]) -> list[tuple[int, bytes]]:
        """Feed a chunk of stream bytes; return any newly completed frames.

        Args:
            chunk: Arbitrary bytes received from the stream. May contain
                zero, partial, one, or many complete lines.

        Returns:
            A list of decoded ``(can_id, data)`` tuples for every complete
            data frame found. Empty if no full data frame completed.

        Raises:
            SlcanError: If a completed line is a malformed data frame.

        Contract on a malformed line (all-or-nothing): because this builds the
        list by draining the :meth:`feed_iter` generator, a ``SlcanError`` from
        a later line in the same chunk discards the frames decoded *earlier* in
        this same ``feed()`` call — they are never returned (the list under
        construction is dropped with the exception). The malformed line has
        already been removed from the internal buffer, but any lines *after* it
        in the chunk remain buffered and unprocessed. A caller that needs to
        recover MUST treat the whole ``feed()`` call as failed and :meth:`reset`
        the stream before feeding again; it cannot rely on having received the
        good frames that preceded the bad line, nor on the position of the
        buffer.
        """
        return list(self.feed_iter(chunk))

    def feed_iter(self, chunk: Union[bytes, bytearray]) -> Iterator[tuple[int, bytes]]:
        """Generator variant of :meth:`feed`.

        Yields each decoded ``(can_id, data)`` tuple as lines complete.
        See :meth:`feed` for the buffering contract.
        """
        self._buffer.extend(chunk)

        # Split on CR. The element after the last CR is an incomplete line
        # that we retain in the buffer for the next feed().
        while True:
            idx = self._buffer.find(b"\r")
            if idx == -1:
                break
            line = bytes(self._buffer[: idx + 1])  # include the CR
            del self._buffer[: idx + 1]
            frame = decode_frame(line)
            if frame is not None:
                yield frame

    @property
    def pending(self) -> bytes:
        """The buffered bytes of the not-yet-terminated trailing line."""
        return bytes(self._buffer)

    def reset(self) -> None:
        """Discard any buffered partial line (e.g. after a reconnect)."""
        self._buffer.clear()
