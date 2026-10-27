"""
Switch a Tactrix OpenPort 2.0 between NC Flash's driver (WinUSB) and Tactrix's.

One instance for the whole app, owned by the main window and handed to the ECU
window and the settings dialog. It explains the change, asks for confirmation,
then runs openport_winusb.ps1 elevated (one Windows admin prompt) in a QProcess
so the windows stay responsive. It never runs while the ECU window holds an
ECU link: switching the driver unplugs the cable from Windows' point of view,
which would cut a flash short.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Optional

from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from src.ecu.openport_setup import (
    EXIT_BUSY,
    EXIT_DECLINED,
    EXIT_LAUNCH_FAILED,
    OpenPortDriver,
    OpenPortState,
    builtin_driver_available,
    detect_openport,
    elevated_setup_command,
    find_setup_script,
    new_log_name,
    read_setup_result,
    setup_log_dir,
)

logger = logging.getLogger(__name__)

INSTALL, RESTORE = "Install", "Restore"

_TRADE_OFF = (
    "Close any other program using the cable first (EcuFlash, RomDrop, a second "
    "NC Flash): switching drivers unplugs it from Windows' point of view.\n\n"
    "Other Tactrix software won't see the cable on this PC while it uses NC "
    "Flash's driver. You can switch back at any time in Settings > ECU > J2534."
)

# Returns why a switch can't run now (e.g. the ECU window holds the cable), or None.
SwitchBlocker = Callable[[], Optional[str]]


class OpenPortDriverSetup(QObject):
    """Runs the OpenPort driver switch, one at a time for the whole app."""

    # succeeded, message for the user, requester (who asked: "ecu_window", "settings")
    finished = Signal(bool, str, str)
    running_changed = Signal(bool)

    def __init__(self, parent: QObject, blocker: SwitchBlocker):
        super().__init__(parent)
        self._blocker = blocker
        self._process: QProcess | None = None
        self._log_path = ""
        self._action = ""
        self._requester = ""

    @property
    def is_running(self) -> bool:
        return self._process is not None

    def offer_fix(self, state: OpenPortState, window: QWidget, requester: str) -> bool:
        """After a failed connect: offer the switch when Windows blocks the cable.

        Returns True if the switch was started.
        """
        if not state.needs_winusb or self.is_running or not builtin_driver_available():
            return False
        if state.driver == OpenPortDriver.BLOCKED:
            why = (
                "Windows is blocking the OpenPort's driver on this PC (Device "
                f"Manager shows Code {state.problem}), so the cable can't be used."
            )
        else:
            why = "The OpenPort has no working driver on this PC."
        reply = QMessageBox.question(
            window,
            "Set Up the Tactrix OpenPort",
            f"{why}\n\nNC Flash can switch the cable to its own driver, which "
            "uses Microsoft's WinUSB driver. Windows will ask for administrator "
            f"permission once.\n\n{_TRADE_OFF}\n\nSwitch the cable now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return False
        return self.start(INSTALL, requester)

    def choose_and_start(self, window: QWidget, requester: str) -> bool:
        """From Settings: show the cable's state and offer the matching switch."""
        title = "Tactrix OpenPort Driver"
        if sys.platform != "win32":
            QMessageBox.information(window, title, "This is only needed on Windows.")
            return False
        if self.is_running:
            QMessageBox.information(
                window, title, "A driver switch is already running; finish it first."
            )
            return False
        state = detect_openport()
        if state.driver == OpenPortDriver.NOT_FOUND:
            QMessageBox.information(
                window,
                title,
                "No Tactrix OpenPort is plugged in. Plug it in and try again.",
            )
            return False
        if state.driver == OpenPortDriver.WINUSB:
            question = (
                "The OpenPort uses NC Flash's driver (WinUSB).\n\n"
                "Switch it back to Tactrix's driver? Only do this if you need "
                "EcuFlash or RomDrop on this PC: recent Windows 11 updates block "
                "Tactrix's driver, and the cable then stops working in every "
                "program until you switch it back.\n\nClose any other program "
                "using the cable first."
            )
            action, default = RESTORE, QMessageBox.No
        else:
            if not builtin_driver_available():
                QMessageBox.warning(
                    window,
                    title,
                    "This NC Flash build doesn't include its own OpenPort driver, "
                    "so switching the cable would leave it unusable.",
                )
                return False
            current = {
                OpenPortDriver.TACTRIX: "Tactrix's driver, which works on this PC",
                OpenPortDriver.BLOCKED: (
                    f"a driver that Windows blocks (Code {state.problem})"
                ),
                OpenPortDriver.NO_DRIVER: "no working driver",
            }.get(state.driver, f"another driver ({state.service or 'unknown'})")
            question = (
                f"The OpenPort uses {current}.\n\nSwitch it to NC Flash's "
                "driver (Microsoft's WinUSB)? Windows will ask for "
                f"administrator permission once.\n\n{_TRADE_OFF}"
            )
            # Optional where Tactrix's driver works: default to leaving it.
            working = state.driver == OpenPortDriver.TACTRIX
            action = INSTALL
            default = QMessageBox.No if working else QMessageBox.Yes
        reply = QMessageBox.question(
            window, title, question, QMessageBox.Yes | QMessageBox.No, default
        )
        if reply != QMessageBox.Yes:
            return False
        return self.start(action, requester)

    def start(self, action: str, requester: str) -> bool:
        """Run the switch elevated. Emits finished when done."""
        if self.is_running:
            return False
        blocked = self._blocker()
        if blocked:
            self.finished.emit(False, blocked, requester)
            return False
        script = find_setup_script()
        if script is None:
            self.finished.emit(
                False,
                "The driver setup script is missing from this NC Flash install.",
                requester,
            )
            return False
        with open(script, encoding="utf-8") as f:
            script_text = f.read()
        log_name = new_log_name()
        self._log_path = os.path.join(setup_log_dir(), log_name)
        self._action, self._requester = action, requester
        command = elevated_setup_command(action, script_text, log_name)
        logger.info("OpenPort driver setup: %s (log %s)", action, self._log_path)
        self._process = QProcess(self)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self.running_changed.emit(True)
        self._process.start(command[0], command[1:])
        return True

    def _on_process_error(self, error) -> None:
        if error == QProcess.FailedToStart and self._process is not None:
            self._finish(False, "Windows PowerShell could not be started.")

    def _on_process_finished(self, exit_code: int, _status) -> None:
        if self._process is None:
            return
        result, log_text = read_setup_result(self._log_path)
        if log_text:
            logger.info("OpenPort driver setup log:\n%s", log_text)
        if exit_code == EXIT_DECLINED and not log_text:
            self._finish(
                False,
                "Windows' administrator prompt was declined, so nothing changed.",
            )
            return
        if exit_code == EXIT_BUSY:
            self._finish(False, "Another driver switch is already running.")
            return
        if exit_code == EXIT_LAUNCH_FAILED and not log_text:
            self._finish(False, "Windows couldn't start the driver switch.")
            return
        state = detect_openport()
        if result == "restart":
            self._finish(
                True,
                "The driver switch is installed. Restart Windows to finish it.",
            )
        elif result == "ok" and self._action == INSTALL:
            if state.driver == OpenPortDriver.WINUSB:
                self._finish(True, "The OpenPort now uses NC Flash's driver.")
            elif state.driver == OpenPortDriver.NOT_FOUND:
                self._finish(
                    True,
                    "NC Flash's driver is installed. It applies when you plug "
                    "the OpenPort in.",
                )
            else:
                self._finish(
                    False, f"The setup ran, but the cable reports {state.driver.value}."
                )
        elif result == "ok" and state.driver == OpenPortDriver.WINUSB:
            self._finish(
                False,
                "The cable is still on WinUSB: another program's WinUSB setup "
                "for it is installed. Remove it in Device Manager.",
            )
        elif result == "ok":
            message = {
                OpenPortDriver.TACTRIX: "The OpenPort is back on Tactrix's driver.",
                OpenPortDriver.BLOCKED: (
                    "The OpenPort is back on Tactrix's driver, which Windows "
                    f"blocks on this PC (Code {state.problem})."
                ),
                OpenPortDriver.NO_DRIVER: (
                    "NC Flash's driver was removed. Tactrix's driver isn't "
                    "installed, so the cable has no driver now."
                ),
                OpenPortDriver.NOT_FOUND: "NC Flash's driver was removed.",
            }.get(state.driver, f"The cable now reports {state.driver.value}.")
            self._finish(True, message)
        else:
            reason = next(
                (ln for ln in log_text.splitlines() if ln.startswith("RESULT:")),
                f"exit code {exit_code}",
            )
            self._finish(
                False,
                f"The driver switch failed ({reason}). It may be partly applied; "
                f"the details are in {self._log_path}.",
            )

    def _finish(self, ok: bool, message: str) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.deleteLater()
        self.running_changed.emit(False)
        self.finished.emit(ok, message, self._requester)
