"""
Patch ROM Dialog

Single-window dialog for applying an XOR patch to a stock ROM.
Replaces the sequential file-dialog approach with an all-in-one view.
"""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QMessageBox,
)

logger = logging.getLogger(__name__)


class PatchRomDialog(QDialog):
    """Dialog for selecting a stock ROM, patch file, and output path."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Patch ROM")
        self.setMinimumWidth(500)
        self.setModal(True)

        layout = QVBoxLayout(self)

        # --- File selection group ---
        file_group = QGroupBox("Files")
        file_layout = QFormLayout()
        file_group.setLayout(file_layout)

        self._stock_edit = QLineEdit()
        self._stock_edit.setPlaceholderText("Select a stock ROM file...")
        self._stock_edit.setReadOnly(True)
        stock_btn = QPushButton("Browse...")
        stock_btn.setFixedWidth(80)
        stock_btn.clicked.connect(self._browse_stock)
        stock_row = QHBoxLayout()
        stock_row.addWidget(self._stock_edit)
        stock_row.addWidget(stock_btn)
        file_layout.addRow("Stock ROM:", stock_row)

        self._patch_edit = QLineEdit()
        self._patch_edit.setPlaceholderText("Select a patch file...")
        self._patch_edit.setReadOnly(True)
        patch_btn = QPushButton("Browse...")
        patch_btn.setFixedWidth(80)
        patch_btn.clicked.connect(self._browse_patch)
        patch_row = QHBoxLayout()
        patch_row.addWidget(self._patch_edit)
        patch_row.addWidget(patch_btn)
        file_layout.addRow("Patch File:", patch_row)

        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("Auto-generated: <CalID>_Rev_<RomID>.bin")
        output_btn = QPushButton("Browse...")
        output_btn.setFixedWidth(80)
        output_btn.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self._output_edit)
        output_row.addWidget(output_btn)
        file_layout.addRow("Output:", output_row)

        layout.addWidget(file_group)

        # --- Result group (hidden until patch applied) ---
        self._result_group = QGroupBox("Result")
        result_layout = QFormLayout()
        self._result_group.setLayout(result_layout)

        self._cal_label = QLabel("—")
        result_layout.addRow("Cal ID:", self._cal_label)

        self._rom_id_label = QLabel("—")
        result_layout.addRow("ROM ID:", self._rom_id_label)

        self._crc_label = QLabel("—")
        result_layout.addRow("Stock CRC:", self._crc_label)

        self._patch_crc_label = QLabel("—")
        result_layout.addRow("Patch CRC:", self._patch_crc_label)

        self._patched_crc_label = QLabel("—")
        result_layout.addRow("Patched CRC:", self._patched_crc_label)

        self._verify_label = QLabel("—")
        result_layout.addRow("Verification:", self._verify_label)

        self._result_group.setVisible(False)
        layout.addWidget(self._result_group)

        # --- Buttons ---
        btn_layout = QHBoxLayout()
        layout.addLayout(btn_layout)
        btn_layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)

        self._apply_btn = QPushButton("Apply Patch")
        self._apply_btn.setDefault(True)
        self._apply_btn.setEnabled(False)
        self._apply_btn.setMinimumWidth(100)
        self._apply_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 6px 16px; }"
        )
        self._apply_btn.clicked.connect(self._apply_patch)
        btn_layout.addWidget(self._apply_btn)

        # Track paths
        self._stock_path = None
        self._patch_path = None

    def _browse_stock(self):
        from src.utils.settings import get_settings

        roms_dir = get_settings().get_roms_directory()
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Stock ROM", roms_dir, "ROM Files (*.bin);;All Files (*)"
        )
        if path:
            self._stock_path = path
            self._stock_edit.setText(path)
            self._update_apply_state()
            self._auto_suggest_output()

    def _browse_patch(self):
        start_dir = str(Path(self._stock_path).parent) if self._stock_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Patch File",
            start_dir,
            "Patch Files (*.bin *.patch);;All Files (*)",
        )
        if path:
            self._patch_path = path
            self._patch_edit.setText(path)
            self._update_apply_state()

    def _browse_output(self):
        start_dir = str(Path(self._stock_path).parent) if self._stock_path else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Patched ROM", start_dir, "ROM Files (*.bin);;All Files (*)"
        )
        if path:
            self._output_edit.setText(path)

    def _update_apply_state(self):
        self._apply_btn.setEnabled(bool(self._stock_path) and bool(self._patch_path))

    def _auto_suggest_output(self):
        if self._stock_path and not self._output_edit.text():
            parent = Path(self._stock_path).parent
            self._output_edit.setText(str(parent / "<CalID>_Rev_<RomID>.bin"))

    def _apply_patch(self):
        from src.ecu.rom_utils import patch_rom
        from src.ecu.exceptions import ROMValidationError

        stock_path = str(self._stock_path)
        patch_path = str(self._patch_path)
        output_path = self._output_edit.text().strip()

        # Load files
        try:
            stock_data = Path(stock_path).read_bytes()
            patch_data = Path(patch_path).read_bytes()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read file:\n{e}")
            return

        # Apply patch
        self._result_group.setVisible(False)
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

        # Build output path from suggested filename
        suggested = result.suggested_filename()
        if not output_path or "<CalID>" in output_path:
            save_dir = Path(stock_path).parent
            output_path = str(save_dir / suggested)
            self._output_edit.setText(output_path)

        # Save
        try:
            Path(output_path).write_bytes(result.patched_rom)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save patched ROM:\n{e}")
            return

        # Show result
        cal_str = result.cal_id.decode("ascii", errors="replace").rstrip("\x00")

        self._cal_label.setText(cal_str)
        self._rom_id_label.setText(result.rom_id)
        self._crc_label.setText(f"0x{result.stock_crc:08X}")
        self._patch_crc_label.setText(f"0x{result.patch_crc:08X}")
        self._patched_crc_label.setText(f"0x{result.patched_crc:08X}")

        if result.crc_verified:
            self._verify_label.setText("PASSED")
            self._verify_label.setStyleSheet("color: #44aa44; font-weight: bold;")
        elif result.crc_warnings:
            warn_text = "WARNING: " + "; ".join(result.crc_warnings)
            self._verify_label.setText(warn_text)
            self._verify_label.setStyleSheet("color: #cc8800; font-weight: bold;")
        else:
            self._verify_label.setText("Skipped")
            self._verify_label.setStyleSheet("color: gray;")

        self._result_group.setVisible(True)

        logger.info(f"Patch applied: {stock_path} + {patch_path} -> {output_path}")
