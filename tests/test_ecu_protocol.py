"""
UDS Protocol Layer Tests

Tests UDSConnection.send_request and typed UDS methods against
a mock J2534Device. This is Tier 1 — every ECU interaction goes
through send_request; bugs here can brick an ECU.
"""

import logging
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
    SID_READ_DTC_COUNT,
    SID_READ_DTC_STATUS,
    SECURITY_REQUEST_SEED,
    SECURITY_SEND_KEY,
    NRC_CONDITIONS_NOT_CORRECT,
    NRC_RESPONSE_PENDING,
    TIMEOUT_RESPONSE_PENDING_MAX,
    PASSTHRU_MSG_DATA_SIZE,
    DOWNLOAD_ADDR,
    DOWNLOAD_SIZE,
    BLOCK_SIZE,
)
from src.ecu.exceptions import (
    J2534Error,
    NegativeResponseError,
    SecurityAccessDenied,
    TransferError,
    UDSError,
    UDSTimeoutError,
    FlashAbortedError,
)

from ecu_test_helpers import (
    build_positive_response,
    build_negative_response,
    build_uds_response,
)

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
# send_request — context-aware NRC quieting (quiet_nrcs)
# -----------------------------------------------------------------------


class TestSendRequestQuietNrcs:
    """quiet_nrcs demotes an *expected* NRC's generic warning to DEBUG.

    The NegativeResponseError is still raised — only the user-facing log
    level changes so a benign/handled refusal doesn't alarm during a flash.
    """

    def _nrc_records(self, caplog):
        return [
            r
            for r in caplog.records
            if r.name == "src.ecu.protocol" and "UDS NRC:" in r.getMessage()
        ]

    def test_quiet_nrc_logged_at_debug_not_warning(
        self, mock_uds, mock_j2534_device, caplog
    ):
        """NRC in quiet_nrcs: 'UDS NRC:' record is DEBUG, never WARNING."""
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_TESTER_PRESENT, NRC_CONDITIONS_NOT_CORRECT)
        ]
        with caplog.at_level(logging.DEBUG, logger="src.ecu.protocol"):
            with pytest.raises(NegativeResponseError) as exc_info:
                mock_uds.send_request(
                    SID_TESTER_PRESENT,
                    b"\x01",
                    quiet_nrcs={NRC_CONDITIONS_NOT_CORRECT},
                )
        assert exc_info.value.nrc == NRC_CONDITIONS_NOT_CORRECT
        nrc_records = self._nrc_records(caplog)
        assert nrc_records, "expected a 'UDS NRC:' record"
        assert all(r.levelno == logging.DEBUG for r in nrc_records)
        assert not any(r.levelno == logging.WARNING for r in nrc_records)

    def test_unlisted_nrc_still_warning(self, mock_uds, mock_j2534_device, caplog):
        """An NRC not in quiet_nrcs stays WARNING even when quiet_nrcs is set."""
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_SECURITY_ACCESS, 0x33)  # securityAccessDenied
        ]
        with caplog.at_level(logging.DEBUG, logger="src.ecu.protocol"):
            with pytest.raises(NegativeResponseError):
                mock_uds.send_request(
                    SID_SECURITY_ACCESS,
                    b"\x00",
                    quiet_nrcs={NRC_CONDITIONS_NOT_CORRECT},
                )
        nrc_records = self._nrc_records(caplog)
        assert nrc_records
        assert all(r.levelno == logging.WARNING for r in nrc_records)

    def test_no_quiet_nrcs_default_unchanged(self, mock_uds, mock_j2534_device, caplog):
        """Default (no quiet_nrcs): even 0x22 stays WARNING (back-compat).

        Guards that a flash/security 0x22 still alarms the user.
        """
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_TESTER_PRESENT, NRC_CONDITIONS_NOT_CORRECT)
        ]
        with caplog.at_level(logging.DEBUG, logger="src.ecu.protocol"):
            with pytest.raises(NegativeResponseError):
                mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")
        nrc_records = self._nrc_records(caplog)
        assert nrc_records
        assert all(r.levelno == logging.WARNING for r in nrc_records)


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


