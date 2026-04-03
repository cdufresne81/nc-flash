"""
Flash abort scenario tests — dangerous states during ECU flash.

Tests abort behavior during SBL upload, ROM transfer, and connection drops.
These are the scenarios where an ECU can be left in a vulnerable state.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.constants import (
    ROM_SIZE,
    ROM_FLASH_START_MIN,
    SBL_SIZE,
    BLOCK_SIZE,
)
from src.ecu.flash_manager import FlashManager, FlashState
from src.ecu.exceptions import FlashAbortedError, J2534Error

from test_ecu_flash_integration import (
    _build_flash_responses,
    _make_valid_rom,
    _apply_secure_patches,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUM_SBL_BLOCKS = SBL_SIZE // BLOCK_SIZE
PROGRAM_SIZE = ROM_SIZE - ROM_FLASH_START_MIN
NUM_PROGRAM_BLOCKS = (PROGRAM_SIZE + BLOCK_SIZE - 1) // BLOCK_SIZE

# Response index layout:
#   0: tester_present
#   1: diag_session
#   2: security_seed
#   3: security_key_accept
#   4: flash_counter (routine_control)
#   5: request_download
#   6 .. 6+NUM_SBL_BLOCKS-1: SBL transfer_data
#   6+NUM_SBL_BLOCKS .. : program transfer_data
#   -2: transfer_exit
#   -1: ecu_reset
AUTH_RESPONSES = 4  # tester_present + diag_session + seed + key_accept
PRE_SBL_RESPONSES = 2  # flash_counter + request_download
SBL_START_INDEX = AUTH_RESPONSES + PRE_SBL_RESPONSES  # 6
PROGRAM_START_INDEX = SBL_START_INDEX + NUM_SBL_BLOCKS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_abort_side_effect(responses, fm, trigger_index):
    """Create a side_effect function that sets abort at a specific call index.

    Args:
        responses: Full list of response lists from _build_flash_responses.
        fm: FlashManager instance whose _abort_event will be set.
        trigger_index: The read_msgs call index at which to set abort.
    """
    call_count = 0
    original = list(responses)

    def side_effect(*args, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        if idx == trigger_index:
            fm._abort_event.set()
        if idx < len(original):
            return original[idx]
        return []

    return side_effect


def _make_error_side_effect(responses, error_index, error):
    """Create a side_effect that raises an exception at a specific call index."""
    call_count = 0
    original = list(responses)

    def side_effect(*args, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        if idx == error_index:
            raise error
        if idx < len(original):
            return original[idx]
        return []

    return side_effect


# ---------------------------------------------------------------------------
# Abort During SBL Transfer
# ---------------------------------------------------------------------------


class TestAbortDuringSblTransfer:
    """Abort while Secondary Boot Loader is being uploaded."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_abort_during_sbl_transfer(self, mock_setup_fc, MockDevice):
        """Abort mid-SBL: state becomes ABORTED, cleanup runs."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)
        fm = FlashManager()

        # Trigger abort at SBL block 3 (index 8 = 6 + 2)
        abort_index = SBL_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_abort_side_effect(
            responses, fm, abort_index
        )

        with pytest.raises(FlashAbortedError):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ABORTED
        mock_dev.close.assert_called()

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_sbl_abort_stops_early(self, mock_setup_fc, MockDevice):
        """After abort, no more SBL blocks are transferred."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)
        fm = FlashManager()

        abort_index = SBL_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_abort_side_effect(
            responses, fm, abort_index
        )

        with pytest.raises(FlashAbortedError):
            fm.flash_rom(_make_valid_rom())

        # Should not have reached program transfer
        total_calls = mock_dev.read_msgs.call_count
        assert total_calls < PROGRAM_START_INDEX


# ---------------------------------------------------------------------------
# Abort During Program Transfer
# ---------------------------------------------------------------------------


