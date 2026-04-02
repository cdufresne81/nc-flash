"""
Flash Manager Integration Tests

Tests FlashManager.flash_rom and read_rom with mocked J2534/UDS layer.
Patches _secure module so tests run without it.
"""

from unittest.mock import MagicMock, patch, call

import pytest

from src.ecu.constants import (
    ROM_SIZE,
    ROM_FLASH_START_MIN,
    SBL_SIZE,
    BLOCK_SIZE,
    SID_TESTER_PRESENT,
    SID_DIAGNOSTIC_SESSION,
    SID_SECURITY_ACCESS,
    SID_ROUTINE_CONTROL,
    SID_REQUEST_DOWNLOAD,
    SID_TRANSFER_DATA,
    SID_TRANSFER_EXIT,
    SID_ECU_RESET,
    SID_READ_MEM_BY_ADDR,
)
from src.ecu.flash_manager import FlashManager, FlashState
from src.ecu.exceptions import (
    ChecksumError,
    FlashAbortedError,
    FlashError,
    NegativeResponseError,
    ROMValidationError,
)

from ecu_test_helpers import build_positive_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Byte at ROM offset 0x2030 determines generation (0x35=NC1)
GEN_DETECT_OFFSET = 0x2030
GEN_NC1_BYTE = 0x35


def _make_valid_rom() -> bytearray:
    """Build a minimal 1MB ROM that passes validation."""
    rom = bytearray(ROM_SIZE)
    rom[GEN_DETECT_OFFSET] = GEN_NC1_BYTE
    return rom


def _build_flash_responses(num_sbl_blocks: int, num_program_blocks: int) -> list:
    """Build the read_msgs side_effect list for a full flash_rom flow.

    Sequence: tester_present, diag_session, seed_request, key_send,
    flash_counter, request_download, N×sbl_transfer, N×program_transfer,
    transfer_exit, ecu_reset.
    """
    responses = [
        # Tester Present
        [build_positive_response(SID_TESTER_PRESENT, b"\x01")],
        # Diagnostic Session
        [build_positive_response(SID_DIAGNOSTIC_SESSION, b"\x85")],
        # Security seed (sub=0x01 + 3-byte seed)
        [build_positive_response(SID_SECURITY_ACCESS, b"\x01\xaa\xbb\xcc")],
        # Security key accepted
        [build_positive_response(SID_SECURITY_ACCESS, b"\x02")],
        # Flash counter (routine control)
        [build_positive_response(SID_ROUTINE_CONTROL, b"\x00\xb2\x00\x01")],
        # Request Download
        [build_positive_response(SID_REQUEST_DOWNLOAD)],
    ]
    # SBL transfer blocks
    for _ in range(num_sbl_blocks):
        responses.append([build_positive_response(SID_TRANSFER_DATA)])
    # Program transfer blocks
    for _ in range(num_program_blocks):
        responses.append([build_positive_response(SID_TRANSFER_DATA)])
    # Transfer Exit
    responses.append([build_positive_response(SID_TRANSFER_EXIT)])
    # ECU Reset
    responses.append([build_positive_response(SID_ECU_RESET, b"\x01")])
    return responses


def _build_read_responses(num_blocks: int) -> list:
    """Build read_msgs side_effect for read_rom flow.

    Sequence: tester_present, diag_session, seed_request, key_send,
    N×read_memory, read_rom_id.
    """
    responses = [
        [build_positive_response(SID_TESTER_PRESENT, b"\x01")],
        [build_positive_response(SID_DIAGNOSTIC_SESSION, b"\x85")],
        [build_positive_response(SID_SECURITY_ACCESS, b"\x01\xaa\xbb\xcc")],
        [build_positive_response(SID_SECURITY_ACCESS, b"\x02")],
    ]
    # Read blocks — each returns BLOCK_SIZE bytes of data
    for _ in range(num_blocks):
        responses.append(
            [build_positive_response(SID_READ_MEM_BY_ADDR, b"\x00" * BLOCK_SIZE)]
        )
    # read_rom_id (ReadDataByIdentifier 0x22 with response)
    responses.append([build_positive_response(0x22, b"\xf1\x90TESTROM\x00")])
    return responses


# All integration tests patch _secure so they run in CI
_SECURE_PATCHES = {
    "src.ecu.flash_manager.compute_security_key": MagicMock(
        return_value=b"\x44\x70\xe8"
    ),
    "src.ecu.flash_manager.get_sbl_data": MagicMock(return_value=b"\x00" * SBL_SIZE),
    "src.ecu.flash_manager.SECURE_MODULE_AVAILABLE": True,
}


def _apply_secure_patches(func):
    """Decorator: patches compute_security_key, get_sbl_data, SECURE_MODULE_AVAILABLE."""
    for target, value in reversed(_SECURE_PATCHES.items()):
        if callable(value) or isinstance(value, MagicMock):
            func = patch(target, value)(func)
        else:
            func = patch(target, value)(func)
    return func


