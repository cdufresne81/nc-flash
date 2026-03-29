"""
ECU Programming Window

Dedicated window for all ECU operations: connect, read conditions,
flash, read ROM, read/clear DTCs. Replaces scattered menu dialogs
with a single purpose-built interface.

Connection flow: Detect dongle → Connect ECU → Validate conditions → Actions
"""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QLabel,
    QPushButton,
    QProgressBar,
    QStackedWidget,
    QMessageBox,
    QFileDialog,
    QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, QObject, QTimer, Signal
from PySide6.QtGui import QFont

from src.ecu.constants import (
    DEFAULT_J2534_DLL,
    ARCHIVE_FILENAME,
    BATTERY_VOLTAGE_WARNING,
)
from src.ecu.session import ECUSession, ECUSessionState
from src.ecu.flash_manager import (
    FlashManager,
    FlashProgress,
    SECURE_MODULE_AVAILABLE,
)
from src.ecu.exceptions import ECUError, FlashAbortedError, ROMValidationError
from src.ui.log_console import LogConsole

logger = logging.getLogger(__name__)

# Refresh interval for voltage/RPM polling (ms)
CONDITION_POLL_INTERVAL = 5000


# --- Workers ---


class _FlashWorker(QObject):
    """Worker for flash/read operations in background thread."""

    progress = Signal(object)  # FlashProgress
    finished = Signal()
    error = Signal(str)

    def __init__(self, flash_manager: FlashManager, operation: str, **kwargs):
        super().__init__()
        self._manager = flash_manager
        self._operation = operation
        self._kwargs = kwargs
        self._result = None

    @property
    def result(self):
        return self._result

    def run(self):
        try:
            if self._operation == "flash":
                self._manager.flash_rom(
                    self._kwargs["rom_data"],
                    progress_cb=self._on_progress,
                    archive_path=self._kwargs.get("archive_path"),
                )
            elif self._operation == "dynamic_flash":
                self._manager.dynamic_flash(
                    self._kwargs["rom_data"],
                    self._kwargs["archive_path"],
                    progress_cb=self._on_progress,
                )
            elif self._operation == "read":
                self._result = self._manager.read_rom(
                    progress_cb=self._on_progress,
                )
            elif self._operation == "scan_ram":
                self._result = self._manager.scan_ram(
                    progress_cb=self._on_progress,
                )
            self.finished.emit()
        except FlashAbortedError:
            self.error.emit("Operation aborted by user")
        except ROMValidationError as e:
            # Not a real error — e.g. "ROMs are identical"
            logger.info("Flash skipped: %s", e)
            self.error.emit(str(e))
        except Exception as e:
            logger.error("Flash worker error: %s", e, exc_info=True)
            self.error.emit(str(e))

    def _on_progress(self, p: FlashProgress):
        self.progress.emit(p)


# --- Status Card ---