# -----------------------------------------------------------------------
# DTC Read — NRC 0x22 Handling
# -----------------------------------------------------------------------


class TestReadDtcNrc0x22:
    """NRC 0x22 (conditions not correct) on DTC reads returns empty results."""

    def test_read_dtc_count_nrc_0x22_returns_zero(self, mock_uds, mock_j2534_device):
        """read_dtc_count() returns 0 on NRC 0x22."""
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_READ_DTC_COUNT, NRC_CONDITIONS_NOT_CORRECT)
        ]
        result = mock_uds.read_dtc_count()
        assert result == 0

    def test_read_dtc_status_nrc_0x22_returns_empty(self, mock_uds, mock_j2534_device):
        """read_dtc_status() returns [] when ReadDTCByStatus gets NRC 0x22."""
        # First call: read_dtc_count returns count=1
        # Second call: ReadDTCByStatus (SID 0x18) gets NRC 0x22
        mock_j2534_device.read_msgs.side_effect = [
            [build_positive_response(SID_READ_DTC_COUNT, bytes([0x02, 0x00, 0x01]))],
            [build_negative_response(SID_READ_DTC_STATUS, NRC_CONDITIONS_NOT_CORRECT)],
        ]
        result = mock_uds.read_dtc_status()
        assert result == []

    def test_read_dtc_count_0x22_generic_warning_demoted(
        self, mock_uds, mock_j2534_device, caplog
    ):
        """The generic 'UDS NRC:' record for the handled 0x22 is DEBUG, not WARNING.

        The existing INFO softening still fires; the new behaviour is that no
        stray WARNING precedes it for the DTC-count service.
        """
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_READ_DTC_COUNT, NRC_CONDITIONS_NOT_CORRECT)
        ]
        with caplog.at_level(logging.DEBUG, logger="src.ecu.protocol"):
            assert mock_uds.read_dtc_count() == 0
        nrc_records = [r for r in caplog.records if "UDS NRC:" in r.getMessage()]
        assert nrc_records
        assert all(r.levelno == logging.DEBUG for r in nrc_records)
        # INFO softening still present.
        assert any(
            r.levelno == logging.INFO and "returning 0" in r.getMessage()
            for r in caplog.records
        )

    def test_read_dtc_status_0x22_generic_warning_demoted(
        self, mock_uds, mock_j2534_device, caplog
    ):
        """The generic 'UDS NRC:' for the handled 0x22 on ReadDTCByStatus is DEBUG."""
        mock_j2534_device.read_msgs.side_effect = [
            [build_positive_response(SID_READ_DTC_COUNT, bytes([0x02, 0x00, 0x01]))],
            [build_negative_response(SID_READ_DTC_STATUS, NRC_CONDITIONS_NOT_CORRECT)],
        ]
        with caplog.at_level(logging.DEBUG, logger="src.ecu.protocol"):
            assert mock_uds.read_dtc_status() == []
        nrc_records = [r for r in caplog.records if "UDS NRC:" in r.getMessage()]
        assert nrc_records
        assert all(r.levelno == logging.DEBUG for r in nrc_records)

    def test_read_dtc_count_other_nrc_raises(self, mock_uds, mock_j2534_device):
        """read_dtc_count() re-raises NRCs other than 0x22."""
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_READ_DTC_COUNT, 0x31)  # request out of range
        ]
        with pytest.raises(NegativeResponseError) as exc_info:
            mock_uds.read_dtc_count()
        assert exc_info.value.nrc == 0x31

    def test_read_dtc_status_other_nrc_raises(self, mock_uds, mock_j2534_device):
        """read_dtc_status() re-raises NRCs other than 0x22."""
        # First call: read_dtc_count returns count=1
        # Second call: ReadDTCByStatus gets NRC 0x10 (general reject)
        mock_j2534_device.read_msgs.side_effect = [
            [build_positive_response(SID_READ_DTC_COUNT, bytes([0x02, 0x00, 0x01]))],
            [build_negative_response(SID_READ_DTC_STATUS, 0x10)],
        ]
        with pytest.raises(NegativeResponseError) as exc_info:
            mock_uds.read_dtc_status()
        assert exc_info.value.nrc == 0x10


