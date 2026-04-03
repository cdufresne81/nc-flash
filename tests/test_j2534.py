"""
Tests for src/ecu/j2534.py — J2534 PassThru device layer.

Tests the actual production code (message construction, error handling,
filter setup) rather than a MagicMock stand-in.
"""

from ctypes import c_ulong, byref
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.j2534 import (
    J2534Device,
    PassThruMsg,
    build_isotp_msg,
    _build_can_id_msg,
    setup_isotp_flow_control,
    _ERROR_DESCRIPTIONS,
    J2534_STATUS_NOERROR,
)
from src.ecu.constants import (
    J2534_PROTOCOL_ISO15765,
    ISO15765_TX_FLAGS,
    CAN_REQUEST_ID,
    CAN_RESPONSE_ID,
    FLOW_CONTROL_FILTER,
)
from src.ecu.exceptions import J2534Error, J2534ConnectionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device_no_init():
    """Create a J2534Device instance without calling __init__.

    Avoids DLL loading / bridge auto-build. Sets the minimum attributes
    needed for method calls under test.
    """
    dev = J2534Device.__new__(J2534Device)
    dev._dll = MagicMock()
    dev._dll_path = "test.dll"
    dev._bridge = None
    dev._device_id = None
    dev._funcs = {"PassThruGetLastError": None}
    return dev


def _get_data_bytes(msg: PassThruMsg, count: int) -> bytes:
    """Extract the first `count` bytes from a PassThruMsg.Data field."""
    return bytes(msg.Data[i] for i in range(count))


# ---------------------------------------------------------------------------
# Tier 1 — Pure Logic (no hardware mocking)
# ---------------------------------------------------------------------------


class TestBuildIsotpMsg:
    """Test build_isotp_msg() message construction."""

    def test_default_tx_id(self):
        """Default CAN ID (0x7E0) is packed big-endian in Data[0:4]."""
        msg = build_isotp_msg(b"\x3E\x01")
        assert _get_data_bytes(msg, 4) == b"\x00\x00\x07\xE0"

    def test_payload_at_offset_4(self):
        """Payload bytes start at Data[4]."""
        msg = build_isotp_msg(b"\x3E\x01")
        assert _get_data_bytes(msg, 6)[4:] == b"\x3E\x01"

    def test_data_size_includes_can_id(self):
        """DataSize = 4 (CAN ID) + payload length."""
        msg = build_isotp_msg(b"\x10\x85")
        assert msg.DataSize == 6

    def test_custom_tx_id(self):
        """Custom CAN ID is packed correctly."""
        msg = build_isotp_msg(b"\x01", tx_id=0x7DF)
        assert _get_data_bytes(msg, 4) == b"\x00\x00\x07\xDF"

    def test_empty_payload(self):
        """Empty payload: DataSize = 4 (CAN ID only)."""
        msg = build_isotp_msg(b"")
        assert msg.DataSize == 4

    def test_large_payload(self):
        """256-byte payload: DataSize = 260, all bytes preserved."""
        payload = bytes(range(256))
        msg = build_isotp_msg(payload)
        assert msg.DataSize == 260
        assert _get_data_bytes(msg, 260)[4:] == payload

    def test_protocol_id(self):
        """ProtocolID set to ISO15765."""
        msg = build_isotp_msg(b"\x00")
        assert msg.ProtocolID == J2534_PROTOCOL_ISO15765

    def test_tx_flags(self):
        """TxFlags set to ISO15765_TX_FLAGS."""
        msg = build_isotp_msg(b"\x00")
        assert msg.TxFlags == ISO15765_TX_FLAGS


class TestBuildCanIdMsg:
    """Test _build_can_id_msg() helper."""

    def test_can_id_big_endian(self):
        """CAN ID 0x7E8 packed as big-endian bytes."""
        msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, 0x7E8)
        assert _get_data_bytes(msg, 4) == b"\x00\x00\x07\xE8"

    def test_data_size_is_4(self):
        """DataSize is always 4 (just the CAN ID)."""
        msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, 0x7E0)
        assert msg.DataSize == 4

    def test_mask_all_ones(self):
        """0xFFFFFFFF mask packs correctly."""
        msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, 0xFFFFFFFF)
        assert _get_data_bytes(msg, 4) == b"\xFF\xFF\xFF\xFF"

    def test_protocol_set(self):
        """Protocol ID passed through to message."""
        msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, 0x7E0)
        assert msg.ProtocolID == J2534_PROTOCOL_ISO15765


