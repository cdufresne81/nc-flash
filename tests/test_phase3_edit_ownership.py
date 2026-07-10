"""Phase 3 (C1/C2/C5): RomDocument is the single owner of per-ROM edit state.

Covers:
- RomDocument owns a TableEditState + its tint (get/set/is_color_assigned).
- The color allocator (first ROM gray, palette after, index monotonic across
  close-all) works off the document, not a path-keyed dict.
- Edit handlers write to the ROM named by the signal's rom_path — never the
  active tab — and fail loud (skip) when the rom_path is unknown (kills the old
  silent active-tab fallback, C2).
- TableViewerWindow re-emits the viewer's edit signals with rom_path bound (C5).
- A standalone TableViewer (no owner) still tracks borders via its own state.
- MainWindow no longer carries the legacy per-path dicts, and the viewer
  constructors no longer accept the shared-dict kwargs.
"""

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

import main
from main import MainWindow
from src.core.rom_definition import RomDefinition, RomID, Scaling, Table, TableType
from src.core.table_edit_state import TableEditState
from src.ui.rom_document import RomDocument
from src.ui.table_viewer import TableViewer
from src.ui.table_viewer_window import TableViewerWindow

_app = QApplication.instance() or QApplication([])


# --- helpers ----------------------------------------------------------------


def _make_definition():
    romid = RomID(
        xmlid="test",
        internalidaddress="0x0",
        internalidstring="T",
        ecuid="",
        make="",
        model="",
        flashmethod="",
        memmodel="",
        checksummodule="",
    )
    scaling = Scaling(
        name="TestScaling",
        units="",
        toexpr="x",
        frexpr="x",
        format="%0.2f",
        min=0.0,
        max=100.0,
        inc=1.0,
        storagetype="float",
        endian="big",
    )
    return RomDefinition(romid=romid, scalings={"TestScaling": scaling})


def _make_1d_table():
    return Table(
        name="Test 1D",
        address="0x600",
        type=TableType.ONE_D,
        elements=1,
        scaling="TestScaling",
    )


def _table():
    return SimpleNamespace(name="Fuel", address="0x1000")


# --- RomDocument ownership ---------------------------------------------------


def test_rom_document_owns_edit_state_and_color():
    doc = RomDocument("/tmp/a.bin", _make_definition(), MagicMock())
    try:
        assert isinstance(doc.edit_state, TableEditState)
        # Default: gray (None).
        assert doc.get_color() is None
        # A real tint round-trips through the document.
        doc.set_color(QColor(1, 2, 3))
        assert doc.get_color().name() == QColor(1, 2, 3).name()
    finally:
        doc.deleteLater()


# --- color allocator ---------------------------------------------------------


class _ColorDoc:
    """Stand-in with RomDocument's color API for allocator tests."""

    def __init__(self):
        self._c = None
        self._assigned = False

    def set_color(self, c):
        self._c = c
        self._assigned = True

    def get_color(self):
        return self._c


class _Stack:
    """Minimal QStackedWidget stand-in (count/widget)."""

    def __init__(self):
        self._w = []

    def add(self, w):
        self._w.append(w)

    def clear(self):
        self._w.clear()

    def count(self):
        return len(self._w)

    def widget(self, i):
        return self._w[i]


def test_color_allocator_first_gray_then_palette_and_monotonic_index():
    palette = ["c0", "c1", "c2", "c3"]
    fake = SimpleNamespace(
        rom_stack=_Stack(), _color_palette=palette, _next_color_index=0
    )

    # Mirror the real open flow: assign, then add to the stack.
    d1 = _ColorDoc()
    MainWindow._assign_rom_color(fake, d1)
    fake.rom_stack.add(d1)
    assert d1.get_color() is None  # first ROM = gray

    d2 = _ColorDoc()
    MainWindow._assign_rom_color(fake, d2)
    fake.rom_stack.add(d2)
    assert d2.get_color() == "c0"

    d3 = _ColorDoc()
    MainWindow._assign_rom_color(fake, d3)
    fake.rom_stack.add(d3)
    assert d3.get_color() == "c1"

    # Close every ROM.
    fake.rom_stack.clear()

    # Next ROM is gray again (no siblings) but the palette index keeps going.
    d4 = _ColorDoc()
    MainWindow._assign_rom_color(fake, d4)
    fake.rom_stack.add(d4)
    assert d4.get_color() is None

    d5 = _ColorDoc()
    MainWindow._assign_rom_color(fake, d5)
    fake.rom_stack.add(d5)
    assert d5.get_color() == "c2"  # continued, not restarted at c0


# --- handler attribution (C2) -----------------------------------------------


def test_cell_handler_writes_to_rompath_target_not_active_tab():
    doc_a = SimpleNamespace(rom_reader=MagicMock(), name="A")
    doc_b = SimpleNamespace(rom_reader=MagicMock(), name="B")
    seen = {}

    def _write(doc, fn, desc):
        seen["doc"] = doc
        return True

    fake = SimpleNamespace(
        _resolve_edit_target=lambda rp: {"romA": doc_a, "romB": doc_b}.get(rp),
        _write_to_rom_and_mark_modified=_write,
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _revert_failed_cell_edit=MagicMock(),
    )
    MainWindow._on_table_cell_changed(fake, "romB", _table(), 0, 0, 1.0, 2.0, 1, 2)
    assert seen["doc"] is doc_b  # resolved by rom_path, not active tab