class TestReadMemoryByAddress:
    def test_returns_payload(self, mock_uds, mock_j2534_device):
        payload = b"\xde\xad\xbe\xef"
        mock_j2534_device.read_msgs.return_value = [
            build_positive_response(SID_READ_MEM_BY_ADDR, payload)
        ]
        result = mock_uds.read_memory_by_address(0x0000, 4)
        assert result == payload

    def test_passes_through_timeout_and_pending_max(self):
        """The read-retry path forwards its tight per-block budget to
        send_request so a dropped block fails fast instead of stalling."""
        from src.ecu.protocol import UDSConnection

        transport = MagicMock()
        transport.receive_message.return_value = build_positive_response(
            SID_READ_MEM_BY_ADDR, b"\x00\x01\x02\x03"
        ).Data[4:8]
        # Easiest: stub send_request and assert the kwargs are threaded through.
        uds = UDSConnection(transport)
        with patch.object(uds, "send_request", return_value=b"\x00") as sr:
            uds.read_memory_by_address(0x10, 4, timeout=1234, pending_max=2345)
        _args, kwargs = sr.call_args
        assert kwargs["timeout"] == 1234
        assert kwargs["pending_max"] == 2345


class TestUdsFlush:
    def test_flush_delegates_to_transport(self):
        """UDSConnection.flush() must delegate to the transport's flush()."""
        from src.ecu.protocol import UDSConnection

        transport = MagicMock()
        uds = UDSConnection(transport)
        uds.flush()
        transport.flush.assert_called_once_with()


# -----------------------------------------------------------------------
# PASSTHRU_MSG_DATA_SIZE (#44)
# -----------------------------------------------------------------------


class TestPassThruMsgDataSize:
    """Verify buffer constant matches SAE J2534-1 spec."""

    def test_data_size_matches_spec(self):
        assert PASSTHRU_MSG_DATA_SIZE == 4128

    def test_passthru_msg_struct_layout(self):
        """PassThruMsg.Data array uses the spec-correct size."""
        from ctypes import sizeof
        from src.ecu.j2534 import PassThruMsg

        # 6 x c_ulong (24 bytes header) + 4128 data bytes
        assert sizeof(PassThruMsg) == 6 * 4 + PASSTHRU_MSG_DATA_SIZE


# -----------------------------------------------------------------------
# Malformed NRC handling (#46)
# -----------------------------------------------------------------------


class TestMalformedNegativeResponse:
    """Short 0x7F responses should raise UDSError immediately, not spin."""

    def test_single_byte_0x7f_raises(self, mock_uds, mock_j2534_device):
        """1-byte [0x7F] response raises UDSError."""
        mock_j2534_device.read_msgs.return_value = [build_uds_response(bytes([0x7F]))]
        with pytest.raises(UDSError, match="[Mm]alformed"):
            mock_uds.send_request(SID_TESTER_PRESENT, b"\x01")

    def test_two_byte_0x7f_raises(self, mock_uds, mock_j2534_device):
        """2-byte [0x7F, SID] response raises UDSError."""
        mock_j2534_device.read_msgs.return_value = [
            build_uds_response(bytes([0x7F, SID_DIAGNOSTIC_SESSION]))
        ]
        with pytest.raises(UDSError, match="[Mm]alformed"):
            mock_uds.send_request(SID_DIAGNOSTIC_SESSION, b"\x85")

    def test_three_byte_nrc_still_works(self, mock_uds, mock_j2534_device):
        """Normal 3-byte NRC [0x7F, SID, NRC] still raises NegativeResponseError."""
        mock_j2534_device.read_msgs.return_value = [
            build_negative_response(SID_DIAGNOSTIC_SESSION, 0x33)
        ]
        with pytest.raises(NegativeResponseError) as exc_info:
            mock_uds.send_request(SID_DIAGNOSTIC_SESSION, b"\x85")
        assert exc_info.value.nrc == 0x33