class TestCheckError:
    """Test _check_error() error code → exception mapping."""

    def _make_device(self):
        return _make_device_no_init()

    def test_noerror_does_not_raise(self):
        """Status code 0 (NOERROR) passes silently."""
        dev = self._make_device()
        dev._check_error(J2534_STATUS_NOERROR, "TestFunc")

    @pytest.mark.parametrize(
        "code,description",
        [
            (code, desc)
            for code, desc in _ERROR_DESCRIPTIONS.items()
            if code != J2534_STATUS_NOERROR
        ],
        ids=[
            _ERROR_DESCRIPTIONS[c]
            for c in _ERROR_DESCRIPTIONS
            if c != J2534_STATUS_NOERROR
        ],
    )
    def test_known_error_code_raises(self, code, description):
        """Each known error code raises J2534Error with its description."""
        dev = self._make_device()
        with pytest.raises(J2534Error, match=description):
            dev._check_error(code, "TestFunc")

    def test_unknown_error_code(self):
        """Unknown error code includes hex representation."""
        dev = self._make_device()
        with pytest.raises(J2534Error, match="UNKNOWN_ERROR_0xFF"):
            dev._check_error(0xFF, "TestFunc")

    def test_function_name_in_message(self):
        """The function name appears in the error message."""
        dev = self._make_device()
        with pytest.raises(J2534Error, match="PassThruReadMsgs"):
            dev._check_error(0x07, "PassThruReadMsgs")

    def test_last_error_appended(self):
        """_get_last_error text is appended when available."""
        dev = self._make_device()
        # Give it a working PassThruGetLastError that returns a string
        mock_func = MagicMock()

        def fill_buffer(buf):
            text = b"DLL internal error detail"
            for i, b in enumerate(text):
                buf[i] = b
            return 0

        mock_func.side_effect = fill_buffer
        dev._funcs["PassThruGetLastError"] = mock_func

        with pytest.raises(J2534Error, match="DLL internal error detail"):
            dev._check_error(0x07, "TestFunc")


class TestSetupIsotpFlowControl:
    """Test setup_isotp_flow_control() filter configuration."""

    def test_calls_start_msg_filter(self):
        """Calls device.start_msg_filter with correct filter type."""
        mock_dev = MagicMock()
        mock_dev.start_msg_filter.return_value = 42

        result = setup_isotp_flow_control(mock_dev, channel_id=5)

        mock_dev.start_msg_filter.assert_called_once()
        args = mock_dev.start_msg_filter.call_args
        assert args[0][0] == 5  # channel_id
        assert args[0][1] == FLOW_CONTROL_FILTER  # filter_type

    def test_mask_is_all_ones(self):
        """Mask message has 0xFFFFFFFF in CAN ID field."""
        mock_dev = MagicMock()
        mock_dev.start_msg_filter.return_value = 1
        setup_isotp_flow_control(mock_dev, 1)

        mask_msg = mock_dev.start_msg_filter.call_args[0][2]
        assert _get_data_bytes(mask_msg, 4) == b"\xFF\xFF\xFF\xFF"

    def test_pattern_is_response_id(self):
        """Pattern message has CAN_RESPONSE_ID (0x7E8)."""
        mock_dev = MagicMock()
        mock_dev.start_msg_filter.return_value = 1
        setup_isotp_flow_control(mock_dev, 1)

        pattern_msg = mock_dev.start_msg_filter.call_args[0][3]
        assert _get_data_bytes(pattern_msg, 4) == CAN_RESPONSE_ID.to_bytes(4, "big")

    def test_flow_control_is_request_id(self):
        """Flow control message has CAN_REQUEST_ID (0x7E0)."""
        mock_dev = MagicMock()
        mock_dev.start_msg_filter.return_value = 1
        setup_isotp_flow_control(mock_dev, 1)

        fc_msg = mock_dev.start_msg_filter.call_args[0][4]
        assert _get_data_bytes(fc_msg, 4) == CAN_REQUEST_ID.to_bytes(4, "big")

    def test_returns_filter_id(self):
        """Returns the filter ID from device.start_msg_filter."""
        mock_dev = MagicMock()
        mock_dev.start_msg_filter.return_value = 99
        assert setup_isotp_flow_control(mock_dev, 1) == 99


# ---------------------------------------------------------------------------
# Tier 2 — Mocked DLL Calls
# ---------------------------------------------------------------------------


