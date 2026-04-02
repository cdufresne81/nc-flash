"""Tests for src/ecu/flash_manager.py — FlashManager state machine and guards."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.flash_manager import (
    FlashManager,
    FlashState,
    _TRANSITIONS,
    SECURE_MODULE_AVAILABLE,
)
from src.ecu.exceptions import (
    FlashError,
    SecureModuleNotAvailable,
    UDSTimeoutError,
)


class TestFlashState:
    """Test FlashState enum values."""

    def test_all_states_defined(self):
        """All expected states exist in the enum."""
        expected = {
            "idle",
            "connecting",
            "authenticating",
            "reading",
            "scanning_ram",
            "preparing_sbl",
            "transferring_sbl",
            "transferring_program",
            "finalizing",
            "resetting",
            "complete",
            "error",
            "aborted",
        }
        actual = {s.value for s in FlashState}
        assert expected == actual


class TestStateMachineTransitions:
    """Test the state transition table."""

    def test_idle_can_go_to_connecting(self):
        """IDLE -> CONNECTING is valid."""
        assert FlashState.CONNECTING in _TRANSITIONS[FlashState.IDLE]

    def test_connecting_can_go_to_authenticating(self):
        """CONNECTING -> AUTHENTICATING is valid."""
        assert FlashState.AUTHENTICATING in _TRANSITIONS[FlashState.CONNECTING]

    def test_authenticating_can_go_to_preparing_sbl(self):
        """AUTHENTICATING -> PREPARING_SBL (flash path) is valid."""
        assert FlashState.PREPARING_SBL in _TRANSITIONS[FlashState.AUTHENTICATING]

    def test_authenticating_can_go_to_reading(self):
        """AUTHENTICATING -> READING (read path) is valid."""
        assert FlashState.READING in _TRANSITIONS[FlashState.AUTHENTICATING]

    def test_invalid_transition_blocked(self, caplog):
        """Invalid state transition is blocked and logged."""
        fm = FlashManager()
        # IDLE -> COMPLETE is not in the transition table
        with caplog.at_level(logging.ERROR):
            fm._set_state(FlashState.COMPLETE)
        assert "Invalid state transition blocked" in caplog.text
        # State should remain IDLE — transition was refused
        assert fm.state == FlashState.IDLE

    def test_valid_transition_no_error(self, caplog):
        """Valid transition does not log an error."""
        fm = FlashManager()
        with caplog.at_level(logging.ERROR):
            fm._set_state(FlashState.CONNECTING)
        assert "Invalid state transition" not in caplog.text
        assert fm.state == FlashState.CONNECTING

    def test_error_reachable_from_most_states(self):
        """ERROR state is reachable from most active states."""
        error_sources = [
            FlashState.CONNECTING,
            FlashState.AUTHENTICATING,
            FlashState.READING,
            FlashState.PREPARING_SBL,
            FlashState.TRANSFERRING_SBL,
            FlashState.TRANSFERRING_PROGRAM,
            FlashState.FINALIZING,
            FlashState.RESETTING,
        ]
        for state in error_sources:
            assert FlashState.ERROR in _TRANSITIONS[state], f"{state} can't reach ERROR"


class TestFlashManagerGuards:
    """Test FlashManager pre-condition guards."""

    def test_flash_requires_secure_module(self):
        """flash_rom() raises SecureModuleNotAvailable in stub mode."""
        if SECURE_MODULE_AVAILABLE:
            pytest.skip("Secure module is available — can't test stub mode")
        fm = FlashManager()
        with pytest.raises(SecureModuleNotAvailable):
            fm.flash_rom(b"\x00" * 0x100000)

    def test_dynamic_flash_requires_secure_module(self, tmp_path):
        """dynamic_flash() raises SecureModuleNotAvailable in stub mode."""
        if SECURE_MODULE_AVAILABLE:
            pytest.skip("Secure module is available — can't test stub mode")
        fm = FlashManager()
        rom = b"\x00" * 0x100000
        archive = tmp_path / "archive.bin"
        archive.write_bytes(rom)
        with pytest.raises(SecureModuleNotAvailable):
            fm.dynamic_flash(rom, str(archive))


class TestIsBusy:
    """Test FlashManager.is_busy property."""

    def test_idle_not_busy(self):
        """IDLE state is not busy."""
        fm = FlashManager()
        assert fm.is_busy is False

    def test_connecting_is_busy(self):
        """Active states are busy."""
        fm = FlashManager()
        fm._state = FlashState.CONNECTING
        assert fm.is_busy is True

    def test_transferring_is_busy(self):
        """Transfer states are busy."""
        fm = FlashManager()
        fm._state = FlashState.TRANSFERRING_PROGRAM
        assert fm.is_busy is True

    def test_complete_not_busy(self):
        """COMPLETE state is not busy."""
        fm = FlashManager()
        fm._state = FlashState.COMPLETE
        assert fm.is_busy is False

    def test_error_not_busy(self):
        """ERROR state is not busy."""
        fm = FlashManager()
        fm._state = FlashState.ERROR
        assert fm.is_busy is False

    def test_aborted_not_busy(self):
        """ABORTED state is not busy."""
        fm = FlashManager()
        fm._state = FlashState.ABORTED
        assert fm.is_busy is False


class TestAbort:
    """Test abort request mechanism."""

    def test_abort_sets_flag(self):
        """abort() sets the internal flag when busy."""
        fm = FlashManager()
        fm._state = FlashState.TRANSFERRING_PROGRAM
        fm.abort()
        assert fm._check_abort() is True

    def test_abort_ignored_when_idle(self):
        """abort() does nothing when not busy."""
        fm = FlashManager()
        fm.abort()
        assert fm._check_abort() is False


# -----------------------------------------------------------------------
# flash_start_index bounds (#47)
# -----------------------------------------------------------------------


class TestFlashStartIndexBounds:
    """Defense-in-depth bounds check on flash_start_index."""

    @staticmethod
    def _valid_rom():
        """1MB ROM with valid generation byte."""
        rom = bytearray(0x100000)
        rom[0x2030] = 0x35  # NC1
        return bytes(rom)

    @patch("src.ecu.flash_manager.correct_rom_checksums", return_value=[])
    def test_zero_index_raises(self, _mock_cksum):
        """flash_start_index=0 rejected (below valid range)."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="out of bounds"):
            fm._flash_rom_inner(self._valid_rom(), 0, None)

    @patch("src.ecu.flash_manager.correct_rom_checksums", return_value=[])
    def test_index_at_rom_end_raises(self, _mock_cksum):
        """flash_start_index=ROM_SIZE rejected (no data to transfer)."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="out of bounds"):
            fm._flash_rom_inner(self._valid_rom(), 0x100000, None)

    @patch("src.ecu.flash_manager.correct_rom_checksums", return_value=[])
    def test_negative_index_raises(self, _mock_cksum):
        """Negative flash_start_index rejected."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="out of bounds"):
            fm._flash_rom_inner(self._valid_rom(), -1, None)


