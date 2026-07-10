"""Regression tests for B15 — the "*" dirty marker must render on project tabs.

Standalone ROM tabs are labelled by file name; project tabs are labelled
"[P] {name}". _update_tab_title() must prefix "*" onto whichever base label the
document carries, rather than clobbering a project label with the bare file name.
Before the fix, project tabs never showed "*" (they weren't even connected to
modified_changed), and the code used document.file_name unconditionally.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from main import MainWindow

_app = QApplication.instance() or QApplication([])


def _fake_window(document):
    tab_bar = MagicMock()
    rom_stack = MagicMock()
    rom_stack.indexOf.return_value = 0
    return SimpleNamespace(tab_bar=tab_bar, rom_stack=rom_stack), tab_bar


def _doc(*, base_title, file_name, modified):
    return SimpleNamespace(
        tab_base_title=base_title,
        file_name=file_name,
        is_modified=lambda: modified,
    )


def test_standalone_clean_shows_plain_filename():
    doc = _doc(base_title="lf9veb.bin", file_name="lf9veb.bin", modified=False)
    win, tab_bar = _fake_window(doc)
    MainWindow._update_tab_title(win, doc)
    tab_bar.setTabText.assert_called_once_with(0, "lf9veb.bin")


def test_standalone_modified_shows_star_prefix():
    doc = _doc(base_title="lf9veb.bin", file_name="lf9veb.bin", modified=True)
    win, tab_bar = _fake_window(doc)
    MainWindow._update_tab_title(win, doc)
    tab_bar.setTabText.assert_called_once_with(0, "*lf9veb.bin")


def test_project_clean_keeps_project_label():
    doc = _doc(base_title="[P] MyTune", file_name="lf9veb.bin", modified=False)
    win, tab_bar = _fake_window(doc)
    MainWindow._update_tab_title(win, doc)
    tab_bar.setTabText.assert_called_once_with(0, "[P] MyTune")


def test_project_modified_prefixes_star_onto_project_label():
    """The bug: a modified project tab must read '*[P] name', not '*lf9veb.bin'."""
    doc = _doc(base_title="[P] MyTune", file_name="lf9veb.bin", modified=True)
    win, tab_bar = _fake_window(doc)
    MainWindow._update_tab_title(win, doc)
    tab_bar.setTabText.assert_called_once_with(0, "*[P] MyTune")


def test_missing_base_title_falls_back_to_file_name():
    """Documents created before tab_base_title existed still render sanely."""
    doc = SimpleNamespace(file_name="legacy.bin", is_modified=lambda: True)
    win, tab_bar = _fake_window(doc)
    MainWindow._update_tab_title(win, doc)
    tab_bar.setTabText.assert_called_once_with(0, "*legacy.bin")
