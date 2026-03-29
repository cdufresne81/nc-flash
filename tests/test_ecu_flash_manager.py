"""Tests for src/ecu/flash_manager.py — FlashManager state machine and guards."""

import logging
import pytest
from src.ecu.flash_manager import (
    FlashManager,
    FlashState,
    _TRANSITIONS,
    SECURE_MODULE_AVAILABLE,
)
from src.ecu.exceptions import SecureModuleNotAvailable


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
