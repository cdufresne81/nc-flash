"""The "which car is this ECU from?" question, asked mid-flash.

The flash runs on a worker thread; the question needs the GUI thread. The real
wiring (``_wire_generation_prompt``) connects the worker's signal BLOCKING and
hands the worker's ask method to the flash driver. These run that wiring across
real threads: a wrong connection type would return before the answer (or
deadlock), and a missing hand-off would make every unreadable ECU fail.
"""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QMessageBox

from src.ui.ecu_window import ECUProgrammingWindow, _FlashWorker


class _Window(QObject):
    """GUI-thread stand-in for the window: the real wiring and slot, with the
    dialog replaced by a fixed answer."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.asked = []
        self._flash_worker = None

    _wire_generation_prompt = ECUProgrammingWindow._wire_generation_prompt

    @Slot(str, str, str)
    def _on_generation_unverified(self, rom_generation, rom_cal_id, reason):
        ECUProgrammingWindow._on_generation_unverified(
            self, rom_generation, rom_cal_id, reason
        )

    def _ask_car_generation(self, rom_generation, rom_cal_id, reason):
        self.asked.append((rom_generation, rom_cal_id, reason))
        return self.answer


class _Driver(QObject):
    """Flash driver stand-in: calls the prompt it was given, from its thread."""

    done = Signal(object)

    def __init__(self):
        super().__init__()
        self.prompt = None

    def set_unverified_generation_prompt(self, prompt):
        self.prompt = prompt

    @Slot()
    def run(self):
        self.done.emit(self.prompt("NC2", "LF9VEB", "the ECU refused"))


@pytest.mark.parametrize("answer", ["NC1", "NC2", None])
def test_answer_crosses_threads_through_the_real_wiring(qtbot, answer):
    window = _Window(answer)
    driver = _Driver()
    worker = _FlashWorker(driver, "flash")
    window._flash_worker = worker
    window._wire_generation_prompt(worker, driver, "flash")
    assert driver.prompt == worker.ask_unverified_generation

    thread = QThread()
    worker.moveToThread(thread)
    driver.moveToThread(thread)
    results = []
    driver.done.connect(results.append, Qt.DirectConnection)
    thread.started.connect(driver.run)
    thread.start()
    try:
        qtbot.waitUntil(lambda: bool(results), timeout=5000)
    finally:
        thread.quit()
        thread.wait(3000)

    assert results == [answer]
    assert window.asked == [("NC2", "LF9VEB", "the ECU refused")]


def test_reads_get_no_prompt():
    window = _Window("NC2")
    driver = MagicMock()
    window._wire_generation_prompt(_FlashWorker(driver, "read"), driver, "read")
    driver.set_unverified_generation_prompt.assert_not_called()


@pytest.mark.parametrize(
    "label,expected", [("NC1", "NC1"), ("NC2", "NC2"), ("Cancel", None)]
)
def test_dialog_maps_buttons_and_defaults_to_cancel(qtbot, label, expected):
    seen = {}

    def fake_exec(box):
        seen["default"] = box.defaultButton().text()
        seen["text"] = box.text()
        for button in box.buttons():
            if button.text().startswith(label):
                button.click()
                return 0
        raise AssertionError(f"no button {label}")

    with patch.object(QMessageBox, "exec", fake_exec):
        got = ECUProgrammingWindow._ask_car_generation(
            None, "NC2", "LF9VEB", "the ECU refused"
        )
    assert got == expected
    # Cancel is the default, so an accidental Enter flashes nothing.
    assert seen["default"] == "Cancel"
    assert "LF9VEB" in seen["text"]
    # The ROM's generation is not shown: the answer must come from the car.
    assert "(NC2)" not in seen["text"]
