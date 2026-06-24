"""WiCAN write routing at the UI seam (``_build_flash_driver``).

Option B Phase 6 enabled WiCAN flashing via the SD-staged, firmware-driven path
(``WiCANSdFlasher``), which removes WiFi from the flash loop and was proven
byte-perfect on the live ECU. The single choke point every flash/read routes
through is ``ECUProgrammingWindow._build_flash_driver``; this asserts WiCAN
``flash``/``dynamic_flash`` route to the SD flasher, that the ``WICAN_WRITE_ENABLED``
gate still works as a kill-switch, and that WiCAN reads / J2534 flashing are
untouched.

The driver-selection logic is exercised against a duck-typed fake ``self`` so the
test needs no QApplication / real adapter — only the decision branch matters.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.ui.ecu_window import ECUProgrammingWindow


class _FakeWindow:
    """Minimal stand-in exposing only what ``_build_flash_driver`` touches."""

    def __init__(self, *, wican: bool, connected: bool = True):
        self._wican = wican
        self._session_acquired = False
        if connected:
            self._session = MagicMock()
            self._session.is_connected = True
            self._session.acquire.return_value = (None, None, None, MagicMock())
            self._session.transport = MagicMock()
        else:
            self._session = None

    def _is_wican(self) -> bool:
        return self._wican

    def _get_dll_path(self) -> str:
        return "fake.dll"


def _build(fake, operation, source_name=None):
    return ECUProgrammingWindow._build_flash_driver(fake, operation, source_name)


class TestWiCANWriteRouting:
    @pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
    def test_wican_write_routes_to_sd_flasher_by_default(self, operation):
        # Phase 6: the gate is ON by default — WiCAN writes route to the SD flasher.
        fake = _FakeWindow(wican=True)
        with patch("src.ecu.wican_sd_flash.WiCANSdFlasher") as MockFlasher:
            driver = _build(fake, operation)
        assert driver is MockFlasher.return_value
        MockFlasher.assert_called_once_with(fake._session.transport, source_name=None)

    @pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
    def test_source_name_forwarded_to_sd_flasher(self, operation):
        # The ROM's display filename reaches the SD flasher so the staged SD image
        # is named after it (sanitised downstream by build_flash_package).
        fake = _FakeWindow(wican=True)
        with patch("src.ecu.wican_sd_flash.WiCANSdFlasher") as MockFlasher:
            _build(fake, operation, source_name="My Tune éà.bin")
        MockFlasher.assert_called_once_with(
            fake._session.transport, source_name="My Tune éà.bin"
        )

    @pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
    def test_wican_write_kill_switch_disables(self, operation):
        # The gate still works as a kill-switch: flipping it OFF refuses the write
        # at the seam before any session is acquired (no ECU contact attempted).
        fake = _FakeWindow(wican=True)
        with patch("src.ecu.wican_flash.WICAN_WRITE_ENABLED", False):
            assert _build(fake, operation) is None
        fake._session.acquire.assert_not_called()

    @pytest.mark.parametrize("operation", ["read", "scan_ram"])
    def test_wican_read_still_builds_a_manager(self, operation):
        fake = _FakeWindow(wican=True)
        with patch("src.ui.ecu_window.FlashManager") as MockFM:
            driver = _build(fake, operation)
        assert driver is MockFM.return_value
        MockFM.return_value.use_uds.assert_called_once()

    @pytest.mark.parametrize("operation", ["flash", "dynamic_flash"])
    def test_j2534_flash_is_never_blocked(self, operation):
        # Non-WiCAN adapter: the gate must not touch the J2534 path.
        fake = _FakeWindow(wican=False)
        with patch("src.ui.ecu_window.FlashManager") as MockFM:
            driver = _build(fake, operation)
        assert driver is MockFM.return_value


class TestWorkerFinishedHandlers:
    """The worker ``finished``/``error`` slots are plain bound methods that
    delegate to ``_on_flash_finished`` using the stored thread+worker handles.

    They exist precisely so the queued connection has a **GUI-thread QObject
    receiver** (this window). The previous bare-lambda connections had no receiver
    QObject, so Qt ran ``_on_flash_finished`` on the *worker* thread and its GUI
    mutations crashed with "Cannot set parent ... different thread" / a cross-thread
    paint. These guard the delegation contract (run unbound against a fake self —
    no QApplication needed).
    """

    def test_finished_delegates_success_with_stored_handles(self):
        fake = SimpleNamespace(
            _flash_thread="THREAD",
            _flash_worker="WORKER",
            _on_flash_finished=MagicMock(),
        )
        ECUProgrammingWindow._on_worker_finished(fake)
        fake._on_flash_finished.assert_called_once_with(True, "THREAD", "WORKER")

    def test_error_delegates_failure_with_message(self):
        fake = SimpleNamespace(
            _flash_thread="THREAD",
            _flash_worker="WORKER",
            _on_flash_finished=MagicMock(),
        )
        ECUProgrammingWindow._on_worker_error(fake, "boom")
        fake._on_flash_finished.assert_called_once_with(
            False, "THREAD", "WORKER", "boom"
        )
