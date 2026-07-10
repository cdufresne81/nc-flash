"""Unit tests for the WiCAN pre-flight flash gate (link-quality + battery).

Option A — the host-driven, block-by-block WiFi flash — was RETIRED (audit D4);
what remains in ``wican_flash`` is the reusable safety gate the SD flasher
composes. These tests pin the gate contract: a bad link or a low battery refuses;
a missing voltage does NOT refuse (fail-open, like the J2534 path); ``preflight``
reports link status without reading the battery. No hardware, no _secure, no socket.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.exceptions import FlashError
from src.ecu.link_quality import LinkQualityResult
from src.ecu.wican_flash import WiCANFlasher


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
    """Patch the link gate + UDSConnection inside wican_flash.

    Yields (check_link_quality_mock, UDSConnection_mock). Defaults: link clean,
    battery 12.5 V.
    """
    with (
        patch("src.ecu.wican_flash.check_link_quality") as clq,
        patch("src.ecu.wican_flash.UDSConnection") as UDS,
    ):
        UDS.return_value.read_battery_voltage.return_value = 12.5
        clq.return_value = _ok_link()
        yield clq, UDS


# --- pre-flight gate --------------------------------------------------------


def test_gate_passes_on_clean_link_and_good_battery(mocks):
    WiCANFlasher(MagicMock())._gate()  # must not raise


def test_bad_link_blocks_gate(mocks):
    clq, _UDS = mocks
    clq.return_value = _bad_link()
    with pytest.raises(FlashError, match="link-quality gate FAILED"):
        WiCANFlasher(MagicMock())._gate()


def test_low_battery_blocks_gate(mocks):
    _clq, UDS = mocks
    UDS.return_value.read_battery_voltage.return_value = 11.0
    with pytest.raises(FlashError, match="Battery voltage"):
        WiCANFlasher(MagicMock())._gate()


def test_missing_voltage_does_not_block_gate(mocks):
    _clq, UDS = mocks
    UDS.return_value.read_battery_voltage.return_value = None
    WiCANFlasher(MagicMock())._gate()  # fail-open: must not raise


# --- preflight (link status only, no battery read) --------------------------


def test_preflight_returns_result_without_battery_read(mocks):
    clq, UDS = mocks
    result = WiCANFlasher(MagicMock()).preflight()
    assert result.ok is True
    clq.assert_called_once()
    UDS.return_value.read_battery_voltage.assert_not_called()


def test_preflight_reports_bad_link(mocks):
    clq, _UDS = mocks
    clq.return_value = _bad_link()
    result = WiCANFlasher(MagicMock()).preflight()
    assert result.ok is False
    assert "packet loss" in result.reason