def test_cell_handler_skips_when_rompath_unknown():
    fake = SimpleNamespace(
        _resolve_edit_target=lambda rp: None,
        _write_to_rom_and_mark_modified=MagicMock(return_value=True),
        table_undo_manager=MagicMock(),
        change_tracker=MagicMock(),
        _revert_failed_cell_edit=MagicMock(),
    )
    MainWindow._on_table_cell_changed(fake, "ghost", _table(), 0, 0, 1.0, 2.0, 1, 2)
    fake.table_undo_manager.record_cell_change.assert_not_called()
    fake.change_tracker.record_pending_change.assert_not_called()
    fake._write_to_rom_and_mark_modified.assert_not_called()


def test_resolve_edit_target_returns_none_and_logs(caplog):
    fake = SimpleNamespace(_find_document_by_rom_path=lambda p: None)
    with caplog.at_level(logging.ERROR, logger="main"):
        result = MainWindow._resolve_edit_target(fake, "ghost")
    assert result is None
    assert any("unknown rom_path" in r.message for r in caplog.records)


# --- window re-emit (C5) -----------------------------------------------------


def test_window_reemits_cell_edit_with_rom_path():
    table = _make_1d_table()
    data = {"values": np.array([1.0])}
    win = TableViewerWindow(table, data, _make_definition(), rom_path="/tmp/a.bin")
    got = []
    win.cell_edited.connect(lambda *a: got.append(a))
    try:
        win.viewer.cell_changed.emit(table, 0, 0, 1.0, 2.0, 1.0, 2.0)
    finally:
        win.close()
    assert got == [("/tmp/a.bin", table, 0, 0, 1.0, 2.0, 1.0, 2.0)]


def test_window_reemits_axis_edit_with_rom_path():
    table = _make_1d_table()
    data = {"values": np.array([1.0])}
    win = TableViewerWindow(table, data, _make_definition(), rom_path="/tmp/b.bin")
    got = []
    win.axis_edited.connect(lambda *a: got.append(a))
    try:
        win.viewer.axis_changed.emit(table, "x_axis", 2, 1.0, 2.0, 1.0, 2.0)
    finally:
        win.close()
    assert got == [("/tmp/b.bin", table, "x_axis", 2, 1.0, 2.0, 1.0, 2.0)]


# --- standalone viewer -------------------------------------------------------


def test_standalone_viewer_tracks_border_without_owner():
    viewer = TableViewer(_make_definition())
    try:
        assert isinstance(viewer._edit_state, TableEditState)
        viewer.mark_cell_modified("0x100", 1, 1)
        assert viewer._edit_state.is_cell_modified("0x100", 1, 1)
    finally:
        viewer.deleteLater()


# --- commit re-baseline (C3 minimal) ----------------------------------------


def test_reset_document_edit_baseline_clears_borders_recaptures_repaints():
    doc = SimpleNamespace(edit_state=TableEditState(), rom_reader=MagicMock())
    doc.edit_state.mark_cell_modified("0x100", 0, 0)
    doc.edit_state.capture_originals("0x100", {"values": np.array([1.0])})
    doc.rom_reader.rom_path = "romX"
    # Committed bytes differ from the stale original (9.0 vs 1.0).
    doc.rom_reader.read_table_data.return_value = {
        "values": np.array([9.0]),
        "x_axis": None,
        "y_axis": None,
    }
    window = SimpleNamespace(
        rom_path="romX", table=SimpleNamespace(address="0x100"), viewer=MagicMock()
    )
    fake = SimpleNamespace(open_table_windows=[window])

    MainWindow._reset_document_edit_baseline(fake, doc)

    # Borders dropped, originals re-captured from committed bytes, repaint asked.
    assert not doc.edit_state.is_cell_modified("0x100", 0, 0)
    assert doc.edit_state.get_original("0x100")["values"][0] == 9.0
    window.viewer.refresh_borders.assert_called_once()


def test_reset_document_edit_baseline_none_document_is_noop():
    # Must not raise when there is no document.
    MainWindow._reset_document_edit_baseline(
        SimpleNamespace(open_table_windows=[]), None
    )


# --- no legacy shared state --------------------------------------------------


def test_mainwindow_has_no_legacy_state_dicts():
    src = inspect.getsource(main.MainWindow.__init__)
    assert "self.modified_cells" not in src
    assert "self.original_table_values" not in src
    assert "self.rom_colors" not in src


def test_viewer_constructors_reject_shared_dict_kwargs():
    tv_params = inspect.signature(TableViewer.__init__).parameters
    assert "modified_cells_dict" not in tv_params
    assert "original_values_dict" not in tv_params
    assert "edit_owner" in tv_params

    tvw_params = inspect.signature(TableViewerWindow.__init__).parameters
    assert "modified_cells_dict" not in tvw_params
    assert "original_values_dict" not in tvw_params
    assert "document" in tvw_params
