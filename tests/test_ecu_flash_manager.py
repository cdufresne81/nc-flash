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

    @patch("src.ecu.flash_prep.correct_rom_checksums", return_value=[])
    def test_zero_index_raises(self, _mock_cksum):
        """flash_start_index=0 rejected (below valid range)."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="out of bounds"):
            fm._flash_rom_inner(self._valid_rom(), 0, None)

    @patch("src.ecu.flash_prep.correct_rom_checksums", return_value=[])
    def test_index_at_rom_end_raises(self, _mock_cksum):
        """flash_start_index=ROM_SIZE rejected (no data to transfer)."""
        fm = FlashManager()
        with pytest.raises(FlashError, match="out of bounds"):
            fm._flash_rom_inner(self._valid_rom(), 0x100000, None)

    @patch("src.ecu.flash_prep.correct_rom_checksums", return_value=[])
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


class TestReadBlockRetry:
    """Per-block read retry over a lossy link (idempotent reads only)."""

    def test_first_attempt_success_is_passthrough(self):
        """A clean read returns immediately with no retry and no flush."""
        uds = MagicMock()
        uds.read_memory_by_address.return_value = b"\xaa" * 0x400
        fm = FlashManager()
        fm.use_uds(uds)
        data = fm._read_block_with_retry(0x1000, 0x400)
        assert data == b"\xaa" * 0x400
        assert uds.read_memory_by_address.call_count == 1
        uds.flush.assert_not_called()

    def test_recovers_after_timeouts_and_flushes_between(self):
        """A block that times out twice then succeeds is recovered; the
        transport is flushed before each retry so stale frames can't corrupt
        the re-requested block."""
        uds = MagicMock()
        good = b"\xbb" * 0x400
        uds.read_memory_by_address.side_effect = [
            UDSTimeoutError("drop 1"),
            UDSTimeoutError("drop 2"),
            good,
        ]
        fm = FlashManager()
        fm.use_uds(uds)
        with patch("src.ecu.flash_manager.time.sleep") as sleep:
            data = fm._read_block_with_retry(0x2000, 0x400)
        assert data == good
        assert uds.read_memory_by_address.call_count == 3
        # Flushed once per failed attempt (twice), not after the success.
        assert uds.flush.call_count == 2
        # Backed off once per failed-then-retried attempt (twice), not after success.
        assert sleep.call_count == 2

    def test_backoff_grows_between_retries_and_not_after_last(self):
        """The retry backoff grows with the attempt (so a transient stall clears)
        and is never applied after the final, give-up attempt."""
        from src.ecu.flash_manager import (
            READ_BLOCK_RETRIES,
            READ_BLOCK_RETRY_BACKOFF_S,
            READ_BLOCK_RETRY_BACKOFF_MAX_S,
        )

        uds = MagicMock()
        uds.read_memory_by_address.side_effect = UDSTimeoutError("always drops")
        fm = FlashManager()
        fm.use_uds(uds)
        with patch("src.ecu.flash_manager.time.sleep") as sleep:
            with pytest.raises(FlashError):
                fm._read_block_with_retry(0x5000, 0x400)
        # One sleep per gap between attempts — never after the last attempt.
        waited = [c.args[0] for c in sleep.call_args_list]
        assert len(waited) == READ_BLOCK_RETRIES - 1
        expected = [
            min(READ_BLOCK_RETRY_BACKOFF_S * n, READ_BLOCK_RETRY_BACKOFF_MAX_S)
            for n in range(1, READ_BLOCK_RETRIES)
        ]
        assert waited == expected

    def test_uses_tight_per_block_budget(self):
        """Reads pass the small per-block timeout/budget so a drop fails fast
        instead of stalling the full 60 s response-pending budget."""
        from src.ecu.flash_manager import (
            READ_BLOCK_TIMEOUT_MS,
            READ_BLOCK_PENDING_MAX_MS,
        )

        uds = MagicMock()
        uds.read_memory_by_address.return_value = b"\x00" * 0x400
        fm = FlashManager()
        fm.use_uds(uds)
        fm._read_block_with_retry(0, 0x400)
        _args, kwargs = uds.read_memory_by_address.call_args
        assert kwargs["timeout"] == READ_BLOCK_TIMEOUT_MS
        assert kwargs["pending_max"] == READ_BLOCK_PENDING_MAX_MS

    def test_exhausting_retries_raises_flasherror(self):
        """When every attempt fails, the read aborts with FlashError after the
        configured number of attempts."""
        from src.ecu.flash_manager import READ_BLOCK_RETRIES

        uds = MagicMock()
        uds.read_memory_by_address.side_effect = UDSTimeoutError("always drops")
        fm = FlashManager()
        fm.use_uds(uds)
        with patch("src.ecu.flash_manager.time.sleep"):
            with pytest.raises(FlashError, match="after .* attempts"):
                fm._read_block_with_retry(0x3000, 0x400)
        assert uds.read_memory_by_address.call_count == READ_BLOCK_RETRIES

    def test_short_block_raises_flasherror(self):
        """A block that returns fewer bytes than requested is a hard error."""
        uds = MagicMock()
        uds.read_memory_by_address.return_value = b"\x00" * 0x200  # half a block
        fm = FlashManager()
        fm.use_uds(uds)
        with pytest.raises(FlashError, match="Short read"):
            fm._read_block_with_retry(0x4000, 0x400)


class TestReadRomBlockSize:
    """`read_rom` honours a configurable per-request read size (clamped)."""

    @staticmethod
    def _make_fm():
        """A FlashManager whose connect/auth are stubbed and whose UDS returns
        exactly the requested number of bytes for every block read."""
        uds = MagicMock()
        uds.read_memory_by_address.side_effect = (
            lambda offset, size, **kw: b"\x00" * size
        )
        fm = FlashManager()
        fm.use_uds(uds)
        fm._connect = lambda *a, **k: None
        fm._authenticate = lambda *a, **k: None
        return fm, uds

    def test_default_block_size_is_0x400(self):
        from src.ecu.constants import ROM_SIZE, BLOCK_SIZE

        fm, uds = self._make_fm()
        fm.read_rom()
        first = uds.read_memory_by_address.call_args_list[0]
        assert first.args[1] == BLOCK_SIZE
        assert uds.read_memory_by_address.call_count == ROM_SIZE // BLOCK_SIZE

    def test_custom_block_size_is_used(self):
        from src.ecu.constants import ROM_SIZE

        fm, uds = self._make_fm()
        fm.read_rom(read_block_size=0x800)
        first = uds.read_memory_by_address.call_args_list[0]
        assert first.args[1] == 0x800
        assert uds.read_memory_by_address.call_count == ROM_SIZE // 0x800

    def test_oversize_block_is_clamped_to_isotp_max(self):
        from src.ecu.flash_manager import MAX_ISOTP_READ_SIZE

        fm, uds = self._make_fm()
        fm.read_rom(read_block_size=0x99999)  # absurd; must clamp
        first = uds.read_memory_by_address.call_args_list[0]
        assert first.args[1] == MAX_ISOTP_READ_SIZE

    def test_zero_elapsed_read_does_not_divide_by_zero(self):
        """On a fast host the whole mocked read can finish within a single
        ``time.monotonic`` tick (``read_elapsed == 0``). The completion-speed
        log line must not raise ZeroDivisionError. Pinning ``monotonic`` to a
        constant makes that race deterministic (regression for the fast-CI-runner
        ``ZeroDivisionError`` at flash_manager.py:878)."""
        fm, uds = self._make_fm()
        captured = []
        with patch("src.ecu.flash_manager.time.monotonic", return_value=123.0):
            # progress_cb exercises the per-block speed line too (also guarded).
            fm.read_rom(progress_cb=lambda p: captured.append(p))
        assert captured  # the read actually ran and emitted progress


class TestEnforceRpmGate:
    """enforce_rpm_gate() — the engine-off flash gate, enforced in code."""

    @staticmethod
    def _uds(rpm):
        """A minimal uds whose read_engine_rpm returns ``rpm``."""
        uds = MagicMock()
        uds.read_engine_rpm.return_value = rpm
        return uds

    def test_engine_off_passes(self):
        from src.ecu.flash_manager import enforce_rpm_gate

        assert enforce_rpm_gate(self._uds(0.0)) == 0.0

    def test_just_below_threshold_passes(self):
        from src.ecu.flash_manager import enforce_rpm_gate

        # default threshold is 1.0 RPM; 0.9 is "off" (sensor noise floor)
        assert enforce_rpm_gate(self._uds(0.9)) == 0.9

    def test_engine_running_blocks(self):
        from src.ecu.flash_manager import enforce_rpm_gate
        from src.ecu.exceptions import EngineRunningError

        with pytest.raises(EngineRunningError) as exc:
            enforce_rpm_gate(self._uds(820.0))
        assert exc.value.rpm == 820.0
        # EngineRunningError is a FlashError so existing flash handlers catch it
        assert isinstance(exc.value, FlashError)

    def test_threshold_is_inclusive(self):
        """rpm == threshold blocks (>=), so the gate can't be squeaked past."""
        from src.ecu.flash_manager import enforce_rpm_gate
        from src.ecu.exceptions import EngineRunningError

        with pytest.raises(EngineRunningError):
            enforce_rpm_gate(self._uds(1.0))

    def test_override_allows_running_engine(self):
        from src.ecu.flash_manager import enforce_rpm_gate

        assert enforce_rpm_gate(self._uds(820.0), allow_override=True) == 820.0

    def test_unreadable_rpm_does_not_block(self):
        """A None read (PID unsupported) must NOT block — can't prove running."""
        from src.ecu.flash_manager import enforce_rpm_gate

        assert enforce_rpm_gate(self._uds(None)) is None

    def test_read_exception_does_not_block(self):
        from src.ecu.flash_manager import enforce_rpm_gate

        uds = MagicMock()
        uds.read_engine_rpm.side_effect = RuntimeError("boom")
        assert enforce_rpm_gate(uds) is None

    def test_no_uds_does_not_block(self):
        from src.ecu.flash_manager import enforce_rpm_gate

        assert enforce_rpm_gate(None) is None

    def test_reads_rpm_before_any_session_entry(self):
        """The gate's ONLY ECU contact is the OBD RPM read — it must not enter a
        diagnostic/programming session (which would make RPM unreadable). Proven
        by asserting no session-entry calls are made on the uds."""
        from src.ecu.flash_manager import enforce_rpm_gate

        uds = self._uds(0.0)
        enforce_rpm_gate(uds)
        uds.read_engine_rpm.assert_called_once()
        uds.diagnostic_session.assert_not_called()
        uds.security_access.assert_not_called()