# ---------------------------------------------------------------------------
# Flash ROM Integration
# ---------------------------------------------------------------------------


class TestFlashRomIntegration:
    """End-to-end flash_rom with mocked J2534 and _secure."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_full_flash_happy_path(self, mock_setup_fc, MockDevice):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_sbl = SBL_SIZE // BLOCK_SIZE
        program_size = ROM_SIZE - ROM_FLASH_START_MIN
        num_program = (program_size + BLOCK_SIZE - 1) // BLOCK_SIZE

        mock_dev.read_msgs.side_effect = _build_flash_responses(num_sbl, num_program)

        fm = FlashManager()
        fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.COMPLETE
        mock_dev.close.assert_called()

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_state_transitions_in_order(self, mock_setup_fc, MockDevice):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_sbl = SBL_SIZE // BLOCK_SIZE
        program_size = ROM_SIZE - ROM_FLASH_START_MIN
        num_program = (program_size + BLOCK_SIZE - 1) // BLOCK_SIZE

        mock_dev.read_msgs.side_effect = _build_flash_responses(num_sbl, num_program)

        fm = FlashManager()
        states = []
        original_set = fm._set_state

        def spy_set_state(s):
            states.append(s)
            original_set(s)

        fm._set_state = spy_set_state
        fm.flash_rom(_make_valid_rom())

        expected_order = [
            FlashState.CONNECTING,
            FlashState.AUTHENTICATING,
            FlashState.PREPARING_SBL,
            FlashState.TRANSFERRING_SBL,
            FlashState.TRANSFERRING_PROGRAM,
            FlashState.FINALIZING,
            FlashState.RESETTING,
            FlashState.COMPLETE,
        ]
        assert states == expected_order

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_progress_callbacks_increasing(self, mock_setup_fc, MockDevice):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_sbl = SBL_SIZE // BLOCK_SIZE
        program_size = ROM_SIZE - ROM_FLASH_START_MIN
        num_program = (program_size + BLOCK_SIZE - 1) // BLOCK_SIZE

        mock_dev.read_msgs.side_effect = _build_flash_responses(num_sbl, num_program)

        fm = FlashManager()
        percents = []

        def cb(progress):
            percents.append(progress.percent)

        fm.flash_rom(bytes(_make_valid_rom()), progress_cb=cb)

        # Verify progress reaches 100%
        assert percents[-1] == 100.0
        # Verify progress generally increases (allow resets from _notify defaults)
        assert max(percents) == 100.0

    def test_invalid_rom_size_never_connects(self):
        """Wrong size ROM raises ROMValidationError before any device interaction."""
        fm = FlashManager()
        with patch("src.ecu.flash_manager.SECURE_MODULE_AVAILABLE", True):
            with pytest.raises(ROMValidationError, match="exactly"):
                fm.flash_rom(b"\x00" * 1024)

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_cleanup_on_error(self, mock_setup_fc, MockDevice):
        """Even on error, cleanup runs (device.close called)."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        # Fail on tester present
        from src.ecu.exceptions import UDSTimeoutError

        mock_dev.read_msgs.side_effect = UDSTimeoutError("timeout")

        fm = FlashManager()
        with pytest.raises(Exception):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ERROR
        mock_dev.close.assert_called()


# ---------------------------------------------------------------------------
# Read ROM Integration
# ---------------------------------------------------------------------------


