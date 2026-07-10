"""Regression tests for B2 — undo/redo after a save must re-mark the document dirty.

save() / commit clear the modified flag but do NOT clear the QUndoStack, so a
user can undo a change that was already saved. The undo appliers write ROM bytes
back; if they don't also set_modified(True), is_modified() stays False and the
close prompt never fires -> the undo is silently discarded on close.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from main import MainWindow
from src.core.exceptions import RomWriteError

_app = QApplication.instance() or QApplication([])


class _Doc:
    """Minimal RomDocument stand-in with real modified semantics."""

    def __init__(self):
        self._modified = False
        self.rom_reader = MagicMock()

    def is_modified(self):
        return self._modified

    def set_modified(self, value):
        self._modified = value


def _cell_change():
    return SimpleNamespace(
        table_key=("rom", 0x1000),
        table_address=0x1000,
        row=1,
        col=2,
        new_value=42.0,
        new_raw=42,
        table_name="Fuel",
    )


def _axis_change():
    return SimpleNamespace(
        table_key=("rom", 0x1000),
        table_address=0x1000,
        axis_type="x",
        index=3,
        new_value=7.0,
        new_raw=7,
        table_name="Fuel",
    )


def _harness(doc):
    window = SimpleNamespace(
        viewer=MagicMock(),
        rom_path="rom",
        table=SimpleNamespace(address=0x1000),
    )
    return SimpleNamespace(
        _find_table_window=lambda key: window,
        _find_document_by_rom_path=lambda path: doc,
    )


def test_cell_undo_after_save_marks_document_modified():
    doc = _Doc()  # simulates a freshly-saved (clean) document
    MainWindow._apply_cell_change_from_undo(_harness(doc), _cell_change())
    assert doc.is_modified() is True
    doc.rom_reader.write_cell_value.assert_called_once()


def test_axis_undo_after_save_marks_document_modified():
    doc = _Doc()
    MainWindow._apply_axis_change_from_undo(_harness(doc), _axis_change())
    assert doc.is_modified() is True
    doc.rom_reader.write_axis_value.assert_called_once()


def test_cell_undo_write_failure_does_not_mark_modified():
    """A failed ROM write must not claim the document changed."""
    doc = _Doc()
    doc.rom_reader.write_cell_value.side_effect = RomWriteError("boom")
    MainWindow._apply_cell_change_from_undo(_harness(doc), _cell_change())
    assert doc.is_modified() is False
