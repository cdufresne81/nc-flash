"""
OBDX Pro (USB serial) ECU transport.

Implements :class:`~src.ecu.transport.EcuTransport` for the CAN-capable OBDX
Pro adapters (VX/GT/FT; bench-tested on the VX) over their USB virtual COM
port. The adapter enumerates as an STMicro CDC device (VID 0483 / PID 5740),
which Windows drives with its inbox ``usbser`` driver — no vendor driver or
J2534 DLL is needed.

The adapter boots in an ELM327-compatible ASCII mode. ``DXDP1`` switches it to
the vendor's binary "DVI" protocol, which this module speaks:

    frame    = [cmd] [len] [len bytes of body] [checksum]
    checksum = ~(sum of every byte before it) & 0xFF
    reply    = cmd + 0x10 (0x7F = adapter-side error)

Commands 0x10/0x11 (short/long length) write one network message; 0x08/0x09
are received network messages. The adapter does ISO-TP itself: a flow-control
filter (ID 0x7E8, flow ID 0x7E0) makes it segment our requests, answer the
ECU's First Frames with Flow Control, and hand back the reassembled response.
So the wire stack is:

    UDS payload (SID + data)            <- the EcuTransport seam
      ISO-TP segmentation/reassembly    <- adapter firmware
        DVI binary frames               <- this module
          USB CDC serial (921600 baud)  <- pyserial

Every ISO-TP setting is written explicitly on open (the vendor's own J2534
connect values), and every filter slot but ours is cleared, so an adapter left
configured by other software behaves the same as a fresh one.

A refused write or a missing ack aborts the operation rather than resending:
the DVI protocol does not say whether the ECU saw part of the message, and a
resent TransferData block could be programmed twice.

Protocol learned from the vendor's MIT-licensed ``OBDXWindows`` NuGet library
and verified on the bench ECU (2026-10-17).

This module is a core transport module and MUST remain headless: it imports
only the standard library, pyserial and sibling core modules — never PySide6.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Optional

from .constants import CAN_REQUEST_ID, CAN_RESPONSE_ID
from .exceptions import ECUError
from .transport import EcuTransport

logger = logging.getLogger(__name__)

#: USB identity of the OBDX Pro's virtual COM port (STMicro CDC).
OBDX_USB_VID = 0x0483
OBDX_USB_PID = 0x5740

#: The adapter ignores the host baud rate (USB CDC), but the vendor opens at this.
OBDX_BAUD = 921600

#: Models with HS-CAN (the VT is VPW-only). Only the VX is bench-tested.
CAN_MODELS = ("OBDX Pro VX", "OBDX Pro GT", "OBDX Pro FT")
TESTED_MODEL = "OBDX Pro VX"

# DVI command bytes (reply = command + 0x10).
CMD_NET_WRITE = 0x10  # write network message, 1-byte length
CMD_NET_WRITE_LONG = 0x11  # write network message, 2-byte length
CMD_NET_RX = 0x08  # received network message, 1-byte length
CMD_NET_RX_LONG = 0x09  # received network message, 2-byte length
CMD_INFO = 0x22  # adapter information (sub: 0 hw, 1 fw, 2 model, 3 name, 4 serial)
CMD_REBOOT = 0x25  # reset the adapter back to ELM327 mode
CMD_OBD = 0x31  # OBD protocol (sub 1: protocol, sub 2: enable)
CMD_CAN = 0x34  # CAN settings (see _configure_can)
CMD_ADC = 0x3A  # analog reads (channel 0xFE: battery voltage, mV)
CMD_ERROR = 0x7F

#: First bytes that can start a frame from the adapter: received network
#: messages, the error reply, and the reply (cmd + 0x10) to every command this
#: module sends. Used to skip junk while resynchronising.
_SENT_COMMANDS = (
    CMD_NET_WRITE,
    CMD_NET_WRITE_LONG,
    CMD_INFO,
    CMD_REBOOT,
    CMD_OBD,
    CMD_CAN,
    CMD_ADC,
)
_REPLY_HEADERS = frozenset(
    {CMD_NET_RX, CMD_NET_RX_LONG, CMD_ERROR} | {cmd + 0x10 for cmd in _SENT_COMMANDS}
)

#: Largest body a long network frame can carry: CAN ID + 2-byte ISO-TP PCI +
#: the 4095-byte ISO-TP maximum. A bigger declared length is a false header.
_MAX_LONG_BODY = 4 + 2 + 0xFFF
_INVALID_LENGTH = -1

#: A frame that starts but does not complete within this window is treated as
#: a false header (the adapter sends whole reassembled messages at USB speed).
_PARTIAL_FRAME_TIMEOUT_S = 0.5

PROTOCOL_HSCAN = 2

# CAN setting sub-commands (CMD_CAN).
_CAN_FILTER = 0
_CAN_HW_FILTERING = 6
_CAN_FLOW_CONTROL_DELAY = 10
_CAN_AUTO_PROCESS = 11
_CAN_PAD_BYTE = 13
_CAN_PAD_ENABLE = 14
_CAN_AUTO_LENGTH = 15
_CAN_WRITE_FRAME_DELAY = 16
_CAN_FLOW_CONTROL_TIMEOUT = 17

#: The adapter has 28 receive filter slots; slot 0 is ours.
_FILTER_SLOTS = 28

#: Largest payload that fits a short (1-byte length) write; the length byte
#: also covers the 4-byte CAN ID. The vendor library switches at the same point.
_SHORT_WRITE_MAX = 250

#: Budget for adapter command replies (config, info, write acks).
_COMMAND_TIMEOUT_S = 2.0

#: How long to wait for "OK" after the ASCII ``DXDP1`` switch.
_DVI_SWITCH_TIMEOUT_S = 0.6

#: flush() returns once the line has been quiet this long (or after the cap).
_FLUSH_QUIET_S = 0.3
_FLUSH_MAX_S = 2.0

_INFO_FIRMWARE = 1
_INFO_NAME = 3


class ObdxError(ECUError):
    """OBDX adapter-level failure (no adapter, bad reply, write refused)."""


def checksum(data: bytes) -> int:
    """DVI checksum: one's complement of the 8-bit sum of ``data``."""
    return (~sum(data)) & 0xFF


