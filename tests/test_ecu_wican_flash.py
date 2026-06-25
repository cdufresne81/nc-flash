"""Unit tests for the host-driven WiCAN flash orchestrator (goal-2 Part B).

The flash core (``FlashManager``) and link gate (``check_link_quality``) are
mocked: these tests pin the orchestration contract — the pre-flight gate, the
battery guard, and most importantly the **abort-and-restart-from-scratch**
behaviour (each retry is a FRESH whole flash, never a mid-stream block resend).
No hardware, no _secure, no socket.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.constants import (
    FLASH_COUNTER_OFFSET,
    FLASH_COUNTER_SIZE,
    ROM_FLASH_START_MIN,
    ROM_SIZE,
)
from src.ecu.exceptions import FlashError, ROMValidationError, TransferError
from src.ecu.link_quality import LinkQualityResult
from src.ecu.wican_flash import WiCANFlasher
from src.ecu.wican_transport import WiCANError


def _ok_link() -> LinkQualityResult:
    return LinkQualityResult(
        pings=25, replies=25, loss_pct=0.0, p95_ms=50.0, ok=True, reason="clean"
    )


def _bad_link() -> LinkQualityResult:
    return LinkQualityResult(
        pings=25,
        replies=20,
        loss_pct=20.0,
        p95_ms=50.0,
        ok=False,
        reason="packet loss 20%",
    )


@pytest.fixture
def mocks():
    """Patch FlashManager, the link gate, and UDSConnection inside wican_flash.

    Yields (FlashManager_mock, check_link_quality_mock, UDSConnection_mock).
    Defaults: link clean, battery 12.5 V.
    """
    with (
        patch("src.ecu.wican_flash.FlashManager") as FM,
        patch("src.ecu.wican_flash.check_link_quality") as clq,
        patch("src.ecu.wican_flash.UDSConnection") as UDS,
    ):
        UDS.return_value.read_battery_voltage.return_value = 12.5
        clq.return_value = _ok_link()
        yield FM, clq, UDS


# --- pre-flight gate --------------------------------------------------------


def test_bad_link_blocks_flash(mocks):
    FM, clq, _UDS = mocks
    clq.return_value = _bad_link()
    flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
    with pytest.raises(FlashError, match="link-quality gate"):
        flasher.flash_rom(b"\x00" * 16)
    FM.assert_not_called()  # never reached the flash


def test_low_battery_blocks_flash(mocks):
    FM, _clq, UDS = mocks
    UDS.return_value.read_battery_voltage.return_value = 11.4
    flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
    with pytest.raises(FlashError, match="Battery voltage"):
        flasher.flash_rom(b"\x00" * 16)
    FM.assert_not_called()


def test_missing_voltage_does_not_block(mocks):
    # PID unsupported (None) — warn, but proceed (matches the J2534 path).
    FM, _clq, UDS = mocks
    UDS.return_value.read_battery_voltage.return_value = None
    flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
    flasher.flash_rom(b"\x00" * 16)
    assert FM.return_value.flash_rom.call_count == 1


# --- happy path + restart-from-scratch --------------------------------------


def test_happy_path_single_attempt(mocks):
    FM, _clq, _UDS = mocks
    transport = MagicMock()
    flasher = WiCANFlasher(transport, restart_backoff_s=0)
    flasher.flash_rom(b"\x00" * 16, archive_path="arch.bin")
    assert FM.call_count == 1
    FM.return_value.use_uds.assert_called_once()
    FM.return_value.flash_rom.assert_called_once()
    transport.flush.assert_not_called()


def test_restart_from_scratch_on_drop(mocks):
    FM, _clq, _UDS = mocks
    FM.return_value.flash_rom.side_effect = [WiCANError("mid-flash drop"), None]
    transport = MagicMock()
    flasher = WiCANFlasher(transport, restart_backoff_s=0)
    flasher.flash_rom(b"\x00" * 16)
    # A fresh FlashManager is built per attempt — proof there is no shared
    # mid-flash state and therefore no mid-stream block resend.
    assert FM.call_count == 2
    assert FM.return_value.flash_rom.call_count == 2
    transport.flush.assert_called_once()  # stale frames drained before restart


def test_exhausts_attempts_then_raises(mocks):
    FM, _clq, _UDS = mocks
    FM.return_value.flash_rom.side_effect = TransferError("always drops")
    flasher = WiCANFlasher(MagicMock(), max_attempts=2, restart_backoff_s=0)
    with pytest.raises(FlashError, match="after 2 attempt"):
        flasher.flash_rom(b"\x00" * 16)
    assert FM.return_value.flash_rom.call_count == 2


def test_non_restartable_error_propagates_immediately(mocks):
    # A ROM validation error won't be fixed by retrying — it must NOT restart.
    FM, _clq, _UDS = mocks
    FM.return_value.flash_rom.side_effect = ROMValidationError("bad size")
    flasher = WiCANFlasher(MagicMock(), max_attempts=3, restart_backoff_s=0)
    with pytest.raises(ROMValidationError):
        flasher.flash_rom(b"\x00" * 16)
    assert FM.return_value.flash_rom.call_count == 1


def test_dynamic_flash_routes_to_dynamic(mocks):
    FM, _clq, _UDS = mocks
    flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
    flasher.dynamic_flash(b"\x00" * 16, "arch.bin")
    FM.return_value.dynamic_flash.assert_called_once()
    FM.return_value.flash_rom.assert_not_called()


# --- preflight-only + read-back verify --------------------------------------


def test_preflight_only_does_not_flash(mocks):
    FM, clq, _UDS = mocks
    flasher = WiCANFlasher(MagicMock())
    result = flasher.preflight()
    assert result.ok is True
    clq.assert_called_once()
    FM.assert_not_called()


def test_verify_pass(mocks):
    FM, _clq, _UDS = mocks
    rom = bytes(ROM_SIZE)
    FM.return_value.read_rom.return_value = bytearray(ROM_SIZE)
    with patch("src.ecu.wican_flash.correct_rom_checksums"):
        flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
        flasher.flash_rom(rom, verify=True)
    FM.return_value.read_rom.assert_called_once()


def test_verify_mismatch_raises(mocks):
    FM, _clq, _UDS = mocks
    rom = bytes(ROM_SIZE)
    written = bytearray(ROM_SIZE)
    written[ROM_FLASH_START_MIN + 100] = 0xFF  # diff inside the verified region
    FM.return_value.read_rom.return_value = written
    with patch("src.ecu.wican_flash.correct_rom_checksums"):
        flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
        with pytest.raises(FlashError, match="verify FAILED"):
            flasher.flash_rom(rom, verify=True)


def test_verify_tolerates_flash_counter(mocks):
    # The ECU stamps its own flash-cycle counter (0xFFB00..+8) during programming,
    # so a read-back that differs ONLY there must still PASS — those bytes are not
    # part of the written image (hardware-confirmed 2026-06-23).
    FM, _clq, _UDS = mocks
    rom = bytes(ROM_SIZE)
    written = bytearray(ROM_SIZE)
    for i in range(FLASH_COUNTER_OFFSET, FLASH_COUNTER_OFFSET + FLASH_COUNTER_SIZE):
        written[i] = 0xAB  # ECU-stamped counter, differs from the source
    FM.return_value.read_rom.return_value = written
    with patch("src.ecu.wican_flash.correct_rom_checksums"):
        flasher = WiCANFlasher(MagicMock(), restart_backoff_s=0)
        flasher.flash_rom(rom, verify=True)  # must NOT raise
    FM.return_value.read_rom.assert_called_once()
