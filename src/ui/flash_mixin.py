"""
Flash Mixin for MainWindow

Handles ECU flash operations: flash ROM, read ROM, clear DTCs.
Replaces the old romdrop.exe subprocess approach with native FlashManager.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self.settings (AppSettings instance)
- self.get_current_document() method
- self._update_tab_title(doc) method
- self.statusBar() method
"""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QHBoxLayout,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject

from src.ecu.constants import DEFAULT_J2534_DLL, ARCHIVE_FILENAME
from src.ecu.flash_manager import (
    FlashManager,
    FlashState,
    FlashProgress,
    SECURE_MODULE_AVAILABLE,
)
from src.ecu.session import ECUSession, ECUSessionState
from src.ecu.exceptions import (
    ECUError,
    FlashAbortedError,
    SecureModuleNotAvailable,
)

logger = logging.getLogger(__name__)


class _FlashWorker(QObject):
    """Worker that runs flash operations in a background thread."""

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
            self.finished.emit()
        except FlashAbortedError:
            self.error.emit("Operation aborted by user")
        except Exception as e:
            self.error.emit(str(e))

    def _on_progress(self, p: FlashProgress):
        self.progress.emit(p)


class FlashProgressDialog(QDialog):
    """Progress dialog for flash operations."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(450, 250)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)

        layout = QVBoxLayout(self)

        self.state_label = QLabel("Preparing...")
        self.state_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(self.state_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setStyleSheet("color: gray;")
        layout.addWidget(self.detail_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(100)
        self.log_text.setStyleSheet("font-family: monospace; font-size: 10px;")
        layout.addWidget(self.log_text)

        btn_layout = QHBoxLayout()
        layout.addLayout(btn_layout)
        btn_layout.addStretch()

        self.abort_button = QPushButton("Abort")
        self.abort_button.setEnabled(True)
        btn_layout.addWidget(self.abort_button)

        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_button)

    def update_progress(self, p: FlashProgress):
        self.state_label.setText(p.state.value.replace("_", " ").title())
        self.progress_bar.setValue(int(p.percent))
        self.detail_label.setText(p.message)
        self.log_text.append(p.message)

        # Allow abort during transfer and read phases
        can_abort = p.state in (
            FlashState.TRANSFERRING_SBL,
            FlashState.TRANSFERRING_PROGRAM,
            FlashState.READING,
        )
        self.abort_button.setEnabled(can_abort)

    def on_finished(self, success: bool, message: str = ""):
        self.abort_button.setEnabled(False)
        self.close_button.setEnabled(True)
        if success:
            self.state_label.setText("Complete")
            self.progress_bar.setValue(100)
            self.detail_label.setText(message or "Operation completed successfully")
            self.state_label.setStyleSheet(
                "font-weight: bold; font-size: 13px; color: green;"
            )
        else:
            self.state_label.setText("Failed")
            self.detail_label.setText(message)
            self.state_label.setStyleSheet(
                "font-weight: bold; font-size: 13px; color: red;"
            )


class FlashMixin:
    """Mixin providing ECU flash operations for MainWindow."""

    _ecu_session: ECUSession | None = None

    def _get_j2534_dll_path(self) -> str:
        """Get J2534 DLL path: settings override, or default (op20pt32.dll)."""
        return self.settings.get_j2534_dll_path() or DEFAULT_J2534_DLL

    # --- ECU Session Management ---

    def _on_ecu_connect(self):
        """ECU > Connect menu action."""
        if self._ecu_session and self._ecu_session.is_connected:
            return

        dll_path = self._get_j2534_dll_path()
        self._ecu_session = ECUSession(dll_path, parent=self)
        self._ecu_session.state_changed.connect(self._on_ecu_state_changed)
        self._ecu_session.connection_lost.connect(self._on_ecu_connection_lost)
        self._ecu_session.connect_ecu()
        self.statusBar().showMessage("Connecting to ECU...")

    def _on_ecu_disconnect(self):
        """ECU > Disconnect menu action."""
        if self._ecu_session:
            self._ecu_session.disconnect_ecu()

    def _on_ecu_state_changed(self, state: str):
        """Update based on session state (legacy — ECU window handles its own)."""
        pass

    def _on_ecu_connection_lost(self, reason: str):
        """Handle unexpected connection loss."""
        self.statusBar().showMessage(f"ECU connection lost: {reason}", 5000)
        logger.warning("ECU connection lost: %s", reason)

    def _cleanup_ecu_session(self):
        """Clean up ECU session on app exit."""
        if self._ecu_session:
            self._ecu_session.cleanup()
            self._ecu_session = None

    def _get_session_uds(self):
        """Return the session's UDS connection if connected, else None."""
        if self._ecu_session and self._ecu_session.is_connected:
            return self._ecu_session.uds
        return None

    def _on_flash_rom(self):
        """Flash the current ROM to the ECU via the setup dialog."""
        from src.ui.flash_setup_dialog import FlashSetupDialog

        document = self.get_current_document()
        if not document:
            QMessageBox.warning(self, "No ROM", "No ROM file is currently loaded.")
            return

        if not SECURE_MODULE_AVAILABLE:
            QMessageBox.warning(
                self,
                "Security Module Missing",
                "The ECU security module is not installed.\n\n"
                "Flash operations require the private _secure/ package.\n"
                "Contact the project maintainer for access.",
            )
            return

        # Auto-save if modified
        if document.is_modified():
            try:
                document.save()
                document.set_modified(False)
                self._update_tab_title(document)
                logger.info(f"Auto-saved ROM before flashing: {document.file_name}")
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "Save Failed",
                    f"Failed to save ROM before flashing:\n{e}\n\nFlash aborted.",
                )
                return

        rom_path = Path(document.rom_path).resolve()
        dll_path = self._get_j2534_dll_path()

        # Show setup dialog (connects to ECU, lets user pick mode)
        session_uds = self._get_session_uds()
        setup = FlashSetupDialog(
            document.file_name, rom_path, dll_path, self, session_uds=session_uds
        )
        if setup.exec() != QDialog.Accepted or setup.selected_mode is None:
            return

        # Load ROM data
        try:
            rom_data = rom_path.read_bytes()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read ROM file:\n{e}")
            return

        archive_path = str(rom_path.parent / ARCHIVE_FILENAME)
        manager = FlashManager(dll_path)

        # Borrow session if connected (flash will acquire exclusive access)
        session_acquired = False
        if self._ecu_session and self._ecu_session.is_connected:
            try:
                device, channel_id, filter_id, uds = self._ecu_session.acquire()
                manager.use_session(device, channel_id, filter_id, uds)
                session_acquired = True
            except RuntimeError:
                pass  # Session not in right state, flash will open its own

        try:
            if setup.selected_mode == "dynamic":
                self._run_flash_operation(
                    manager,
                    "dynamic_flash",
                    rom_data=rom_data,
                    archive_path=archive_path,
                )
            else:
                self._run_flash_operation(
                    manager, "flash", rom_data=rom_data, archive_path=archive_path
                )
        finally:
            if session_acquired and self._ecu_session:
                # If state is still IDLE, pre-checks failed before any ECU
                # communication — connection is fine.  Otherwise the ECU
                # was contacted (programming session, transfer, or reset)
                # and the connection state is uncertain.
                from src.ecu.flash_manager import FlashState

                connection_dead = manager.state != FlashState.IDLE
                self._ecu_session.release(connection_dead=connection_dead)

    def _on_read_rom(self):
        """Read ROM from ECU and save to file."""
        if not SECURE_MODULE_AVAILABLE:
            QMessageBox.warning(
                self,
                "Security Module Missing",
                "The ECU security module is not installed.\n\n"
                "ROM read operations require the private _secure/ package.",
            )
            return

        manager = FlashManager(self._get_j2534_dll_path())

        # Borrow session if connected
        session_acquired = False
        if self._ecu_session and self._ecu_session.is_connected:
            try:
                device, channel_id, filter_id, uds = self._ecu_session.acquire()
                manager.use_session(device, channel_id, filter_id, uds)
                session_acquired = True
            except RuntimeError:
                pass

        dialog = FlashProgressDialog("Read ROM from ECU", self)
        worker = _FlashWorker(manager, "read")
        thread = QThread()
        worker.moveToThread(thread)

        worker.progress.connect(dialog.update_progress, Qt.QueuedConnection)
        worker.finished.connect(
            lambda: self._on_read_rom_finished(dialog, worker, manager),
            Qt.QueuedConnection,
        )
        worker.error.connect(
            lambda msg: dialog.on_finished(False, msg), Qt.QueuedConnection
        )
        dialog.abort_button.clicked.connect(manager.abort)

        def _on_read_thread_finished():
            # Read ROM enters programming session — connection state unknown
            if session_acquired and self._ecu_session:
                self._ecu_session.release(connection_dead=True)

        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(_on_read_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()
        dialog.exec()

        # Clean up thread if still running
        if thread.isRunning():
            thread.quit()
            thread.wait(5000)

    def _on_read_rom_finished(self, dialog, worker, manager):
        """Handle completed ROM read — prompt save."""
        rom_data = worker.result
        if rom_data is None:
            dialog.on_finished(False, "No data received")
            return

        dialog.on_finished(True, f"ROM read complete ({len(rom_data)} bytes)")

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save ROM",
            "",
            "ROM Files (*.bin);;All Files (*)",
        )
        if file_path:
            try:
                Path(file_path).write_bytes(rom_data)
                logger.info(f"ROM saved to {file_path}")
                self.statusBar().showMessage(f"ROM saved: {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save ROM:\n{e}")

    def _on_clear_dtcs(self):
        """Read and clear DTCs from the ECU."""
        manager = FlashManager(self._get_j2534_dll_path())
        session_uds = self._get_session_uds()

        try:
            dtcs = manager.read_dtcs(uds=session_uds)
        except ECUError as e:
            QMessageBox.critical(self, "Error", f"Failed to read DTCs:\n{e}")
            return

        # Deduplicate DTCs, preserving order
        seen = set()
        unique_dtcs = []
        for d in dtcs:
            if d.code not in seen:
                seen.add(d.code)
                unique_dtcs.append(d)

        if not unique_dtcs:
            QMessageBox.information(self, "DTCs", "No diagnostic trouble codes stored.")
            return

        dtc_text = "\n".join(f"  {d.formatted}: {d.description}" for d in unique_dtcs)
        reply = QMessageBox.question(
            self,
            "Clear DTCs?",
            f"Found {len(unique_dtcs)} DTC(s):\n\n{dtc_text}\n\nClear all DTCs?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            try:
                manager.clear_dtcs(uds=session_uds)
                QMessageBox.information(
                    self, "DTCs Cleared", "All DTCs have been cleared."
                )
                self.statusBar().showMessage("DTCs cleared")
            except ECUError as e:
                QMessageBox.critical(self, "Error", f"Failed to clear DTCs:\n{e}")

    def _on_patch_rom(self):
        """Apply an XOR patch to a stock ROM via the Patch ROM dialog."""
        from src.ui.patch_dialog import PatchRomDialog

        dlg = PatchRomDialog(parent=self)
        dlg.exec()

    def _on_ecu_info(self):
        """Show ECU information (VIN, flash counter, ROM ID)."""
        session_uds = self._get_session_uds()

        try:
            if session_uds:
                # Use existing session
                vin_data = session_uds.read_vin_block()
                rom_id = session_uds.read_rom_id()
                dtcs = session_uds.read_dtc_status()
            else:
                # Open a temporary connection
                from src.ecu.j2534 import J2534Device, setup_isotp_flow_control
                from src.ecu.protocol import UDSConnection
                from src.ecu.constants import (
                    J2534_PROTOCOL_ISO15765,
                    CAN_BAUDRATE,
                    ISO15765_BS,
                    ISO15765_STMIN,
                )

                with J2534Device(self._get_j2534_dll_path()) as device:
                    channel_id = device.connect(
                        J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
                    )
                    device.set_config(channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})
                    setup_isotp_flow_control(device, channel_id)

                    uds = UDSConnection(device, channel_id)
                    uds.tester_present()

                    vin_data = uds.read_vin_block()
                    rom_id = uds.read_rom_id()
                    dtcs = uds.read_dtc_status()

            # VIN is exactly 17 printable ASCII characters
            if vin_data:
                raw = vin_data[:17] if len(vin_data) >= 17 else vin_data
                vin_str = (
                    "".join(chr(b) if 0x20 <= b <= 0x7E else "" for b in raw) or "N/A"
                )
            else:
                vin_str = "N/A"

            # Deduplicate DTCs, preserving order
            seen = set()
            unique_dtcs = []
            for d in dtcs:
                if d.code not in seen:
                    seen.add(d.code)
                    unique_dtcs.append(d)
            dtc_count = len(unique_dtcs)

            dtc_lines = ""
            if dtc_count > 0:
                dtc_lines = "\n".join(
                    f"  {d.formatted}: {d.description}" for d in unique_dtcs
                )
                dtc_lines = f"\n\n{dtc_lines}"

            QMessageBox.information(
                self,
                "ECU Info",
                f"VIN: {vin_str}\n"
                f"ROM ID: {rom_id or 'N/A'}\n"
                f"DTCs: {dtc_count} stored{dtc_lines}",
            )

        except ECUError as e:
            QMessageBox.critical(self, "ECU Error", f"Failed to read ECU info:\n{e}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to connect to ECU:\n{e}")

    def _run_flash_operation(self, manager: FlashManager, operation: str, **kwargs):
        """Run a flash operation with progress dialog."""
        title_map = {
            "flash": "Flashing ROM to ECU",
            "dynamic_flash": "Dynamic Flash ROM to ECU",
        }
        dialog = FlashProgressDialog(title_map.get(operation, "ECU Operation"), self)
        worker = _FlashWorker(manager, operation, **kwargs)
        thread = QThread()
        worker.moveToThread(thread)

        worker.progress.connect(dialog.update_progress, Qt.QueuedConnection)
        worker.finished.connect(lambda: dialog.on_finished(True), Qt.QueuedConnection)
        worker.error.connect(
            lambda msg: dialog.on_finished(False, msg), Qt.QueuedConnection
        )
        dialog.abort_button.clicked.connect(manager.abort)

        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.start()
        dialog.exec()

        if thread.isRunning():
            thread.quit()
            thread.wait(5000)