class TestReadMsgs:
    """Test read_msgs() with mocked DLL functions."""

    def _setup_device(self, read_return=0):
        """Create a device with mocked PassThruReadMsgs."""
        dev = _make_device_no_init()
        dev._device_id = 1
        mock_func = MagicMock(return_value=read_return)
        dev._funcs["PassThruReadMsgs"] = mock_func
        return dev, mock_func

    def test_timeout_returns_empty(self):
        """ERR_TIMEOUT (0x09) is non-fatal — returns empty list."""
        dev, _ = self._setup_device(read_return=0x09)
        result = dev.read_msgs(channel_id=1, count=1, timeout=100)
        assert result == []

    def test_buffer_empty_returns_empty(self):
        """ERR_BUFFER_EMPTY (0x10) is non-fatal — returns empty list."""
        dev, _ = self._setup_device(read_return=0x10)
        result = dev.read_msgs(channel_id=1, count=1, timeout=100)
        assert result == []

    def test_other_error_raises(self):
        """ERR_FAILED (0x07) raises J2534Error."""
        dev, _ = self._setup_device(read_return=0x07)
        with pytest.raises(J2534Error, match="ERR_FAILED"):
            dev.read_msgs(channel_id=1, count=1, timeout=100)

    def test_success_returns_messages(self):
        """NOERROR returns the read messages."""
        dev, mock_func = self._setup_device(read_return=0)

        # The DLL function modifies num_msgs via pointer — simulate that
        def side_effect(ch, msgs_ptr, num_ptr, timeout):
            # num_msgs stays at its initial value (count=1)
            return 0

        mock_func.side_effect = side_effect
        result = dev.read_msgs(channel_id=1, count=1, timeout=100)
        assert len(result) == 1
        assert isinstance(result[0], PassThruMsg)


class TestWriteMsgs:
    """Test write_msgs() with mocked DLL functions."""

    def test_empty_list_noop(self):
        """Empty message list does nothing (no DLL call)."""
        dev = _make_device_no_init()
        dev._device_id = 1
        mock_func = MagicMock(return_value=0)
        dev._funcs["PassThruWriteMsgs"] = mock_func

        dev.write_msgs(channel_id=1, msgs=[], timeout=100)
        mock_func.assert_not_called()

    def test_error_raises(self):
        """Write error propagates as J2534Error."""
        dev = _make_device_no_init()
        dev._device_id = 1
        dev._funcs["PassThruWriteMsgs"] = MagicMock(return_value=0x07)

        msg = build_isotp_msg(b"\x3E\x01")
        with pytest.raises(J2534Error, match="ERR_FAILED"):
            dev.write_msgs(channel_id=1, msgs=[msg], timeout=100)

    def test_success_no_exception(self):
        """Successful write raises no exception."""
        dev = _make_device_no_init()
        dev._device_id = 1
        dev._funcs["PassThruWriteMsgs"] = MagicMock(return_value=0)

        msg = build_isotp_msg(b"\x3E\x01")
        dev.write_msgs(channel_id=1, msgs=[msg], timeout=100)


class TestOpenClose:
    """Test device open/close lifecycle."""

    def test_double_close_safe(self):
        """Calling close() twice does not raise."""
        dev = _make_device_no_init()
        dev._device_id = None  # Already closed
        dev.close()  # Should be a no-op
        dev.close()  # Still a no-op

    def test_close_catches_dll_error(self):
        """close() logs but does not raise on DLL error."""
        dev = _make_device_no_init()
        dev._device_id = 1
        dev._funcs["PassThruClose"] = MagicMock(side_effect=OSError("USB gone"))
        dev.close()  # Should not raise
        assert dev._device_id is None


class TestConnectDisconnect:
    """Test channel connect/disconnect."""

    def test_connect_requires_open_device(self):
        """connect() raises when device is not open."""
        dev = _make_device_no_init()
        dev._device_id = None
        with pytest.raises(J2534Error, match="not open"):
            dev.connect(J2534_PROTOCOL_ISO15765, 0, 500000)

    def test_connect_returns_channel_id(self):
        """connect() returns the channel ID from the DLL."""
        dev = _make_device_no_init()
        dev._device_id = 1

        def mock_connect(dev_id, proto, flags, baud, ch_ptr):
            # Simulate DLL setting channel_id via pointer
            from ctypes import cast, POINTER, c_ulong

            ptr = cast(ch_ptr, POINTER(c_ulong))
            ptr[0] = 7  # channel_id = 7
            return 0

        dev._funcs["PassThruConnect"] = MagicMock(side_effect=mock_connect)
        ch = dev.connect(J2534_PROTOCOL_ISO15765, 0, 500000)
        assert ch == 7
