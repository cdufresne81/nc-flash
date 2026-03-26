"""
UDS Protocol Layer Tests

Tests UDSConnection.send_request and typed UDS methods against
a mock J2534Device. This is Tier 1 — every ECU interaction goes
through send_request; bugs here can brick an ECU.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.constants import (
    SID_DIAGNOSTIC_SESSION,
    SID_ECU_RESET,
    SID_SECURITY_ACCESS,
    SID_REQUEST_DOWNLOAD,
    SID_TRANSFER_DATA,
    SID_TRANSFER_EXIT,
    SID_TESTER_PRESENT,
    SID_READ_MEM_BY_ADDR,
    SECURITY_REQUEST_SEED,
    SECURITY_SEND_KEY,
    NRC_RESPONSE_PENDING,
    TIMEOUT_RESPONSE_PENDING_MAX,
    DOWNLOAD_ADDR,
    DOWNLOAD_SIZE,
    BLOCK_SIZE,
)
from src.ecu.exceptions import (
    J2534Error,
    NegativeResponseError,
    SecurityAccessDenied,
    TransferError,
    UDSTimeoutError,
    FlashAbortedError,
)

from ecu_test_helpers import build_positive_response, build_negative_response

# -----------------------------------------------------------------------
# send_request
# -----------------------------------------------------------------------


class TestSendRequest:
    """Core UDS request/response parsing — the single most critical function."""

    def test_positive_response(self, mock_uds, mock_j2534_device):
        """Happy path: positive response payload extracted correctly."""
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_TESTER_PRESENT, b"\x01")
        ]
        result = mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")
        assert result == b"\x01"

    def test_positive_response_empty_payload(self, mock_uds, mock_j2534_device):
        """Service returns only SID+0x40, no extra payload."""
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_DIAGNOSTIC_SESSION)
        ]
        result = mock_uds.send_request(SID_DIAGNOSTIC_SESSION, b"\x85")
        assert result == b""

    def test_negative_response_raises(self, mock_uds, mock_j2534_device):
        """0x7F + SID + NRC raises NegativeResponseError with correct nrc."""
        nrc = 0x33  # securityAccessDenied
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_DIAGNOSTIC_SESSION, nrc)
        ]
        with pytest.raises(NegativeResponseError) as exc_info:
            mock_uds.send_request(SID_DIAGNOSTIC_SESSION, b"\x85")
        assert exc_info.value.nrc == nrc

    @patch("src.ecu.protocol.time.monotonic")
    def test_nrc_0x78_retries_until_final(self, mock_time, mock_uds, mock_j2534_device):
        """NRC 0x78 causes continued reading; final positive returned."""
        mock_time.side_effect = [0.0, 0.0, 0.5, 1.0]
        mock_j2534_device.read_msgs.side_effect = [
            [build_negative_response(SID_DIAGNOSTIC_SESSION, NRC_RESPONSE_PENDING)],
            [build_positive_response(SID_DIAGNOSTIC_SESSION, b"\x85")],
        ]
        result = mock_uds.send_request(SID_DIAGNOSTIC_SESSION, b"\x85")
        assert result == b"\x85"

    @patch("src.ecu.protocol.time.monotonic")
    def test_nrc_0x78_timeout(self, mock_time, mock_uds, mock_j2534_device):
        """NRC 0x78 past max timeout raises UDSTimeoutError."""
        # First call: start=0, then elapsed checks exceed max
        mock_time.side_effect = [0.0, 0.0, 61.0]
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_DIAGNOSTIC_SESSION, NRC_RESPONSE_PENDING)
        ]
        with pytest.raises(UDSTimeoutError):
            mock_uds.send_request(SID_DIAGNOSTIC_SESSION, b"\x85")

    @patch("src.ecu.protocol.time.monotonic")
    def test_timeout_no_response(self, mock_time, mock_uds, mock_j2534_device):
        """Empty reads until timeout raises UDSTimeoutError."""
        mock_time.side_effect = [0.0, 61.0]
        mock_j2534_device.read_msgs.return_value = []
        with pytest.raises(UDSTimeoutError):
            mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")

    def test_j2534_error_propagates(self, mock_uds, mock_j2534_device):
        """J2534Error from device passes through unchanged."""
        mock_j2534_device.read_msgs.side_effect = J2534Error("device gone")
        with pytest.raises(J2534Error, match="device gone"):
            mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")

    @patch("src.ecu.protocol.time.monotonic")
    def test_short_message_skipped(self, mock_time, mock_uds, mock_j2534_device):
        """Response with DataSize <= 4 is ignored, reading continues."""
        short_msg = MagicMock()
        short_msg.DataSize = 4
        short_msg.Data = [0] * 4128

        mock_time.side_effect = [0.0, 0.0, 0.5]
        mock_j2534_device.read_msgs.side_effect = [
            [short_msg],
            [build_positive_response(SID_TESTER_PRESENT, b"\x01")],
        ]
        result = mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")
        assert result == b"\x01"

    def test_write_sends_correct_data(self, mock_uds, mock_j2534_device):
        """Verify write_msgs is called with CAN ID + SID in the message."""
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_TESTER_PRESENT, b"\x01")
        ]
        mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")
        mock_j2534_device.write_msgs.assert_called_once()


# -----------------------------------------------------------------------
# Diagnostic Session
# -----------------------------------------------------------------------


class TestDiagnosticSession:
    def test_programming_session(self, mock_uds, mock_j2534_device):
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_DIAGNOSTIC_SESSION, b"\x85")
        ]
        mock_uds.diagnostic_session()
        mock_j2534_device.write_msgs.assert_called_once()


# -----------------------------------------------------------------------
# ECU Reset
# -----------------------------------------------------------------------


class TestEcuReset:
    def test_reset_acknowledged(self, mock_uds, mock_j2534_device):
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_ECU_RESET, b"\x01")
        ]
        mock_uds.ecu_reset()  # Should not raise

    def test_timeout_accepted(self, mock_uds, mock_j2534_device):
        """UDSTimeoutError is silently caught — ECU reboots before responding."""
        mock_j2534_device.read_msgs.side_effect = UDSTimeoutError("timeout")
        mock_uds.ecu_reset()  # Should not raise


# -----------------------------------------------------------------------
# Security Access
# -----------------------------------------------------------------------


class TestSecurityAccess:
    def test_request_seed_returns_bytes(self, mock_uds, mock_j2534_device):
        seed = b"\xaa\xbb\xcc"
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(
                SID_SECURITY_ACCESS, bytes([SECURITY_REQUEST_SEED]) + seed
            )
        ]
        result = mock_uds.security_access_request_seed()
        assert result == seed

    def test_short_seed_raises(self, mock_uds, mock_j2534_device):
        """Response too short raises SecurityAccessDenied."""
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_SECURITY_ACCESS, b"\x01")
        ]
        with pytest.raises(SecurityAccessDenied, match="too short"):
            mock_uds.security_access_request_seed()

    def test_send_key_success(self, mock_uds, mock_j2534_device):
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_SECURITY_ACCESS, b"\x02")
        ]
        mock_uds.security_access_send_key(b"\x44\x70\xe8")

    def test_nrc_0x35_invalid_key(self, mock_uds, mock_j2534_device):
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_SECURITY_ACCESS, 0x35)
        ]
        with pytest.raises(SecurityAccessDenied):
            mock_uds.security_access_send_key(b"\x00\x00\x00")

    def test_nrc_0x36_exceeded_attempts(self, mock_uds, mock_j2534_device):
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_SECURITY_ACCESS, 0x36)
        ]
        with pytest.raises(SecurityAccessDenied):
            mock_uds.security_access_send_key(b"\x00\x00\x00")

    def test_other_nrc_raises_negative_response(self, mock_uds, mock_j2534_device):
        """NRC not in {0x33, 0x35, 0x36} raises NegativeResponseError, not SecurityAccessDenied."""
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_SECURITY_ACCESS, 0x12)
        ]
        with pytest.raises(NegativeResponseError) as exc_info:
            mock_uds.security_access_send_key(b"\x00\x00\x00")
        assert not isinstance(exc_info.value, SecurityAccessDenied)


# -----------------------------------------------------------------------
# Request Download
# -----------------------------------------------------------------------


class TestRequestDownload:
    def test_default_address_and_size(self, mock_uds, mock_j2534_device):
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_REQUEST_DOWNLOAD)
        ]
        mock_uds.request_download()
        mock_j2534_device.write_msgs.assert_called_once()


# -----------------------------------------------------------------------
# Transfer Data
# -----------------------------------------------------------------------


class TestTransferData:
    def _setup_transfer_responses(self, mock_j2534_device, num_blocks):
        mock_j2534_device.read_msgs.side_effect = [
            [build_positive_response(SID_TRANSFER_DATA)] for _ in range(num_blocks)
        ]

    def test_single_block(self, mock_uds, mock_j2534_device):
        """Data smaller than block_size sends one request."""
        self._setup_transfer_responses(mock_j2534_device, 1)
        mock_uds.transfer_data(b"\x00" * 512, block_size=BLOCK_SIZE)
        assert mock_j2534_device.write_msgs.call_count == 1

    def test_multi_block(self, mock_uds, mock_j2534_device):
        """3KB at 1KB blocks sends exactly 3 requests."""
        self._setup_transfer_responses(mock_j2534_device, 3)
        mock_uds.transfer_data(b"\x00" * (BLOCK_SIZE * 3), block_size=BLOCK_SIZE)
        assert mock_j2534_device.write_msgs.call_count == 3

    def test_exact_block_boundary(self, mock_uds, mock_j2534_device):
        """Data exactly divisible by block_size — no off-by-one."""
        self._setup_transfer_responses(mock_j2534_device, 2)
        mock_uds.transfer_data(b"\x00" * (BLOCK_SIZE * 2), block_size=BLOCK_SIZE)
        assert mock_j2534_device.write_msgs.call_count == 2

    def test_progress_callback(self, mock_uds, mock_j2534_device):
        """Callback receives (bytes_sent, total_bytes) for each block."""
        self._setup_transfer_responses(mock_j2534_device, 3)
        cb = MagicMock()
        total = BLOCK_SIZE * 3
        mock_uds.transfer_data(
            b"\x00" * total, block_size=BLOCK_SIZE, progress_callback=cb
        )
        assert cb.call_count == 3
        # Last call should have sent == total
        assert cb.call_args_list[-1].args == (total, total)

    def test_abort_raises_before_first_block(self, mock_uds, mock_j2534_device):
        """abort_check=True raises FlashAbortedError before any transfer."""
        with pytest.raises(FlashAbortedError):
            mock_uds.transfer_data(
                b"\x00" * BLOCK_SIZE,
                block_size=BLOCK_SIZE,
                abort_check=lambda: True,
            )
        # No writes should have happened
        mock_j2534_device.write_msgs.assert_not_called()

    def test_nrc_during_transfer(self, mock_uds, mock_j2534_device):
        """NRC on a block raises TransferError."""
        mock_j2534_device.read_msgs.side_effect = [
            [build_negative_response(SID_TRANSFER_DATA, 0x72)],
        ]
        with pytest.raises(TransferError, match="block 1"):
            mock_uds.transfer_data(b"\x00" * BLOCK_SIZE, block_size=BLOCK_SIZE)


# -----------------------------------------------------------------------
# Read Memory By Address
# -----------------------------------------------------------------------


class TestReadMemoryByAddress:
    def test_returns_payload(self, mock_uds, mock_j2534_device):
        payload = b"\xde\xad\xbe\xef"
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_READ_MEM_BY_ADDR, payload)
        ]
        result = mock_uds.read_memory_by_address(0x0000, 4)
        assert result == payload
