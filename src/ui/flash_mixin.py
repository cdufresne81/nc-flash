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

from src.ecu.constants import DEFAULT_J2534_DLL
from src.ecu.flash_manager import (
    FlashManager,
    FlashState,
    FlashProgress,
    SECURE_MODULE_AVAILABLE,
)
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
                )
            elif self._operation == "dynamic_flash":
                self._manager.dynamic_flash(
                    self._kwargs["rom_data"],
                    self._kwargs["archive_rom_data"],
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

        # Only allow abort during transfer phases
        can_abort = p.state in (
            FlashState.TRANSFERRING_SBL,
            FlashState.TRANSFERRING_PROGRAM,
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

    def _get_j2534_dll_path(self) -> str:
        """Get J2534 DLL path: settings override, or default (op20pt32.dll)."""
        return self.settings.get_j2534_dll_path() or DEFAULT_J2534_DLL

    def _on_flash_rom(self):
        """Flash the current ROM to the ECU."""
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

        dll_path = self._get_j2534_dll_path()

        needs_save = document.is_modified()
        title = "Save and Flash ROM" if needs_save else "Flash ROM to ECU"
        action_label = "Save and Flash" if needs_save else "Flash"

        warning_text = (
            "<b>WARNING \u2014 Read carefully before proceeding</b><br><br>"
            "<ul>"
            "<li>The engine must be <b>OFF</b> \u2014 ignition key in the ON position only "
            "(dash lights on, engine not running)</li>"
            "<li>Ensure the car battery is healthy and fully charged</li>"
            "<li>Do not disconnect the OBD-II cable during the flash</li>"
            "<li><b>Do NOT interrupt the flashing process once it has started</b></li>"
            "</ul>"
            f"Flashing: <b>{document.file_name}</b>"
        )

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setText(warning_text)
        flash_button = msg_box.addButton(action_label, QMessageBox.AcceptRole)
        msg_box.addButton(QMessageBox.Cancel)
        msg_box.setDefaultButton(QMessageBox.Cancel)
        msg_box.exec()

        if msg_box.clickedButton() != flash_button:
            return

        # Save if needed
        if needs_save:
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

        # Load ROM data
        rom_path = Path(document.rom_path).resolve()
        try:
            rom_data = rom_path.read_bytes()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read ROM file:\n{e}")
            return

        # Create flash manager and run
        manager = FlashManager(dll_path)
        self._run_flash_operation(manager, "flash", rom_data=rom_data)

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

        dialog = FlashProgressDialog("Read ROM from ECU", self)
        worker = _FlashWorker(manager, "read")
        thread = QThread()
        worker.moveToThread(thread)

        worker.progress.connect(dialog.update_progress)
        worker.finished.connect(
            lambda: self._on_read_rom_finished(dialog, worker, manager)
        )
        worker.error.connect(lambda msg: dialog.on_finished(False, msg))
        dialog.abort_button.clicked.connect(manager.abort)

        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
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

        try:
            dtcs = manager.read_dtcs()
        except ECUError as e:
            QMessageBox.critical(self, "Error", f"Failed to read DTCs:\n{e}")
            return

        if not dtcs:
            QMessageBox.information(self, "DTCs", "No diagnostic trouble codes stored.")
            return

        dtc_text = "\n".join(f"  {d.formatted}: {d.description}" for d in dtcs)
        reply = QMessageBox.question(
            self,
            "Clear DTCs?",
            f"Found {len(dtcs)} DTC(s):\n\n{dtc_text}\n\nClear all DTCs?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            try:
                manager.clear_dtcs()
                QMessageBox.information(
                    self, "DTCs Cleared", "All DTCs have been cleared."
                )
                self.statusBar().showMessage("DTCs cleared")
            except ECUError as e:
                QMessageBox.critical(self, "Error", f"Failed to clear DTCs:\n{e}")

    def _on_patch_rom(self):
        """Apply an XOR patch to a stock ROM, producing a patched ROM."""
        from src.ecu.rom_utils import patch_rom
        from src.ecu.exceptions import ROMValidationError

        # Step 1: Select stock ROM
        stock_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Stock ROM",
            "",
            "ROM Files (*.bin);;All Files (*)",
        )
        if not stock_path:
            return

        # Step 2: Select patch file
        patch_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Patch File",
            "",
            "ROM Files (*.bin);;All Files (*)",
        )
        if not patch_path:
            return

        # Load both files
        try:
            stock_data = Path(stock_path).read_bytes()
            patch_data = Path(patch_path).read_bytes()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read file:\n{e}")
            return

        # Apply patch
        try:
            result = patch_rom(stock_data, patch_data)
        except ROMValidationError as e:
            QMessageBox.warning(self, "Patch Failed", str(e))
            return
        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Unexpected error during patching:\n{e}"
            )
            return

        # Step 3: Save result
        suggested_name = result.suggested_filename()
        save_dir = str(Path(stock_path).parent)

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Patched ROM",
            str(Path(save_dir) / suggested_name),
            "ROM Files (*.bin);;All Files (*)",
        )
        if not save_path:
            return

        try:
            Path(save_path).write_bytes(result.patched_rom)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save patched ROM:\n{e}")
            return

        cal_str = result.cal_id.decode("ascii", errors="replace").rstrip("\x00")
        QMessageBox.information(
            self,
            "Patch Applied",
            f"Patched ROM saved successfully.\n\n"
            f"Cal ID: {cal_str}\n"
            f"ROM ID: {result.rom_id}\n"
            f"Stock CRC: 0x{result.stock_crc:08X}\n"
            f"Patched CRC: 0x{result.patched_crc:08X}",
        )
        self.statusBar().showMessage(f"Patched ROM saved: {Path(save_path).name}")
        logger.info(f"Patch applied: {stock_path} + {patch_path} -> {save_path}")

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

        worker.progress.connect(dialog.update_progress)
        worker.finished.connect(lambda: dialog.on_finished(True))
        worker.error.connect(lambda msg: dialog.on_finished(False, msg))
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