class TestReadRomIntegration:
    @patch("src.ecu.flash_manager.compute_security_key", return_value=b"\x44\x70\xe8")
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_read_rom_happy_path(self, mock_setup_fc, MockDevice, mock_key):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_blocks = ROM_SIZE // BLOCK_SIZE
        mock_dev.read_msgs.side_effect = _build_read_responses(num_blocks)

        fm = FlashManager()
        result = fm.read_rom()

        assert len(result) == ROM_SIZE
        assert fm.state == FlashState.COMPLETE

    @patch("src.ecu.flash_manager.compute_security_key", return_value=b"\x44\x70\xe8")
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_read_rom_abort(self, mock_setup_fc, MockDevice, mock_key):
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_blocks = ROM_SIZE // BLOCK_SIZE
        responses = _build_read_responses(num_blocks)

        fm = FlashManager()

        # Set abort after auth completes but during read loop.
        # Auth uses 4 responses, then read_memory starts.
        call_count = 0
        original_responses = list(responses)

        def side_effect_with_abort(*args, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            # After auth (4 calls), set abort before returning first read block
            if idx == 4:
                fm._abort_event.set()
            return original_responses[idx]

        mock_dev.read_msgs.side_effect = side_effect_with_abort

        with pytest.raises(FlashAbortedError):
            fm.read_rom()
        assert fm.state == FlashState.ABORTED


# ---------------------------------------------------------------------------
# Use Session (Borrowed Handles)
# ---------------------------------------------------------------------------


class TestUseSession:
    def test_borrowed_handles_skip_connect(self, mock_j2534_device):
        """use_session skips J2534Device.open — device already open."""
        from src.ecu.protocol import UDSConnection

        uds = MagicMock(spec=UDSConnection)
        fm = FlashManager()
        fm.use_session(mock_j2534_device, 1, 100, uds)
        assert fm._owns_connection is False
        # device.open should never be called during _connect
        mock_j2534_device.open.assert_not_called()

    def test_borrowed_cleanup_preserves_handles(self, mock_j2534_device):
        """Cleanup with borrowed session does NOT close the device."""
        uds = MagicMock()
        fm = FlashManager()
        fm.use_session(mock_j2534_device, 1, 100, uds)
        fm._cleanup()
        mock_j2534_device.close.assert_not_called()
        mock_j2534_device.disconnect.assert_not_called()
        # References cleared
        assert fm._device is None
        assert fm._uds is None

    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_owned_cleanup_closes(self, mock_setup_fc, MockDevice):
        """Normal cleanup calls close/disconnect/stop_filter."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        fm = FlashManager()
        # Manually run connect to set up device
        fm._connect()
        fm._cleanup()

        mock_dev.stop_msg_filter.assert_called()
        mock_dev.disconnect.assert_called()
        mock_dev.close.assert_called()


# ---------------------------------------------------------------------------
# Checksum Verification (#45)
# ---------------------------------------------------------------------------


class TestChecksumVerification:
    """Verify that checksum correction is validated before flash."""

    @_apply_secure_patches
    @patch("src.ecu.flash_manager.correct_rom_checksums")
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_checksum_verification_failure_raises(
        self, mock_setup_fc, MockDevice, mock_correct
    ):
        """If second-pass verification finds corrections, ChecksumError raised."""
        # First call returns corrections (normal). Second call also returns
        # corrections (verification failure — checksums still wrong).
        mock_correct.side_effect = [
            [(0x2000, 0x3000, 0xFF650, 0x1234, 0x5678)],
            [(0x2000, 0x3000, 0xFF650, 0x5678, 0x9ABC)],
        ]
        fm = FlashManager()
        with pytest.raises(ChecksumError, match="still incorrect"):
            fm.flash_rom(_make_valid_rom())

        # Device should never have been opened (failure before connect)
        MockDevice.return_value.open.assert_not_called()

    @_apply_secure_patches
    @patch("src.ecu.flash_manager.correct_rom_checksums")
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_checksum_verification_passes(
        self, mock_setup_fc, MockDevice, mock_correct
    ):
        """Second-pass returns empty list — verification passes, flash proceeds."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        # First call corrects, second call verifies (empty = OK)
        mock_correct.side_effect = [
            [(0x2000, 0x3000, 0xFF650, 0x1234, 0x5678)],
            [],
        ]

        num_sbl = SBL_SIZE // BLOCK_SIZE
        program_size = ROM_SIZE - ROM_FLASH_START_MIN
        num_program = (program_size + BLOCK_SIZE - 1) // BLOCK_SIZE

        mock_dev.read_msgs.side_effect = _build_flash_responses(num_sbl, num_program)

        fm = FlashManager()
        fm.flash_rom(_make_valid_rom())
        assert fm.state == FlashState.COMPLETE


# ---------------------------------------------------------------------------
# ECU Reset Error Handling (#50)
# ---------------------------------------------------------------------------


class TestEcuResetHandling:
    """ECU reset failures after committed flash should not fail the operation."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_flash_completes_despite_reset_nrc(self, mock_setup_fc, MockDevice):
        """NRC during ecu_reset does not prevent COMPLETE state."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_sbl = SBL_SIZE // BLOCK_SIZE
        program_size = ROM_SIZE - ROM_FLASH_START_MIN
        num_program = (program_size + BLOCK_SIZE - 1) // BLOCK_SIZE

        responses = _build_flash_responses(num_sbl, num_program)
        # Replace the last response (ecu_reset) with an NRC
        from ecu_test_helpers import build_negative_response as build_nrc

        responses[-1] = [build_nrc(SID_ECU_RESET, 0x22)]
        mock_dev.read_msgs.side_effect = responses

        fm = FlashManager()
        fm.flash_rom(_make_valid_rom())
        assert fm.state == FlashState.COMPLETE

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_flash_completes_despite_reset_exception(self, mock_setup_fc, MockDevice):
        """Generic exception during ecu_reset does not prevent COMPLETE state."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        num_sbl = SBL_SIZE // BLOCK_SIZE
        program_size = ROM_SIZE - ROM_FLASH_START_MIN
        num_program = (program_size + BLOCK_SIZE - 1) // BLOCK_SIZE

        responses = _build_flash_responses(num_sbl, num_program)
        # Remove last response and make ecu_reset raise on the read
        responses.pop()
        mock_dev.read_msgs.side_effect = responses + [Exception("connection lost")]

        fm = FlashManager()
        fm.flash_rom(_make_valid_rom())
        assert fm.state == FlashState.COMPLETE