def build_frame(*body: int) -> bytes:
    """Build a DVI frame from ``[cmd, len, ...]`` and append its checksum."""
    raw = bytes(body)
    return raw + bytes([checksum(raw)])


def build_network_write(can_id: int, payload: bytes) -> bytes:
    """Frame one UDS payload for transmission on ``can_id``.

    The adapter adds the ISO-TP PCI and segments the message itself.
    """
    body = can_id.to_bytes(4, "big") + bytes(payload)
    if len(payload) <= _SHORT_WRITE_MAX:
        return build_frame(CMD_NET_WRITE, len(body), *body)
    return build_frame(CMD_NET_WRITE_LONG, len(body) >> 8, len(body) & 0xFF, *body)


def decode_network_frame(frame: bytes) -> Optional[tuple[int, bytes]]:
    """Return ``(can_id, uds_payload)`` from a received 0x08/0x09 frame.

    The adapter keeps the ISO-TP PCI of the (reassembled) message: a Single
    Frame length nibble, or a First Frame's 12-bit total length. Returns
    ``None`` for a frame that carries no complete UDS message.
    """
    if frame[0] == CMD_NET_RX_LONG:
        body = frame[3:-1]
    else:
        body = frame[2:-1]
    if len(body) < 5:
        return None
    can_id = int.from_bytes(body[:4], "big")
    data = body[4:]
    pci = data[0]
    if 0x10 <= pci <= 0x1F:
        if len(data) < 2:
            return None
        length = ((pci & 0x0F) << 8) | data[1]
        payload = data[2 : 2 + length]
    elif 1 <= pci <= 7:
        length = pci
        payload = data[1 : 1 + length]
    else:
        logger.debug("OBDX: frame with unexpected PCI 0x%02X ignored", pci)
        return None
    if len(payload) < length:
        logger.debug(
            "OBDX: truncated message (%d of %d bytes) ignored", len(payload), length
        )
        return None
    return can_id, bytes(payload)