# -----------------------------------------------------------------------
# Borrowed session validation (#49)
# -----------------------------------------------------------------------


class TestBorrowedSessionValidation:
    """use_session validates handles; _connect verifies ECU is alive."""

    def test_use_session_none_device_raises(self):
        """use_session rejects None device."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="must not be None"):
            fm.use_session(None, 1, 100, MagicMock())

    def test_use_session_none_channel_raises(self):
        """use_session rejects None channel_id."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="must not be None"):
            fm.use_session(MagicMock(), None, 100, MagicMock())

    def test_use_session_none_uds_raises(self):
        """use_session rejects None uds."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="must not be None"):
            fm.use_session(MagicMock(), 1, 100, None)

    def test_borrowed_session_verified_alive(self, mock_j2534_device):
        """_connect with borrowed session calls tester_present."""
        uds = MagicMock()
        fm = FlashManager()
        fm.use_session(mock_j2534_device, 1, 100, uds)
        fm._connect()
        uds.tester_present.assert_called_once()
        assert fm.state == FlashState.CONNECTING

    def test_borrowed_session_dead_raises(self, mock_j2534_device):
        """Dead borrowed session raises FlashError from _connect."""
        uds = MagicMock()
        uds.tester_present.side_effect = UDSTimeoutError("no response")
        fm = FlashManager()
        fm.use_session(mock_j2534_device, 1, 100, uds)
        with pytest.raises(FlashError, match="not responsive"):
            fm._connect()
