"""
Standard ROM dialog

Pick a ROM (stock or tuned), see which standard ROM it belongs to, and move
its edited tables onto that standard. Writes a new file plus a report; never
changes the source ROM and never talks to an ECU.

All the logic is in ``src/core/standard_rom.py``; this is only the form.
"""

import json
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from src.core.standard_rom import (
    PACK_FILE_SUFFIX,
    StandardPack,
    StandardPackError,
    convert,
)
from src.utils.settings import get_settings

logger = logging.getLogger(__name__)

_INTRO = (
    "The 78 factory ROM IDs reduce to 13 standard ROMs. This moves the tables "
    "you edited onto the standard ROM for your car and writes a new file. "
    "Your ROM is not changed. Review the report before flashing the result."
)


class StandardRomDialog(QDialog):
    """Identify a ROM's standard and convert the ROM onto it."""

    def __init__(self, parent=None, rom_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Move ROM to Its Standard ROM")
        self.setMinimumWidth(640)
        self.setModal(True)

        # Result handed back to the caller after exec().
        self.converted_rom_path = None
        self.open_requested = False
        self._pack = None
        self._pack_path = ""

        layout = QVBoxLayout(self)
        intro = QLabel(_INTRO)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        files = QGroupBox("Files")
        form = QFormLayout()
        files.setLayout(form)

        self._rom_edit = QLineEdit(rom_path)
        self._rom_edit.setObjectName("standard_rom_source")
        self._rom_edit.setPlaceholderText("Select your ROM file...")
        self._rom_edit.setReadOnly(True)
        form.addRow("Your ROM:", self._row(self._rom_edit, self._browse_rom))

        self._pack_edit = QLineEdit(get_settings().get_standard_pack_path())
        self._pack_edit.setObjectName("standard_rom_pack")
        self._pack_edit.setPlaceholderText("Select the standard ROM pack file...")
        self._pack_edit.setReadOnly(True)
        pack_row = self._row(self._pack_edit, self._browse_pack)
        folder_btn = QPushButton("Folder...")
        folder_btn.setFixedWidth(80)
        folder_btn.setToolTip("Use a pack that is a folder instead of one file")
        folder_btn.clicked.connect(self._browse_pack_folder)
        pack_row.addWidget(folder_btn)
        form.addRow("Standard pack:", pack_row)
        layout.addWidget(files)

        self._report = QPlainTextEdit()
        self._report.setObjectName("standard_rom_report")
        self._report.setReadOnly(True)
        self._report.setMinimumHeight(260)
        self._report.setPlaceholderText(
            "Pick a ROM and the standard pack to see where the ROM belongs."
        )
        layout.addWidget(self._report)

        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        self._convert_btn = QPushButton("Convert and Save As...")
        self._convert_btn.setObjectName("standard_rom_convert")
        self._convert_btn.setEnabled(False)
        self._convert_btn.clicked.connect(self._convert)
        buttons.addWidget(self._convert_btn)

        self._refresh()

    def _row(self, edit: QLineEdit, on_browse) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit)
        button = QPushButton("Browse...")
        button.setFixedWidth(80)
        button.clicked.connect(on_browse)
        row.addWidget(button)
        return row

    # ------------------------------------------------------------------ #

    def _browse_rom(self):
        start = self._rom_edit.text() or get_settings().get_roms_directory()
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Your ROM", start, "ROM Files (*.bin);;All Files (*)"
        )
        if path:
            self._rom_edit.setText(path)
            self._refresh()

    def _browse_pack(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select the Standard ROM Pack",
            self._pack_edit.text(),
            f"Standard ROM Pack (*{PACK_FILE_SUFFIX});;All Files (*)",
        )
        self._set_pack(path)

    def _browse_pack_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select the Standard ROM Pack Folder", self._pack_edit.text()
        )
        self._set_pack(path)

    def _set_pack(self, path: str):
        if path:
            self._pack_edit.setText(path)
            get_settings().set_standard_pack_path(path)
            self._refresh()

    def _load(self):
        """(pack, rom bytes) for the current selection; raises on a bad pick."""
        path = self._pack_edit.text()
        if self._pack is None or self._pack_path != path:
            # A pack file is unpacked in memory: do it once per pick.
            self._pack = StandardPack(path)
            self._pack_path = path
        rom = Path(self._rom_edit.text()).read_bytes()
        return self._pack, rom

    def _refresh(self):
        """Show where the selected ROM belongs; enable Convert if it can move."""
        self._convert_btn.setEnabled(False)
        if not self._rom_edit.text() or not self._pack_edit.text():
            self._report.setPlainText("")
            return
        try:
            pack, rom = self._load()
            identity = pack.identify(rom)
        except (StandardPackError, OSError) as exc:
            self._report.setPlainText(f"Could not read the selection:\n{exc}")
            return
        if identity is None:
            self._report.setPlainText(
                "This ROM's ID is not one of the IDs the pack knows."
            )
            return
        self._report.setPlainText(identity.summary())
        self._convert_btn.setEnabled(identity.needs_move)

    def _convert(self):
        try:
            pack, rom = self._load()
            image, report = convert(pack, rom)
        except (StandardPackError, OSError) as exc:
            QMessageBox.warning(self, "Conversion Failed", str(exc))
            return
        self._report.setPlainText(report.summary())
        if image is None:
            QMessageBox.warning(
                self,
                "Not Converted",
                f"{report.reason}\n\nNothing was written. See the report for details.",
            )
            return

        source = Path(self._rom_edit.text())
        suggested = source.with_name(
            f"{source.stem}_on_{report.identity.target}{source.suffix}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Converted ROM",
            str(suggested),
            "ROM Files (*.bin);;All Files (*)",
        )
        if not path:
            return
        if Path(path).resolve() == source.resolve():
            QMessageBox.warning(
                self, "Not Saved", "Pick another name: your ROM is never overwritten."
            )
            return
        try:
            Path(path).write_bytes(image)
            Path(path + ".report.json").write_text(
                json.dumps(report.to_dict(), indent=1) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Failed to save:\n{exc}")
            return
        logger.info(
            "Standard ROM conversion: %s (%s) -> %s, %d tables moved, %d not moved",
            source.name,
            report.identity.rom_id,
            report.identity.target,
            len(report.moved),
            len(report.not_moved),
        )
        self.converted_rom_path = path
        answer = QMessageBox.question(
            self,
            "Converted",
            f"Saved {Path(path).name} and its report.\n\n"
            f"{len(report.moved)} edited tables were moved, "
            f"{len(report.not_moved)} were not.\n\n"
            "Open the converted ROM now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.open_requested = True
            self.accept()