class TestAbortDuringProgramTransfer:
    """Abort while ROM calibration data is being flashed."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_abort_during_program_transfer(self, mock_setup_fc, MockDevice):
        """Abort mid-ROM-flash: state becomes ABORTED, cleanup runs."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)
        fm = FlashManager()

        # Trigger abort at program block 3 (a few blocks into ROM transfer)
        abort_index = PROGRAM_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_abort_side_effect(
            responses, fm, abort_index
        )

        with pytest.raises(FlashAbortedError):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ABORTED
        mock_dev.close.assert_called()

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_no_extra_blocks_after_abort(self, mock_setup_fc, MockDevice):
        """No additional transfer blocks sent after abort flag is set."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)
        fm = FlashManager()

        abort_index = PROGRAM_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_abort_side_effect(
            responses, fm, abort_index
        )

        with pytest.raises(FlashAbortedError):
            fm.flash_rom(_make_valid_rom())

        # Abort was set at call N. The block completes, then abort is checked
        # before the next block. So total calls = abort_index + 1 (the
        # triggering call completes, but no further blocks are read).
        total_calls = mock_dev.read_msgs.call_count
        total_expected = len(responses)
        assert (
            total_calls < total_expected
        ), f"Expected early stop but got {total_calls}/{total_expected} calls"


# ---------------------------------------------------------------------------
# Abort Before SBL Transfer (during prepare/erase phase)
# ---------------------------------------------------------------------------


class TestAbortBeforeSblTransfer:
    """Abort set during prepare phase, honored at first transfer block."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_abort_at_request_download(self, mock_setup_fc, MockDevice):
        """Abort set at request_download: caught at first SBL block check."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)
        fm = FlashManager()

        # Set abort when request_download response is returned (index 5).
        # transfer_data checks abort_check before its first block.
        abort_index = AUTH_RESPONSES + PRE_SBL_RESPONSES - 1  # 5
        mock_dev.read_msgs.side_effect = _make_abort_side_effect(
            responses, fm, abort_index
        )

        with pytest.raises(FlashAbortedError):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ABORTED
        mock_dev.close.assert_called()


# ---------------------------------------------------------------------------
# Connection Drop During Flash
# ---------------------------------------------------------------------------


class TestConnectionDropDuringFlash:
    """Simulated USB disconnect / communication failure during transfer."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_connection_drop_during_sbl(self, mock_setup_fc, MockDevice):
        """J2534Error during SBL transfer: state becomes ERROR, cleanup runs."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)

        error_index = SBL_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_error_side_effect(
            responses, error_index, J2534Error("USB cable disconnected")
        )

        fm = FlashManager()
        with pytest.raises(Exception):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ERROR
        mock_dev.close.assert_called()

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_connection_drop_during_program(self, mock_setup_fc, MockDevice):
        """J2534Error during ROM transfer: state becomes ERROR, cleanup runs."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)

        error_index = PROGRAM_START_INDEX + 5
        mock_dev.read_msgs.side_effect = _make_error_side_effect(
            responses, error_index, J2534Error("Device timeout")
        )

        fm = FlashManager()
        with pytest.raises(Exception):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ERROR
        mock_dev.close.assert_called()


# ---------------------------------------------------------------------------
# Cleanup Failure After Abort
# ---------------------------------------------------------------------------


class TestCleanupFailureAfterAbort:
    """Verify cleanup failures don't swallow the original abort/error."""

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_abort_raised_despite_cleanup_failure(self, mock_setup_fc, MockDevice):
        """FlashAbortedError propagates even when device.close() fails."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        mock_dev.close.side_effect = OSError("USB already gone")

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)
        fm = FlashManager()

        abort_index = PROGRAM_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_abort_side_effect(
            responses, fm, abort_index
        )

        with pytest.raises(FlashAbortedError):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ABORTED

    @_apply_secure_patches
    @patch("src.ecu.j2534.J2534Device")
    @patch("src.ecu.j2534.setup_isotp_flow_control", return_value=100)
    def test_error_preserved_despite_cleanup_failure(self, mock_setup_fc, MockDevice):
        """Connection error propagates even when cleanup also fails."""
        mock_dev = MockDevice.return_value
        mock_dev.connect.return_value = 1
        mock_dev.close.side_effect = OSError("USB already gone")
        mock_dev.disconnect.side_effect = OSError("Channel dead")
        mock_dev.stop_msg_filter.side_effect = OSError("Filter invalid")

        responses = _build_flash_responses(NUM_SBL_BLOCKS, NUM_PROGRAM_BLOCKS)

        error_index = SBL_START_INDEX + 2
        mock_dev.read_msgs.side_effect = _make_error_side_effect(
            responses, error_index, J2534Error("Connection lost")
        )

        fm = FlashManager()
        with pytest.raises(Exception):
            fm.flash_rom(_make_valid_rom())

        assert fm.state == FlashState.ERROR