class _StatusCard(QFrame):
    """A small card showing a title, value, and status subtitle."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statusCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedHeight(80)
        self.setStyleSheet(
            "#statusCard { background: #2a2a2a; border: 1px solid #444; "
            "border-radius: 6px; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        self._title = QLabel(title)
        self._title.setStyleSheet("color: #bbb; font-size: 10px;")
        layout.addWidget(self._title)

        self._value = QLabel("—")
        self._value.setStyleSheet("font-size: 15px; font-weight: bold; color: white;")
        layout.addWidget(self._value)

        self._subtitle = QLabel("")
        self._subtitle.setStyleSheet("font-size: 10px; color: #aaa;")
        layout.addWidget(self._subtitle)

    def get_value(self) -> str:
        return self._value.text()

    def set_value(self, text: str, color: str = "white"):
        self._value.setText(text)
        self._value.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {color};"
        )

    def set_subtitle(self, text: str, color: str = "#aaa"):
        self._subtitle.setText(text)
        self._subtitle.setStyleSheet(f"font-size: 10px; color: {color};")


# --- Main Window ---


class ECUProgrammingWindow(QMainWindow):
    """Dedicated window for ECU programming operations."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._session = None
        self._voltage = None
        self._rpm = None
        self._dtcs = []
        self._flash_thread = None
        self._current_manager = None
        self._session_acquired = False
        self._current_operation = None
        self._ecu_busy = False  # True during any ECU operation
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(CONDITION_POLL_INTERVAL)
        self._poll_timer.timeout.connect(self._poll_conditions)

        self.setWindowTitle("ECU Programming")
        self.setMinimumSize(700, 500)
        self.resize(750, 580)

        self._build_ui()
        self._update_action_states()

        # Auto-connect on open
        QTimer.singleShot(100, self._on_connect)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # --- Connection Bar ---
        conn_frame = QFrame()
        conn_frame.setFrameShape(QFrame.StyledPanel)
        conn_frame.setStyleSheet(
            "QFrame { background: #333; border: 1px solid #555; "
            "border-radius: 6px; padding: 6px; }"
        )
        conn_layout = QHBoxLayout(conn_frame)
        conn_layout.setContentsMargins(12, 8, 12, 8)

        self._conn_label = QLabel("Disconnected")
        self._conn_label.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: gray; border: none;"
        )
        conn_layout.addWidget(self._conn_label)
        conn_layout.addStretch()

        self._btn_connect = QPushButton("Connect")
        self._btn_connect.clicked.connect(self._on_connect)
        conn_layout.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        self._btn_disconnect.setEnabled(False)
        conn_layout.addWidget(self._btn_disconnect)

        root.addWidget(conn_frame)

        # --- Status Cards ---
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(8)

        self._card_battery = _StatusCard("BATTERY")
        self._card_engine = _StatusCard("ENGINE")
        self._card_ecu = _StatusCard("ECU")

        cards_layout.addWidget(self._card_battery)
        cards_layout.addWidget(self._card_engine)
        cards_layout.addWidget(self._card_ecu)
        root.addLayout(cards_layout)

        # --- Actions / Progress (stacked) ---
        self._stack = QStackedWidget()

        # Page 0: Action buttons
        actions_page = QWidget()
        actions_layout = QVBoxLayout(actions_page)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)

        # Primary flash button
        self._btn_flash_current = QPushButton("Flash Current ROM")
        self._btn_flash_current.setMinimumHeight(50)
        self._btn_flash_current.setStyleSheet(
            "QPushButton { font-size: 15px; font-weight: bold; "
            "background: #2d5a2d; color: white; border-radius: 6px; } "
            "QPushButton:hover { background: #3a7a3a; } "
            "QPushButton:disabled { background: #444; color: #888; }"
        )
        self._btn_flash_current.clicked.connect(self._on_flash_current)
        actions_layout.addWidget(self._btn_flash_current)

        self._flash_subtitle = QLabel("")
        self._flash_subtitle.setStyleSheet(
            "color: #888; font-size: 10px; margin-top: -4px;"
        )
        self._flash_subtitle.setAlignment(Qt.AlignCenter)
        actions_layout.addWidget(self._flash_subtitle)

        # Secondary buttons row
        row = QHBoxLayout()
        row.setSpacing(6)

        self._btn_full_flash = QPushButton("Full Flash...")
        self._btn_full_flash.setMinimumHeight(36)
        self._btn_full_flash.clicked.connect(self._on_full_flash)
        row.addWidget(self._btn_full_flash)

        self._btn_read_rom = QPushButton("Read ROM")
        self._btn_read_rom.setMinimumHeight(36)
        self._btn_read_rom.clicked.connect(self._on_read_rom)
        row.addWidget(self._btn_read_rom)

        self._btn_read_dtcs = QPushButton("Read DTCs")
        self._btn_read_dtcs.setMinimumHeight(36)
        self._btn_read_dtcs.clicked.connect(self._on_read_dtcs)
        row.addWidget(self._btn_read_dtcs)

        self._btn_clear_dtcs = QPushButton("Clear DTCs")
        self._btn_clear_dtcs.setMinimumHeight(36)
        self._btn_clear_dtcs.clicked.connect(self._on_clear_dtcs)
        row.addWidget(self._btn_clear_dtcs)

        self._btn_scan_ram = QPushButton("Scan RAM")
        self._btn_scan_ram.setMinimumHeight(36)
        self._btn_scan_ram.clicked.connect(self._on_scan_ram)
        row.addWidget(self._btn_scan_ram)

        actions_layout.addLayout(row)
        self._stack.addWidget(actions_page)

        # Page 1: Progress
        progress_page = QWidget()
        progress_layout = QVBoxLayout(progress_page)
        progress_layout.setContentsMargins(0, 8, 0, 0)

        self._progress_state = QLabel("Preparing...")
        self._progress_state.setStyleSheet("font-weight: bold; font-size: 13px;")
        progress_layout.addWidget(self._progress_state)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        progress_layout.addWidget(self._progress_bar)

        self._progress_detail = QLabel("")
        self._progress_detail.setStyleSheet("color: gray;")
        progress_layout.addWidget(self._progress_detail)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_abort = QPushButton("Abort")
        self._btn_abort.setEnabled(False)
        btn_row.addWidget(self._btn_abort)
        self._btn_done = QPushButton("Done")
        self._btn_done.setVisible(False)
        self._btn_done.clicked.connect(self._on_progress_done)
        btn_row.addWidget(self._btn_done)
        progress_layout.addLayout(btn_row)
        progress_layout.addStretch()

        self._stack.addWidget(progress_page)
        root.addWidget(self._stack)

        # --- Activity Log ---
        self._log = LogConsole(auto_register=True)
        self._log.setMinimumHeight(150)
        root.addWidget(self._log, stretch=1)

    # --- Connection ---

    def _get_dll_path(self) -> str:
        return self._main_window.settings.get_j2534_dll_path() or DEFAULT_J2534_DLL

    def _on_connect(self):
        if self._session and self._session.is_connected:
            return

        # Disconnect main window session if active
        if (
            hasattr(self._main_window, "_ecu_session")
            and self._main_window._ecu_session
        ):
            if self._main_window._ecu_session.is_connected:
                self._main_window._ecu_session.disconnect_ecu()

        self._conn_label.setText("Connecting...")
        self._conn_label.setStyleSheet(
            "font-weight: bold; font-size: 13px; color: #ccaa44; border: none;"
        )
        self._btn_connect.setEnabled(False)

        self._session = ECUSession(self._get_dll_path(), parent=self)
        self._session.state_changed.connect(self._on_session_state)
        self._session.connection_lost.connect(self._on_connection_lost)
        self._session.connect_ecu()

    def _on_disconnect(self):
        self._poll_timer.stop()
        if self._session:
            self._session.disconnect_ecu()

    def _on_session_state(self, state: str):
        if state == ECUSessionState.CONNECTED.value:
            self._conn_label.setText("Connected")
            self._conn_label.setStyleSheet(
                "font-weight: bold; font-size: 13px; color: #44aa44; border: none;"
            )
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._read_conditions_async()
        elif state == ECUSessionState.DISCONNECTED.value:
            self._conn_label.setText("Disconnected")
            self._conn_label.setStyleSheet(
                "font-weight: bold; font-size: 13px; color: gray; border: none;"
            )
            self._btn_connect.setEnabled(True)
            self._btn_disconnect.setEnabled(False)
            self._poll_timer.stop()
            self._voltage = None
            self._rpm = None
            self._reset_cards()
            self._update_action_states()
        elif state == ECUSessionState.BUSY.value:
            self._btn_disconnect.setEnabled(False)

        self._update_action_states()

    def _on_connection_lost(self, reason: str):
        logger.warning("Connection lost: %s", reason)
        self._poll_timer.stop()
        self._reset_cards()
        self._update_action_states()

    # --- Condition Reading ---

    def _read_conditions_async(self):
        """Read ECU conditions. Runs synchronously (UDS calls are fast)."""
        if not self._session or not self._session.uds:
            return
        uds = self._session.uds
        data = {}
        try:
            uds.tester_present()
        except Exception:
            pass
        try:
            vin_data = uds.read_vin_block()
            if vin_data:
                raw = vin_data[:17] if len(vin_data) >= 17 else vin_data
                data["vin"] = (
                    "".join(chr(b) if 0x20 <= b <= 0x7E else "" for b in raw) or "N/A"
                )
            else:
                data["vin"] = "N/A"
        except Exception:
            data["vin"] = "N/A"
        try:
            data["rom_id"] = uds.read_rom_id() or "N/A"
        except Exception:
            data["rom_id"] = "N/A"
        try:
            dtcs = uds.read_dtc_status()
            seen = set()
            unique = [d for d in dtcs if d.code not in seen and not seen.add(d.code)]
            data["dtc_count"] = len(unique)
            data["dtcs"] = unique
        except Exception:
            data["dtc_count"] = None
            data["dtcs"] = []
        try:
            data["voltage"] = uds.read_battery_voltage()
        except Exception:
            data["voltage"] = None
        try:
            data["rpm"] = uds.read_engine_rpm()
        except Exception:
            data["rpm"] = None
        self._on_conditions_loaded(data)

    def _on_conditions_loaded(self, data: dict):
        self._voltage = data.get("voltage")
        self._rpm = data.get("rpm")
        self._dtcs = data.get("dtcs", [])

        # Battery card
        if self._voltage is not None:
            v = self._voltage
            if v >= BATTERY_VOLTAGE_WARNING:
                self._card_battery.set_value(f"{v:.1f}V", "#44aa44")
                self._card_battery.set_subtitle("OK", "#44aa44")
            else:
                self._card_battery.set_value(f"{v:.1f}V", "#cc4444")
                self._card_battery.set_subtitle("LOW — charge battery", "#cc4444")
        else:
            self._card_battery.set_value("N/A", "#888")
            self._card_battery.set_subtitle("PID not supported")

        # Engine card
        if self._rpm is not None:
            if self._rpm < 1.0:
                self._card_engine.set_value("OFF", "#44aa44")
                self._card_engine.set_subtitle("Safe to flash", "#44aa44")
            else:
                self._card_engine.set_value(f"{self._rpm:.0f} RPM", "#cc4444")
                self._card_engine.set_subtitle("ENGINE RUNNING — turn off!", "#cc4444")
        else:
            self._card_engine.set_value("N/A", "#888")
            self._card_engine.set_subtitle("PID not supported")

        # ECU card
        vin = data.get("vin", "N/A")
        rom_id = data.get("rom_id", "N/A")
        dtc_count = data.get("dtc_count")
        self._card_ecu.set_value(rom_id, "white")
        dtc_str = f"{dtc_count} stored" if dtc_count is not None else "N/A"
        self._card_ecu.set_subtitle(f"VIN: {vin}\nDTCs: {dtc_str}", "#aaa")

        if self._voltage is not None or self._rpm is not None:
            log_parts = []
            if self._voltage is not None:
                log_parts.append(f"Battery: {self._voltage:.1f}V")
            if self._rpm is not None:
                log_parts.append(
                    f"Engine: {'OFF' if self._rpm < 1 else f'{self._rpm:.0f} RPM'}"
                )
            logger.info(" — ".join(log_parts))

        self._update_action_states()
        self._poll_timer.start()

    def _poll_conditions(self):
        """Periodic lightweight refresh of voltage + RPM (synchronous)."""
        if not self._session or not self._session.is_connected:
            self._poll_timer.stop()
            return
        if self._session.state == ECUSessionState.BUSY:
            return
        if self._ecu_busy:
            return

        uds = self._session.uds
        try:
            voltage = uds.read_battery_voltage()
        except Exception:
            voltage = None
        try:
            rpm = uds.read_engine_rpm()
        except Exception:
            rpm = None

        self._voltage = voltage
        self._rpm = rpm

        if self._voltage is not None:
            v = self._voltage
            if v >= BATTERY_VOLTAGE_WARNING:
                self._card_battery.set_value(f"{v:.1f}V", "#44aa44")
                self._card_battery.set_subtitle("OK", "#44aa44")
            else:
                self._card_battery.set_value(f"{v:.1f}V", "#cc4444")
                self._card_battery.set_subtitle("LOW — charge battery", "#cc4444")

        if self._rpm is not None:
            if self._rpm < 1.0:
                self._card_engine.set_value("OFF", "#44aa44")
                self._card_engine.set_subtitle("Safe to flash", "#44aa44")
            else:
                self._card_engine.set_value(f"{self._rpm:.0f} RPM", "#cc4444")
                self._card_engine.set_subtitle("ENGINE RUNNING — turn off!", "#cc4444")

        self._update_action_states()

    def _reset_cards(self):
        self._card_battery.set_value("—", "gray")
        self._card_battery.set_subtitle("")
        self._card_engine.set_value("—", "gray")
        self._card_engine.set_subtitle("")
        self._card_ecu.set_value("—", "gray")
        self._card_ecu.set_subtitle("")

    # --- Action Gating ---

    def _update_action_states(self):
        connected = self._session and self._session.is_connected
        flash_running = (
            self._flash_thread is not None and self._flash_thread.isRunning()
        )
        busy = self._ecu_busy or flash_running
        engine_off = self._rpm is None or self._rpm < 1.0
        has_rom = self._get_current_rom_data() is not None
        # Voltage is a warning only — never a hard block (bench PSU may read differently)
        safe_conditions = bool(connected and engine_off)

        # Nothing works while an operation is in progress
        if busy:
            self._btn_read_dtcs.setEnabled(False)
            self._btn_clear_dtcs.setEnabled(False)
            self._btn_full_flash.setEnabled(False)
            self._btn_read_rom.setEnabled(False)
            self._btn_scan_ram.setEnabled(False)
            self._btn_flash_current.setEnabled(False)
            return

        # Safe operations: just need connection
        self._btn_read_dtcs.setEnabled(bool(connected))
        self._btn_clear_dtcs.setEnabled(bool(connected))

        # Read ROM / Scan RAM: needs safe conditions + secure module
        can_read = safe_conditions and SECURE_MODULE_AVAILABLE
        self._btn_read_rom.setEnabled(bool(can_read))
        self._btn_scan_ram.setEnabled(bool(can_read))

        # Flash: needs safe conditions + secure module + ROM loaded
        can_flash = safe_conditions and SECURE_MODULE_AVAILABLE
        self._btn_full_flash.setEnabled(bool(can_flash and has_rom))

        # One-click flash
        doc = self._main_window.get_current_document()
        if doc and has_rom:
            rom_path = Path(doc.rom_path).resolve()
            archive_path = rom_path.parent / ARCHIVE_FILENAME
            if archive_path.is_file():
                self._btn_flash_current.setText("Flash Current ROM (Dynamic)")
                self._flash_subtitle.setText(f"{doc.file_name} — only changed blocks")
            else:
                self._btn_flash_current.setText("Flash Current ROM (Full)")
                self._flash_subtitle.setText(
                    f"{doc.file_name} — first flash, no archive"
                )
            self._btn_flash_current.setEnabled(bool(can_flash))
        else:
            self._btn_flash_current.setText("Flash Current ROM")
            self._flash_subtitle.setText("No ROM loaded in editor")
            self._btn_flash_current.setEnabled(False)

        # Tooltips for disabled states
        if not connected:
            tip = "Connect to ECU first"
        elif not engine_off:
            tip = "Turn engine OFF before flashing"
        elif not SECURE_MODULE_AVAILABLE:
            tip = "Security module not installed"
        else:
            tip = ""
        self._btn_full_flash.setToolTip(tip)
        self._btn_read_rom.setToolTip(tip)
        self._btn_scan_ram.setToolTip(tip)
        self._btn_flash_current.setToolTip(tip)

    def _get_current_rom_data(self) -> bytes | None:
        doc = self._main_window.get_current_document()
        if doc and hasattr(doc, "rom_path") and doc.rom_path:
            try:
                return Path(doc.rom_path).resolve().read_bytes()
            except Exception:
                pass
        return None

    # --- Flash Operations ---

    def _check_voltage_warning(self, operation: str = "flash") -> bool:
        """Warn if battery voltage is low. Returns True if OK to proceed.

        Args:
            operation: ``"flash"`` (default) shows a strong bricking warning;
                       ``"read"`` shows a softer timeout-only warning.
        """
        if self._voltage is not None and self._voltage < BATTERY_VOLTAGE_WARNING:
            if operation == "read":
                message = (
                    f"Battery voltage is {self._voltage:.1f}V "
                    f"(recommended: {BATTERY_VOLTAGE_WARNING}V+).\n\n"
                    "Low voltage may cause communication timeouts.\n"
                    "You can safely retry if the read fails.\n\n"
                    "Proceed anyway?"
                )
            else:
                message = (
                    f"Battery voltage is {self._voltage:.1f}V "
                    f"(recommended: {BATTERY_VOLTAGE_WARNING}V+).\n\n"
                    "Low voltage risks bricking the ECU during flash.\n"
                    "Proceed anyway?"
                )
            reply = QMessageBox.warning(
                self,
                "Low Battery Voltage",
                message,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return reply == QMessageBox.Yes
        return True

    def _on_flash_current(self):
        """One-click flash: dynamic if archive exists, else full."""
        self._ecu_busy = True
        self._update_action_states()
        started = False
        try:
            if not self._check_voltage_warning():
                return
            doc = self._main_window.get_current_document()
            if not doc:
                return

            # Auto-save
            if doc.is_modified():
                try:
                    doc.save()
                    doc.set_modified(False)
                    self._main_window._update_tab_title(doc)
                except Exception as e:
                    QMessageBox.warning(self, "Save Failed", f"Cannot save ROM:\n{e}")
                    return

            rom_path = Path(doc.rom_path).resolve()
            rom_data = rom_path.read_bytes()
            archive_path = str(rom_path.parent / ARCHIVE_FILENAME)

            if Path(archive_path).is_file():
                self._start_flash(
                    "dynamic_flash", rom_data=rom_data, archive_path=archive_path
                )
            else:
                self._start_flash("flash", rom_data=rom_data, archive_path=archive_path)
            started = True
        except Exception:
            pass
        finally:
            if not started:
                self._ecu_busy = False
                self._update_action_states()

    def _on_full_flash(self):
        self._ecu_busy = True
        self._update_action_states()
        started = False
        try:
            if not self._check_voltage_warning():
                return
            doc = self._main_window.get_current_document()
            if not doc:
                return
            if doc.is_modified():
                try:
                    doc.save()
                    doc.set_modified(False)
                    self._main_window._update_tab_title(doc)
                except Exception as e:
                    QMessageBox.warning(self, "Save Failed", f"Cannot save ROM:\n{e}")
                    return

            rom_path = Path(doc.rom_path).resolve()
            rom_data = rom_path.read_bytes()
            archive_path = str(rom_path.parent / ARCHIVE_FILENAME)
            self._start_flash("flash", rom_data=rom_data, archive_path=archive_path)
            started = True
        except Exception:
            pass
        finally:
            if not started:
                self._ecu_busy = False
                self._update_action_states()

    def _on_read_rom(self):
        self._ecu_busy = True
        self._update_action_states()
        if not self._check_voltage_warning(operation="read"):
            self._ecu_busy = False
            self._update_action_states()
            return
        self._start_flash("read")

    def _on_scan_ram(self):
        self._ecu_busy = True
        self._update_action_states()
        if not self._check_voltage_warning(operation="read"):
            self._ecu_busy = False
            self._update_action_states()
            return
        self._start_flash("scan_ram")

    def _start_flash(self, operation: str, **kwargs):
        """Start a flash/read operation with inline progress."""
        self._poll_timer.stop()
        self._update_action_states()  # Disable all buttons
        self._stack.setCurrentIndex(1)  # Show progress page
        self._progress_bar.setValue(0)
        self._progress_state.setText("Preparing...")
        self._progress_state.setStyleSheet("font-weight: bold; font-size: 13px;")
        self._progress_detail.setText("")
        # Only allow abort during read-only ops — aborting a flash risks bricking the ECU
        allow_abort = operation in ("read", "scan_ram")
        self._btn_abort.setEnabled(allow_abort)
        self._btn_abort.setVisible(allow_abort)
        self._btn_done.setVisible(False)

        manager = FlashManager(self._get_dll_path())

        # Borrow session if connected
        session_acquired = False
        if self._session and self._session.is_connected:
            try:
                device, channel_id, filter_id, uds = self._session.acquire()
                manager.use_session(device, channel_id, filter_id, uds)
                session_acquired = True
            except RuntimeError:
                pass

        self._current_manager = manager
        self._session_acquired = session_acquired
        self._current_operation = operation

        worker = _FlashWorker(manager, operation, **kwargs)
        thread = QThread()
        worker.moveToThread(thread)

        worker.progress.connect(self._on_flash_progress, Qt.QueuedConnection)
        worker.finished.connect(
            lambda: self._on_flash_finished(True, thread, worker),
            Qt.QueuedConnection,
        )
        worker.error.connect(
            lambda msg: self._on_flash_finished(False, thread, worker, msg),
            Qt.QueuedConnection,
        )
        try:
            self._btn_abort.clicked.disconnect()
        except RuntimeError:
            pass  # No previous connections
        self._btn_abort.clicked.connect(manager.abort)

        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)

        self._flash_thread = thread
        self._flash_worker = worker
        thread.start()

    def _on_flash_progress(self, p: FlashProgress):
        self._progress_state.setText(p.state.value.replace("_", " ").title())
        self._progress_bar.setValue(int(p.percent))
        self._progress_detail.setText(p.message)

    def _on_flash_finished(self, success: bool, thread, worker, error_msg: str = ""):
        # Don't wait on thread from within signal handler — schedule cleanup
        self._flash_thread = None
        self._ecu_busy = False
        QTimer.singleShot(0, lambda: self._cleanup_thread(thread))

        self._btn_abort.setEnabled(False)
        self._btn_done.setVisible(True)

        # Release session
        if self._session_acquired and self._session:
            # Flash ends with ECU reset, read leaves ECU in programming session
            # Both require reconnect to get back to a clean default session
            connection_dead = self._current_operation in (
                "flash",
                "dynamic_flash",
                "read",
                "scan_ram",
            )
            self._session.release(connection_dead=connection_dead)

        if success:
            self._progress_state.setText("Complete")
            self._progress_state.setStyleSheet(
                "font-weight: bold; font-size: 13px; color: #44aa44;"
            )
            self._progress_bar.setValue(100)

            if self._current_operation == "read" and worker.result:
                saved_path = self._auto_save_rom(worker.result)
                if saved_path:
                    self._progress_detail.setText(
                        f"ROM saved to {saved_path.name}. Reconnecting..."
                    )
                    # Open directory with file selected
                    try:
                        import subprocess

                        subprocess.Popen(["explorer", "/select,", str(saved_path)])
                    except Exception:
                        pass  # Non-critical — ROM is saved regardless
                    # Remind user to keep a backup
                    QTimer.singleShot(
                        500,
                        lambda: QMessageBox.information(
                            self,
                            "ROM Read Complete",
                            f"ROM saved to:\n{saved_path.name}\n\n"
                            "Keep a copy of this file somewhere safe.\n"
                            "This is your stock ROM backup — you will need it\n"
                            "to revert to factory if anything goes wrong.",
                        ),
                    )
                else:
                    self._progress_detail.setText("ROM save failed. Reconnecting...")
                # ECU is stuck in programming session — must reconnect
                QTimer.singleShot(500, self._auto_reconnect)
            elif self._current_operation == "scan_ram" and worker.result:
                saved_path = self._auto_save_ram_dump(worker.result)
                if saved_path:
                    self._progress_detail.setText(
                        f"RAM dump saved to {saved_path.name}. Reconnecting..."
                    )
                    try:
                        import subprocess

                        subprocess.Popen(["explorer", "/select,", str(saved_path)])
                    except Exception:
                        pass
                else:
                    self._progress_detail.setText(
                        "RAM dump save failed. Reconnecting..."
                    )
                QTimer.singleShot(500, self._auto_reconnect)
            elif self._current_operation in ("flash", "dynamic_flash"):
                self._progress_detail.setText("Flash complete! ECU is rebooting...")
                QTimer.singleShot(3000, self._auto_reconnect)
        else:
            # "ROMs are identical" is not a failure — show as info
            is_info = "identical" in error_msg.lower()
            if is_info:
                self._progress_state.setText("Nothing to flash")
                self._progress_state.setStyleSheet(
                    "font-weight: bold; font-size: 13px; color: #aaaaaa;"
                )
            else:
                self._progress_state.setText("Failed")
                self._progress_state.setStyleSheet(
                    "font-weight: bold; font-size: 13px; color: #cc4444;"
                )
            self._progress_detail.setText(error_msg)

    @staticmethod
    def _cleanup_thread(thread):
        """Clean up a QThread safely from the main thread."""
        if thread and thread.isRunning():
            thread.quit()
            thread.wait(3000)

    def _on_progress_done(self):
        """Return to actions page and reconnect if needed."""
        self._stack.setCurrentIndex(0)
        if not self._session or not self._session.is_connected:
            self._on_connect()
        else:
            self._update_action_states()

    def _auto_reconnect(self):
        """Try to reconnect after ECU reset."""
        if self._session:
            self._session.disconnect_ecu()
            self._session = None
        self._progress_detail.setText("Reconnecting...")
        QTimer.singleShot(500, self._on_connect)

    def _auto_save_rom(self, rom_data: bytearray) -> Path | None:
        """Auto-save ROM to project root as {ROM_ID}_{timestamp}.bin."""
        from datetime import datetime

        # Get ROM ID from ECU card (read during conditions)
        rom_id = self._card_ecu.get_value().strip()
        if not rom_id or rom_id == "—":
            rom_id = "ecu_read"
        # Strip .HEX suffix if present
        if rom_id.upper().endswith(".HEX"):
            rom_id = rom_id[:-4]

        auto_save_dir = Path.home() / ".nc-flash" / "reads"
        auto_save_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"{rom_id}_{timestamp}.bin"
        save_path = auto_save_dir / file_name
        try:
            save_path.write_bytes(rom_data)
            logger.info("ROM auto-saved to %s", save_path)
            return save_path
        except Exception as e:
            logger.error("Failed to auto-save ROM: %s", e)
            return None

    def _auto_save_ram_dump(self, ram_data: bytearray) -> Path | None:
        """Auto-save RAM dump to ~/.nc-flash/reads/ as {ROM_ID}_RAM_{timestamp}.bin."""
        from datetime import datetime

        rom_id = self._card_ecu.get_value().strip()
        if not rom_id or rom_id == "—":
            rom_id = "ecu"
        if rom_id.upper().endswith(".HEX"):
            rom_id = rom_id[:-4]

        auto_save_dir = Path.home() / ".nc-flash" / "reads"
        auto_save_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"{rom_id}_RAM_{timestamp}.bin"
        save_path = auto_save_dir / file_name
        try:
            save_path.write_bytes(ram_data)
            logger.info("RAM dump saved to %s (%d bytes)", save_path, len(ram_data))
            return save_path
        except Exception as e:
            logger.error("Failed to save RAM dump: %s", e)
            return None

    # --- DTC Operations ---

    def _on_read_dtcs(self):
        if not self._session or not self._session.uds:
            return
        self._ecu_busy = True
        self._update_action_states()
        try:
            manager = FlashManager(self._get_dll_path())
            dtcs = manager.read_dtcs(uds=self._session.uds)

            seen = set()
            unique = [d for d in dtcs if d.code not in seen and not seen.add(d.code)]
            self._dtcs = unique

            if not unique:
                QMessageBox.information(self, "DTCs", "No DTCs stored.")
            else:
                text = "\n".join(f"  {d.formatted}: {d.description}" for d in unique)
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Information)
                msg.setWindowTitle("DTCs")
                msg.setText(f"Found {len(unique)} DTC(s):\n\n{text}")
                msg.addButton(QMessageBox.Ok)
                clear_btn = msg.addButton("Clear DTCs", QMessageBox.ActionRole)
                msg.exec()
                if msg.clickedButton() == clear_btn:
                    self._do_clear_dtcs(manager)
            self._card_ecu.set_subtitle(
                f"DTCs: {len(unique)} stored", "#cc8800" if unique else "#44aa44"
            )
        except ECUError as e:
            QMessageBox.critical(self, "Error", f"Failed to read DTCs:\n{e}")
        finally:
            self._ecu_busy = False
            self._update_action_states()

    def _do_clear_dtcs(self, manager: FlashManager):
        """Send clear-DTC command and update the UI status card."""
        try:
            manager.clear_dtcs(uds=self._session.uds)
            QMessageBox.information(self, "DTCs Cleared", "All DTCs cleared.")
            self._dtcs = []
            self._card_ecu.set_subtitle("DTCs: 0 stored", "#44aa44")
            logger.info("DTCs cleared")
        except ECUError as e:
            QMessageBox.critical(self, "Error", f"Failed to clear DTCs:\n{e}")

    def _on_clear_dtcs(self):
        if not self._session or not self._session.uds:
            return
        self._ecu_busy = True
        self._update_action_states()
        reply = QMessageBox.question(
            self,
            "Clear DTCs?",
            "Clear all stored diagnostic trouble codes?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            self._ecu_busy = False
            self._update_action_states()
            return
        try:
            manager = FlashManager(self._get_dll_path())
            self._do_clear_dtcs(manager)
        except ECUError as e:
            QMessageBox.critical(self, "Error", f"Failed to clear DTCs:\n{e}")
        finally:
            self._ecu_busy = False
            self._update_action_states()

    # --- Cleanup ---

    def closeEvent(self, event):
        self._poll_timer.stop()
        if self._flash_thread and self._flash_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "Operation in Progress",
                "An ECU operation is running. Close anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            # Force-stop the running operation
            if self._current_manager:
                self._current_manager.abort()
            if self._flash_thread and self._flash_thread.isRunning():
                self._flash_thread.quit()
                self._flash_thread.wait(3000)
        if self._session:
            self._session.cleanup()
            self._session = None
        event.accept()
