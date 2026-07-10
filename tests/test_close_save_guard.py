"""Regression tests for B5 — a failed save-on-close must not lose edits or crash.

Both close paths (SessionMixin._handle_close for the window, MainWindow.close_tab
for a single tab) previously called document.save() bare. A RomFileError there
escaped closeEvent (a hard crash under PySide6) and the user's edits vanished
with no dialog. The guard catches it, surfaces the error, and aborts the close.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

import main
from main import MainWindow
from src.ui import session_mixin
from src.ui.session_mixin import SessionMixin
from src.core.exceptions import RomWriteError

_app = QApplication.instance() or QApplication([])


def _modified_doc_that_fails_to_save():
    doc = MagicMock()
    doc.is_modified.return_value = True
    doc.file_name = "tune.bin"
    doc.save.side_effect = RomWriteError("disk full")
    return doc


def test_handle_close_aborts_when_save_fails():
    """Window close is cancelled (event.ignore) and no session teardown runs."""
    doc = _modified_doc_that_fails_to_save()
    stack = MagicMock()
    stack.count.return_value = 1
    stack.widget.return_value = doc
    fake = SimpleNamespace(rom_stack=stack)
    event = MagicMock()

    with (
        patch.object(QMessageBox, "question", return_value=QMessageBox.Save),
        patch.object(session_mixin, "handle_rom_operation_error") as mock_handle,
    ):
        SessionMixin._handle_close(fake, event)

    mock_handle.assert_called_once()
    event.ignore.assert_called_once()
    event.accept.assert_not_called()


def test_close_tab_keeps_tab_open_when_save_fails():
    """The tab is not removed when its save fails, so the edits survive."""
    doc = _modified_doc_that_fails_to_save()
    tab_bar = MagicMock()
    tab_bar.count.return_value = 1
    rom_stack = MagicMock()
    rom_stack.widget.return_value = doc
    fake = SimpleNamespace(tab_bar=tab_bar, rom_stack=rom_stack)

    with (
        patch.object(QMessageBox, "question", return_value=QMessageBox.Save),
        patch.object(main, "handle_rom_operation_error") as mock_handle,
    ):
        MainWindow.close_tab(fake, 0)

    mock_handle.assert_called_once()
    tab_bar.removeTab.assert_not_called()
    rom_stack.removeWidget.assert_not_called()
