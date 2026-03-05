"""
J2534 PassThru DLL Wrapper

Provides a ctypes-based interface to J2534 PassThru device drivers on Windows.
Default target: Tactrix OpenPort 2.0 (op20pt32.dll).

This is safety-critical code used for ECU communication and flash operations.
All function calls validate return codes and raise descriptive exceptions on failure.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_ulong,
    c_ubyte,
)
from typing import Optional

from .constants import (
    J2534_STATUS_NOERROR,
    PASSTHRU_MSG_DATA_SIZE,
    J2534_PROTOCOL_ISO15765,
    CAN_BAUDRATE,
    CAN_REQUEST_ID,
    CAN_RESPONSE_ID,
    PASS_FILTER,
    BLOCK_FILTER,
    FLOW_CONTROL_FILTER,
    GET_CONFIG,
    SET_CONFIG,
    CLEAR_TX_BUFFER,
    CLEAR_RX_BUFFER,
    ISO15765_BS,
    ISO15765_STMIN,
    ISO15765_TX_FLAGS,
    DEFAULT_J2534_DLL,
)
from .exceptions import (
    J2534Error,
    J2534DLLNotFound,
    J2534DeviceNotFound,
    J2534ConnectionError,
)

logger = logging.getLogger(__name__)

# J2534 error code descriptions
_ERROR_DESCRIPTIONS = {
    0x00: "STATUS_NOERROR",
    0x01: "ERR_NOT_SUPPORTED",
    0x02: "ERR_INVALID_CHANNEL_ID",
    0x03: "ERR_INVALID_PROTOCOL_ID",
    0x04: "ERR_NULL_PARAMETER",
    0x05: "ERR_INVALID_IOCTL_VALUE",
    0x06: "ERR_INVALID_FLAGS",
    0x07: "ERR_FAILED",
    0x08: "ERR_DEVICE_NOT_CONNECTED",
    0x09: "ERR_TIMEOUT",
    0x0A: "ERR_INVALID_MSG",
    0x0B: "ERR_INVALID_TIME_INTERVAL",
    0x0C: "ERR_EXCEEDED_LIMIT",
    0x0D: "ERR_INVALID_MSG_ID",
    0x0E: "ERR_DEVICE_IN_USE",
    0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY",
    0x11: "ERR_BUFFER_FULL",
    0x12: "ERR_BUFFER_OVERFLOW",
    0x13: "ERR_PIN_INVALID",
    0x14: "ERR_CHANNEL_IN_USE",
    0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID",
    0x17: "ERR_NO_FLOW_CONTROL",
    0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE",
    0x1A: "ERR_INVALID_DEVICE_ID",
}


# ---------------------------------------------------------------------------
# ctypes Structure Definitions
# ---------------------------------------------------------------------------


class PassThruMsg(Structure):
    """J2534 PASSTHRU_MSG structure for CAN/ISO-15765 message exchange."""

    _fields_ = [
        ("ProtocolID", c_ulong),
        ("RxStatus", c_ulong),
        ("TxFlags", c_ulong),
        ("Timestamp", c_ulong),
        ("DataSize", c_ulong),
        ("ExtraDataIndex", c_ulong),
        ("Data", c_ubyte * PASSTHRU_MSG_DATA_SIZE),
    ]


# Alias for external use
PASSTHRU_MSG = PassThruMsg


class SCONFIG(Structure):
    """J2534 SCONFIG structure for a single configuration parameter."""

    _fields_ = [
        ("Parameter", c_ulong),
        ("Value", c_ulong),
    ]


class SCONFIG_LIST(Structure):
    """J2534 SCONFIG_LIST structure for batch configuration operations."""

    _fields_ = [
        ("NumOfParams", c_ulong),
        ("ConfigPtr", POINTER(SCONFIG)),
    ]


# J2534 PassThru function names as defined in the SAE J2534 specification
_PASSTHRU_FUNCTIONS = [
    "PassThruOpen",
    "PassThruClose",
    "PassThruConnect",
    "PassThruDisconnect",
    "PassThruReadMsgs",
    "PassThruWriteMsgs",
    "PassThruStartPeriodicMsg",
    "PassThruStopPeriodicMsg",
    "PassThruStartMsgFilter",
    "PassThruStopMsgFilter",
    "PassThruSetProgrammingVoltage",
    "PassThruReadVersion",
    "PassThruGetLastError",
    "PassThruIoctl",
]


class J2534Device:
    """
    J2534 PassThru device interface.

    Wraps a J2534-compliant DLL to provide ECU communication over CAN/ISO-15765.
    Supports context manager protocol for automatic resource cleanup.

    Usage:
        with J2534Device() as dev:
            ch = dev.connect(J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE)
            ...
            dev.disconnect(ch)
    """

    def __init__(self, dll_path: str = DEFAULT_J2534_DLL):
        self._dll_path = dll_path
        self._dll = None
        self._funcs: dict = {}
        self._device_id: Optional[int] = None

    def __enter__(self) -> J2534Device:
        self.open()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.close()
        return None

    # ------------------------------------------------------------------
    # DLL Loading
    # ------------------------------------------------------------------

    def _ensure_dll(self) -> None:
        """Lazy-load the J2534 DLL on first use."""
        if self._dll is not None:
            return

        try:
            try:
                self._dll = ctypes.WinDLL(self._dll_path)
            except AttributeError:
                # Non-Windows or WinDLL unavailable — fall back to CDLL
                self._dll = ctypes.CDLL(self._dll_path)
        except OSError as e:
            raise J2534DLLNotFound(
                f"Cannot load J2534 DLL '{self._dll_path}': {e}"
            ) from e

        self._resolve_functions()

    def _resolve_functions(self) -> None:
        """Resolve all 14 PassThru function pointers from the loaded DLL."""
        for name in _PASSTHRU_FUNCTIONS:
            try:
                func = getattr(self._dll, name)
                self._funcs[name] = func
            except AttributeError:
                logger.warning("J2534 function '%s' not found in DLL", name)
                self._funcs[name] = None

    # ------------------------------------------------------------------
    # Error Handling
    # ------------------------------------------------------------------

    def _check_error(self, result: int, function_name: str) -> None:
        """
        Validate a J2534 return code.

        Raises J2534Error with a descriptive message if the result is non-zero.
        Maps known error codes to SAE J2534 error names.
        """
        if result == J2534_STATUS_NOERROR:
            return

        desc = _ERROR_DESCRIPTIONS.get(result, f"UNKNOWN_ERROR_0x{result:02X}")
        last_error = self._get_last_error()
        msg = f"{function_name} failed: {desc} (code {result})"
        if last_error:
            msg += f" — {last_error}"
        logger.error(msg)
        raise J2534Error(msg)

    def _get_last_error(self) -> str:
        """Retrieve the last error description string from the DLL."""
        func = self._funcs.get("PassThruGetLastError")
        if func is None:
            return ""
        try:
            buf = ctypes.create_string_buffer(256)
            func(buf)
            return buf.value.decode("ascii", errors="replace").strip()
        except Exception:
            return ""

    def _require_func(self, name: str):
        """Return the resolved function pointer or raise J2534Error."""
        func = self._funcs.get(name)
        if func is None:
            raise J2534Error(
                f"J2534 function '{name}' is not available in the loaded DLL"
            )
        return func

    # ------------------------------------------------------------------
    # Device / Channel Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """
        Open a connection to the J2534 device.

        Stores the device ID for subsequent operations.
        Raises J2534DeviceNotFound if the device cannot be opened.
        """
        self._ensure_dll()

        func = self._require_func("PassThruOpen")
        device_id = c_ulong()

        try:
            result = func(None, byref(device_id))
        except OSError as e:
            raise J2534DeviceNotFound(
                f"Failed to communicate with J2534 device: {e}"
            ) from e

        try:
            self._check_error(result, "PassThruOpen")
        except J2534Error as e:
            raise J2534DeviceNotFound(str(e)) from e

        self._device_id = device_id.value
        logger.info("Opened J2534 device (ID=%d)", self._device_id)

    def close(self) -> None:
        """
        Close the J2534 device connection.

        Safe to call multiple times. Logs but does not raise on failure,
        since close is often called during cleanup.
        """
        if self._device_id is None:
            return

        func = self._funcs.get("PassThruClose")
        if func is not None:
            try:
                result = func(c_ulong(self._device_id))
                if result != J2534_STATUS_NOERROR:
                    desc = _ERROR_DESCRIPTIONS.get(result, f"0x{result:02X}")
                    logger.warning("PassThruClose returned %s", desc)
            except OSError as e:
                logger.warning("PassThruClose raised OSError: %s", e)

        logger.info("Closed J2534 device (ID=%d)", self._device_id)
        self._device_id = None

    def connect(self, protocol: int, flags: int, baudrate: int) -> int:
        """
        Open a communication channel on the device.

        Args:
            protocol: J2534 protocol ID (e.g. J2534_PROTOCOL_ISO15765).
            flags: Connection flags (typically 0).
            baudrate: Channel baud rate (e.g. 500000 for CAN).

        Returns:
            Channel ID for use in subsequent read/write/filter operations.

        Raises:
            J2534ConnectionError: If the connection cannot be established.
        """
        if self._device_id is None:
            raise J2534Error("Device not open — call open() first")

        self._ensure_dll()
        func = self._require_func("PassThruConnect")
        channel_id = c_ulong()

        try:
            result = func(
                c_ulong(self._device_id),
                c_ulong(protocol),
                c_ulong(flags),
                c_ulong(baudrate),
                byref(channel_id),
            )
        except OSError as e:
            raise J2534ConnectionError(f"PassThruConnect failed: {e}") from e

        try:
            self._check_error(result, "PassThruConnect")
        except J2534Error as e:
            raise J2534ConnectionError(str(e)) from e

        logger.info(
            "Connected channel %d (protocol=%d, baudrate=%d)",
            channel_id.value,
            protocol,
            baudrate,
        )
        return channel_id.value

    def disconnect(self, channel_id: int) -> None:
        """
        Disconnect a communication channel.

        Args:
            channel_id: Channel ID returned by connect().
        """
        self._ensure_dll()
        func = self._require_func("PassThruDisconnect")
        result = func(c_ulong(channel_id))
        self._check_error(result, "PassThruDisconnect")
        logger.info("Disconnected channel %d", channel_id)

    # ------------------------------------------------------------------
    # Message I/O
    # ------------------------------------------------------------------

    def read_msgs(
        self, channel_id: int, count: int = 1, timeout: int = 1000
    ) -> list[PassThruMsg]:
        """
        Read messages from a channel.

        Args:
            channel_id: Channel ID returned by connect().
            count: Maximum number of messages to read.
            timeout: Read timeout in milliseconds.

        Returns:
            List of received PassThruMsg instances (may be fewer than count).
        """
        self._ensure_dll()
        func = self._require_func("PassThruReadMsgs")

        msgs = (PassThruMsg * count)()
        num_msgs = c_ulong(count)

        result = func(
            c_ulong(channel_id),
            byref(msgs),
            byref(num_msgs),
            c_ulong(timeout),
        )

        # ERR_BUFFER_EMPTY (0x10) and ERR_TIMEOUT (0x09) are non-fatal for reads
        if result == 0x10 or result == 0x09:
            return []

        self._check_error(result, "PassThruReadMsgs")
        return [msgs[i] for i in range(num_msgs.value)]

    def write_msgs(
        self,
        channel_id: int,
        msgs: list[PassThruMsg],
        timeout: int = 1000,
    ) -> None:
        """
        Write messages to a channel.

        Args:
            channel_id: Channel ID returned by connect().
            msgs: List of PassThruMsg instances to transmit.
            timeout: Write timeout in milliseconds.
        """
        if not msgs:
            return

        self._ensure_dll()
        func = self._require_func("PassThruWriteMsgs")

        msg_array = (PassThruMsg * len(msgs))(*msgs)
        num_msgs = c_ulong(len(msgs))

        result = func(
            c_ulong(channel_id),
            byref(msg_array),
            byref(num_msgs),
            c_ulong(timeout),
        )
        self._check_error(result, "PassThruWriteMsgs")
        logger.debug("Wrote %d message(s) to channel %d", num_msgs.value, channel_id)

    # ------------------------------------------------------------------
    # Message Filters
    # ------------------------------------------------------------------

    def start_msg_filter(
        self,
        channel_id: int,
        filter_type: int,
        mask_msg: Optional[PassThruMsg],
        pattern_msg: Optional[PassThruMsg],
        flow_control_msg: Optional[PassThruMsg] = None,
    ) -> int:
        """
        Start a message filter on a channel.

        Args:
            channel_id: Channel ID returned by connect().
            filter_type: PASS_FILTER, BLOCK_FILTER, or FLOW_CONTROL_FILTER.
            mask_msg: Mask message (which bits to check).
            pattern_msg: Pattern message (expected bit values).
            flow_control_msg: Flow control message (required for ISO-15765).

        Returns:
            Filter ID for use with stop_msg_filter().
        """
        self._ensure_dll()
        func = self._require_func("PassThruStartMsgFilter")

        filter_id = c_ulong()

        mask_ptr = byref(mask_msg) if mask_msg is not None else None
        pattern_ptr = byref(pattern_msg) if pattern_msg is not None else None
        fc_ptr = byref(flow_control_msg) if flow_control_msg is not None else None

        result = func(
            c_ulong(channel_id),
            c_ulong(filter_type),
            mask_ptr,
            pattern_ptr,
            fc_ptr,
            byref(filter_id),
        )
        self._check_error(result, "PassThruStartMsgFilter")
        logger.info(
            "Started filter %d on channel %d (type=%d)",
            filter_id.value,
            channel_id,
            filter_type,
        )
        return filter_id.value

    def stop_msg_filter(self, channel_id: int, filter_id: int) -> None:
        """
        Stop a previously started message filter.

        Args:
            channel_id: Channel ID returned by connect().
            filter_id: Filter ID returned by start_msg_filter().
        """
        self._ensure_dll()
        func = self._require_func("PassThruStopMsgFilter")
        result = func(c_ulong(channel_id), c_ulong(filter_id))
        self._check_error(result, "PassThruStopMsgFilter")
        logger.info("Stopped filter %d on channel %d", filter_id, channel_id)

    # ------------------------------------------------------------------
    # Configuration (IOCTL)
    # ------------------------------------------------------------------

    def set_config(self, channel_id: int, params: dict[int, int]) -> None:
        """
        Set configuration parameters on a channel via SET_CONFIG IOCTL.

        Args:
            channel_id: Channel ID returned by connect().
            params: Dict mapping parameter IDs to values
                    (e.g. {ISO15765_BS: 0, ISO15765_STMIN: 0}).
        """
        if not params:
            return

        self._ensure_dll()

        config_array = (SCONFIG * len(params))()
        for i, (param_id, value) in enumerate(params.items()):
            config_array[i].Parameter = param_id
            config_array[i].Value = value

        config_list = SCONFIG_LIST()
        config_list.NumOfParams = len(params)
        config_list.ConfigPtr = ctypes.cast(config_array, POINTER(SCONFIG))

        self.ioctl(channel_id, SET_CONFIG, byref(config_list))
        logger.debug("Set %d config param(s) on channel %d", len(params), channel_id)

    def ioctl(
        self,
        channel_id: int,
        ioctl_id: int,
        input_data=None,
    ) -> None:
        """
        Execute a J2534 IOCTL command on a channel.

        Args:
            channel_id: Channel ID returned by connect().
            ioctl_id: IOCTL command ID (e.g. SET_CONFIG, CLEAR_TX_BUFFER).
            input_data: Optional ctypes pointer/byref to input data structure.
        """
        self._ensure_dll()
        func = self._require_func("PassThruIoctl")

        result = func(
            c_ulong(channel_id),
            c_ulong(ioctl_id),
            input_data,
            None,
        )
        self._check_error(result, "PassThruIoctl")


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def build_isotp_msg(data: bytes, tx_id: int = CAN_REQUEST_ID) -> PassThruMsg:
    """
    Build an ISO-15765 message for transmission.

    The first 4 bytes of the message Data field contain the CAN arbitration ID
    in big-endian byte order, followed by the payload.

    Args:
        data: Payload bytes to send (UDS request, etc.).
        tx_id: CAN arbitration ID (default 0x7E0).

    Returns:
        A populated PassThruMsg ready for write_msgs().
    """
    msg = PassThruMsg()
    msg.ProtocolID = J2534_PROTOCOL_ISO15765
    msg.TxFlags = ISO15765_TX_FLAGS
    # First 4 bytes are the CAN ID in big-endian
    id_bytes = tx_id.to_bytes(4, "big")
    for i, b in enumerate(id_bytes):
        msg.Data[i] = b
    for i, b in enumerate(data):
        msg.Data[4 + i] = b
    msg.DataSize = 4 + len(data)
    return msg


def _build_can_id_msg(protocol: int, can_id: int) -> PassThruMsg:
    """Build a PassThruMsg containing a 4-byte CAN ID for filter setup."""
    msg = PassThruMsg()
    msg.ProtocolID = protocol
    msg.TxFlags = ISO15765_TX_FLAGS
    id_bytes = can_id.to_bytes(4, "big")
    for i, b in enumerate(id_bytes):
        msg.Data[i] = b
    msg.DataSize = 4
    return msg


def setup_isotp_flow_control(device: J2534Device, channel_id: int) -> int:
    """
    Setup ISO-15765 flow control filter for ECU communication (0x7E0 -> 0x7E8).

    Configures the standard OBD-II flow control filter so that ISO-TP
    multi-frame communication works correctly between the tester (0x7E0)
    and the ECU (0x7E8).

    Args:
        device: An open J2534Device instance.
        channel_id: Channel ID returned by device.connect().

    Returns:
        Filter ID that can be used with device.stop_msg_filter().
    """
    mask_msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, 0xFFFFFFFF)
    pattern_msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, CAN_RESPONSE_ID)
    flow_control_msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, CAN_REQUEST_ID)

    return device.start_msg_filter(
        channel_id,
        FLOW_CONTROL_FILTER,
        mask_msg,
        pattern_msg,
        flow_control_msg,
    )