class DviFrameParser:
    """Incremental splitter of the adapter's byte stream into DVI frames.

    There is no sync marker in the protocol, so the parser guards against a
    false header three ways: it only accepts a known reply byte as a header
    while resynchronising (and starts that way, skipping ASCII left over from
    the mode switch), it rejects impossible lengths, and it gives up on a frame
    that never completes.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buf = bytearray()
        self._resyncing = True
        self._partial_since: Optional[float] = None

    def feed(self, chunk: bytes = b"") -> list[bytes]:
        """Add ``chunk`` and return every complete, checksum-valid frame.

        Call it with no data too: that is how a stalled partial frame times out.
        """
        self._buf += chunk
        frames = []
        while self._buf:
            if self._resyncing and self._buf[0] not in _REPLY_HEADERS:
                del self._buf[:1]
                continue
            total = self._frame_length()
            if total == _INVALID_LENGTH:
                logger.warning("OBDX: impossible frame length, resyncing")
                self._drop_header()
                continue
            if total is None or len(self._buf) < total:
                now = self._clock()
                if self._partial_since is None or chunk:
                    # The window restarts whenever bytes arrive: a frame is only
                    # given up on after a full timeout with nothing received.
                    self._partial_since = now
                    break
                if now - self._partial_since < _PARTIAL_FRAME_TIMEOUT_S:
                    break
                logger.warning(
                    "OBDX: incomplete frame timed out, resyncing: %s",
                    bytes(self._buf[:16]).hex(),
                )
                self._drop_header()
                continue
            frame = bytes(self._buf[:total])
            if checksum(frame[:-1]) != frame[-1]:
                logger.warning("OBDX: checksum mismatch, resyncing: %s", frame.hex())
                self._drop_header()
                continue
            del self._buf[:total]
            self._resyncing = False
            self._partial_since = None
            frames.append(frame)
        return frames

    def _drop_header(self) -> None:
        """Discard the byte that looked like a header and resynchronise."""
        del self._buf[:1]
        self._resyncing = True
        self._partial_since = None

    def _frame_length(self) -> Optional[int]:
        buf = self._buf
        if len(buf) < 2:
            return None
        # Only received network messages carry a 2-byte length; every reply,
        # including the long-write ack, has a 1-byte one.
        if buf[0] == CMD_NET_RX_LONG:
            if len(buf) < 3:
                return None
            body = (buf[1] << 8) | buf[2]
            return body + 4 if body <= _MAX_LONG_BODY else _INVALID_LENGTH
        return buf[1] + 3

    def reset(self) -> None:
        self._buf.clear()
        self._resyncing = True
        self._partial_since = None


def find_obdx_ports() -> list[str]:
    """COM ports that may be an OBDX Pro (its USB identity or description).

    0483:5740 is STMicro's generic virtual COM port ID, so other STM32 devices
    can match too; the transport keeps the first port that answers as OBDX.
    """
    from serial.tools import list_ports

    ports = []
    for info in list_ports.comports():
        if (info.vid, info.pid) == (OBDX_USB_VID, OBDX_USB_PID) or "OBDX" in (
            info.description or ""
        ):
            ports.append(info.device)
    return ports


def probe_adapter(port: str = "") -> dict:
    """Identify the adapter without joining the CAN bus.

    Returns ``{"port", "name", "firmware", "voltage"}``; raises
    :class:`ObdxError` when no adapter answers.
    """
    transport = ObdxTransport(port=port)
    transport._open_adapter()
    try:
        return {
            "port": transport.port,
            "name": transport.adapter_name,
            "firmware": transport.firmware,
            "voltage": transport.battery_voltage(),
        }
    finally:
        transport.close()


def _open_serial(port: str):
    import serial

    # exclusive: a second opener (Settings > Test Connection, another NC Flash)
    # must fail rather than reset an adapter mid-flash. Windows ports are
    # always exclusive; this makes POSIX match.
    return serial.Serial(port, OBDX_BAUD, timeout=0.02, write_timeout=2, exclusive=True)


class ObdxTransport(EcuTransport):
    """EcuTransport over an OBDX Pro adapter on a USB COM port.

    Owns the serial port: :meth:`open` switches the adapter to DVI mode and
    configures HS-CAN with an ISO-TP flow-control filter; :meth:`close`
    disables the protocol, returns the adapter to ELM327 mode and releases
    the port.
    """

    def __init__(
        self,
        port: str = "",
        tx_id: int = CAN_REQUEST_ID,
        rx_id: int = CAN_RESPONSE_ID,
        padding: int = 0x00,
        serial_factory: Optional[Callable[[str], object]] = None,
    ):
        """
        Args:
            port: COM port (e.g. ``"COM7"``); empty = auto-detect.
            tx_id: CAN ID requests are sent on.
            rx_id: CAN ID the ECU responds on.
            padding: Byte the adapter pads short CAN frames with.
            serial_factory: Test seam; ``fn(port) -> serial-like object``.
        """
        self._port_name = port
        self._tx_id = tx_id
        self._rx_id = rx_id
        self._padding = padding
        self._serial_factory = serial_factory or _open_serial
        self._sp = None
        self._parser = DviFrameParser()
        self._rx_queue: deque[bytes] = deque()
        self.adapter_name = ""
        self.firmware = ""

    # --- Lifecycle ---

    @property
    def port(self) -> str:
        return self._port_name

    @property
    def is_tested_model(self) -> bool:
        """True for the adapter model NC Flash was bench-tested with."""
        return self.adapter_name == TESTED_MODEL

    def open(self) -> None:
        if self._sp is not None:
            return
        self._open_adapter()
        try:
            self._configure_can()
        except Exception:
            self.close()
            raise

    def _open_adapter(self) -> None:
        """Open the port and switch to DVI mode; no CAN setup, no bus traffic."""
        if self._port_name:
            self._open_port(self._port_name)
        else:
            candidates = find_obdx_ports()
            if not candidates:
                raise ObdxError(
                    "No OBDX Pro adapter found on any USB COM port. Check the USB "
                    "cable, or set the COM port in Settings > ECU > OBDX Pro."
                )
            errors = []
            for port in candidates:
                try:
                    self._open_port(port)
                    break
                except ObdxError as exc:
                    errors.append(f"{port}: {exc}")
            else:
                raise ObdxError("No OBDX Pro adapter answered. " + "; ".join(errors))
            self._port_name = port
        logger.info(
            "OBDX: %s fw %s on %s", self.adapter_name, self.firmware, self._port_name
        )

    def _open_port(self, port: str) -> None:
        try:
            self._sp = self._serial_factory(port)
        except Exception as exc:
            self._sp = None
            raise ObdxError(
                f"Could not open {port}; it may be in use (is NC Flash already "
                f"connected to the ECU, or another program open?): {exc}"
            ) from exc
        try:
            self._enter_dvi_mode(port)
            name = self._info_text(_INFO_NAME)
            if name not in CAN_MODELS:
                raise ObdxError(
                    f"{port} answered as {name!r}; NC Flash needs a CAN-capable "
                    f"OBDX Pro ({', '.join(CAN_MODELS)})"
                )
            self.adapter_name = name
            self.firmware = self._info_version(_INFO_FIRMWARE)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        sp, self._sp = self._sp, None
        if sp is None:
            return
        try:
            # Stop the adapter talking on the bus, then hand it back in ELM327
            # mode so other apps (and our next open) find it in a known state.
            sp.write(build_frame(CMD_OBD, 2, 2, 0))
            sp.write(build_frame(CMD_REBOOT, 0))
            sp.flush()
        except Exception:
            pass
        try:
            sp.close()
        except Exception:
            pass
        self._parser.reset()
        self._rx_queue.clear()

    # --- EcuTransport ---

    def send_message(self, payload: bytes, timeout_ms: int) -> None:
        frame = build_network_write(self._tx_id, payload)
        self._write(frame)
        # The ack lands once the adapter has put the whole message on the bus
        # (after the ECU's Flow Control for a multi-frame request).
        ack = self._await_reply(frame[0] + 0x10, max(timeout_ms / 1000, 1.0))
        status = ack[2]
        if status != 0:
            raise ObdxError(f"OBDX refused the CAN write (status 0x{status:02X})")

    def receive_message(self, timeout_ms: int) -> Optional[bytes]:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            while self._rx_queue:
                decoded = decode_network_frame(self._rx_queue.popleft())
                if decoded is not None and decoded[0] == self._rx_id:
                    return decoded[1]
            if time.monotonic() >= deadline:
                return None
            for frame in self._read_frames():
                if frame[0] in (CMD_NET_RX, CMD_NET_RX_LONG):
                    self._rx_queue.append(frame)
                elif frame[0] == CMD_ERROR:
                    logger.warning("OBDX: adapter error frame %s", frame.hex(" "))
                else:
                    logger.debug("OBDX: stray adapter reply %s", frame.hex())

    def flush(self) -> None:
        """Drop queued responses and wait for the line to go quiet.

        Waits out a response that is still on its way (a late reply would
        otherwise be taken as the answer to the next request).
        """
        if self._sp is None:
            return
        self._rx_queue.clear()
        start = last_data = time.monotonic()
        while True:
            now = time.monotonic()
            if now - last_data >= _FLUSH_QUIET_S or now - start >= _FLUSH_MAX_S:
                break
            if self._read_frames():
                last_data = time.monotonic()
        self._rx_queue.clear()

    @property
    def description(self) -> str:
        return f"OBDX ({self._port_name})" if self._port_name else "OBDX"

    # --- Adapter queries ---

    def battery_voltage(self) -> float:
        """Voltage on the OBD port's pin 16, in volts."""
        reply = self._command(CMD_ADC, 2, 0, 0xFE)
        if len(reply) < 7:
            raise ObdxError(f"OBDX sent a short voltage reply: {reply.hex(' ')}")
        return ((reply[4] << 8) | reply[5]) / 1000

    # --- Internal ---

    def _require_open(self):
        if self._sp is None:
            raise ObdxError("OBDX transport is not open")
        return self._sp

    def _write(self, data: bytes) -> None:
        sp = self._require_open()
        try:
            sp.write(data)
        except Exception as exc:
            raise ObdxError(
                f"OBDX serial write failed (USB unplugged?): {exc}"
            ) from exc

    def _read_frames(self) -> list[bytes]:
        sp = self._require_open()
        try:
            chunk = sp.read(sp.in_waiting or 1)
        except Exception as exc:
            raise ObdxError(f"OBDX serial read failed (USB unplugged?): {exc}") from exc
        return self._parser.feed(chunk or b"")

    def _await_reply(self, reply_cmd: int, timeout_s: float) -> bytes:
        """Read until the reply to a command arrives, queueing ECU traffic."""
        deadline = time.monotonic() + timeout_s
        reply = None
        while reply is None and time.monotonic() < deadline:
            # Keep going through the whole batch: the ECU's response often
            # arrives in the same read as the write ack and must be queued.
            for frame in self._read_frames():
                if reply is None and frame[0] == reply_cmd:
                    reply = frame
                elif frame[0] in (CMD_NET_RX, CMD_NET_RX_LONG):
                    self._rx_queue.append(frame)
                elif frame[0] == CMD_ERROR:
                    if reply is None:
                        raise ObdxError(f"OBDX rejected command: {frame.hex(' ')}")
                    logger.warning("OBDX: adapter error frame %s", frame.hex(" "))
                else:
                    logger.debug("OBDX: unexpected reply %s", frame.hex())
        if reply is None:
            raise ObdxError(f"OBDX did not answer command 0x{reply_cmd - 0x10:02X}")
        return reply

    def _command(self, *body: int) -> bytes:
        self._write(build_frame(*body))
        return self._await_reply(body[0] + 0x10, _COMMAND_TIMEOUT_S)

    def _set(self, *body: int) -> None:
        """Send a setting and check the adapter echoed back the same value."""
        reply = self._command(*body)
        if reply[2:-1] != bytes(body[2:]):
            raise ObdxError(
                f"OBDX did not accept setting {bytes(body).hex(' ')} "
                f"(answered {reply.hex(' ')})"
            )

    def _enter_dvi_mode(self, port: str) -> None:
        """Get the adapter from whatever state it is in to DVI mode.

        A reboot frame first (as the vendor library does): it returns an
        adapter left in DVI mode by an earlier session to ELM327 mode, and is
        ignored as garbage by one already in ELM327 mode. ATZ twice, as the
        vendor does, then wait for the ``DXDP1`` "OK".
        """
        sp = self._sp
        try:
            sp.write(build_frame(CMD_REBOOT, 0))
            time.sleep(0.1)
            for _ in range(2):
                sp.write(b"ATZ \r")
                time.sleep(0.15)
                sp.reset_input_buffer()
            sp.write(b"DXDP1 \r")
            reply = b""
            deadline = time.monotonic() + _DVI_SWITCH_TIMEOUT_S
            while b"OK" not in reply and time.monotonic() < deadline:
                reply += sp.read(sp.in_waiting or 1)
        except Exception as exc:
            raise ObdxError(f"{port}: serial I/O failed during setup: {exc}") from exc
        if b"OK" not in reply:
            raise ObdxError(
                f"{port} did not switch to OBDX binary mode (got {reply!r}). If "
                "this is the OBDX, unplug it from the OBD port for a few seconds "
                "and try again."
            )
        # Let the trailing ">" prompt arrive, then start clean.
        time.sleep(0.05)
        sp.reset_input_buffer()
        self._parser.reset()

    def _info_text(self, sub: int) -> str:
        reply = self._command(CMD_INFO, 1, sub)
        return reply[3:-1].decode("ascii", errors="replace")

    def _info_version(self, sub: int) -> str:
        reply = self._command(CMD_INFO, 1, sub)
        return ".".join(str(b) for b in reply[3:-1])

    def _configure_can(self) -> None:
        """Put the adapter in a known ISO-TP state for the ECU.

        Values follow the vendor's own J2534 ISO15765 connect, so nothing is
        inherited from whatever last used the adapter.
        """
        self._set(CMD_OBD, 2, 2, 0)  # protocol off while configuring
        self._set(CMD_OBD, 2, 1, PROTOCOL_HSCAN)
        self._set(CMD_CAN, 2, _CAN_HW_FILTERING, 0)
        self._set(CMD_CAN, 2, _CAN_AUTO_PROCESS, 1)  # adapter does ISO-TP
        self._set(CMD_CAN, 2, _CAN_AUTO_LENGTH, 1)
        self._set(CMD_CAN, 2, _CAN_PAD_BYTE, self._padding)
        self._set(CMD_CAN, 2, _CAN_PAD_ENABLE, 1)
        self._set(CMD_CAN, 3, _CAN_WRITE_FRAME_DELAY, 0, 10)
        self._set(CMD_CAN, 3, _CAN_FLOW_CONTROL_DELAY, 0, 1)
        self._set(CMD_CAN, 3, _CAN_FLOW_CONTROL_TIMEOUT, 0x01, 0xF4)  # 500
        for slot in range(1, _FILTER_SLOTS):
            self._command(CMD_CAN, 17, _CAN_FILTER, slot, *bytes(15))
        # Slot 0: 11-bit, type 1 (flow control), enabled, ID / mask / flow ID.
        self._set(
            CMD_CAN,
            17,
            _CAN_FILTER,
            0,
            0,
            1,
            1,
            *self._rx_id.to_bytes(4, "big"),
            *(0x7FF).to_bytes(4, "big"),
            *self._tx_id.to_bytes(4, "big"),
        )
        self._set(CMD_OBD, 2, 2, 1)
