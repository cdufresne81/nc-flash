"""Bulk edits and interpolation never write a value the ROM can't hold.

- Set/Add/Multiply refuse "1e999" (Python reads it as infinity) and other
  unstorable input. Before, Set 1e999 wrote 7f800000 (+inf) into float
  tables and crashed midway on integer tables.
- A bulk operation whose result is not a finite number (a multiply that
  overflows, arithmetic on an unprogrammed NaN cell) skips that cell before
  anything is touched.
- 2D interpolation with an axis row/column in the selection used to raise
  after changing cells on screen only; now the axes are left out.
- Interpolating an integer table with an offset scaling (x-41) keeps memory
  in step with the ROM on .5 values.
- Add/multiply/set/smoothing on real integer tables keep memory, screen and
  ROM in step, and undo restores the exact bytes.

Everything runs the real TableViewer against a real RomReader and compares
real ROM bytes.
"""

import math
import shutil
import struct
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QTableWidgetSelectionRange

import src.utils.settings as settings_module
from src.core.definition_parser import load_definition
from src.core.rom_definition import (
    AxisType,
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
)
from src.core.rom_reader import RomReader
from src.core.table_undo_manager import TableUndoManager, make_table_key
from src.ui.data_operation_dialogs import (
    AddValueDialog,
    MultiplyDialog,
    SetValueDialog,
)
from src.ui.table_viewer import TableViewer
from src.ui.table_viewer_helpers import interpolation as interp_module
from src.ui.table_viewer_helpers import operations as ops_module

_app = QApplication.instance() or QApplication([])

X_ADDR, Y_ADDR, DATA_ADDR = 0x100, 0x200, 0x300
N = 5
X_RAW = [100.0, 200.0, 300.0, 400.0, 500.0]
Y_RAW = [0.0, 5.0, 20.0, 45.0, 80.0]  # non-linear Y axis
DATA_RAW = [
    [10, 3, 25, 60, 0],
    [37, 90, 1, 12, 44],
    [5, 70, 33, 8, 21],
    [64, 2, 99, 17, 50],
    [20, 45, 6, 81, 30],
]


class _NoAutoRound:
    """The real settings, with auto-round forced off."""

    def __init__(self, real):
        self._real = real

    def get_auto_round(self):
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture(autouse=True)
def _no_auto_round(monkeypatch):
    real = settings_module.get_settings
    off = lambda: _NoAutoRound(real())  # noqa: E731
    monkeypatch.setattr(interp_module, "get_settings", off)
    monkeypatch.setattr(settings_module, "get_settings", off)


# --- builders ----------------------------------------------------------------


def _romid():
    return RomID(
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


def _scaling(name, storage, toexpr="x", frexpr="x", fmt="%0.2f"):
    return Scaling(
        name=name,
        units="",
        toexpr=toexpr,
        frexpr=frexpr,
        format=fmt,
        min=0.0,
        max=255.0,
        inc=1.0,
        storagetype=storage,
        endian="big",
    )


def _pack(storage, value):
    return struct.pack(">f", value) if storage == "float" else bytes([int(value)])


def _table_3d(tmp_path, storage, data=DATA_RAW):
    """5x5 3D table: float X/Y axes (Y non-linear), identity-scaled data."""
    scalings = {
        "Data": _scaling("Data", storage),
        "Axis": _scaling("Axis", "float", fmt="%0.1f"),
    }
    x_axis = Table(
        name="X",
        address=hex(X_ADDR),
        elements=N,
        scaling="Axis",
        type=TableType.ONE_D,
        axis_type=AxisType.X_AXIS,
    )
    y_axis = Table(
        name="Y",
        address=hex(Y_ADDR),
        elements=N,
        scaling="Axis",
        type=TableType.ONE_D,
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name="Map",
        address=hex(DATA_ADDR),
        elements=N * N,
        scaling="Data",
        type=TableType.THREE_D,
        children=[x_axis, y_axis],
    )
    rom = bytearray(0x400)
    for i in range(N):
        rom[X_ADDR + 4 * i : X_ADDR + 4 * i + 4] = struct.pack(">f", X_RAW[i])
        rom[Y_ADDR + 4 * i : Y_ADDR + 4 * i + 4] = struct.pack(">f", Y_RAW[i])
    size = 4 if storage == "float" else 1
    for i, v in enumerate(v for row in data for v in row):
        rom[DATA_ADDR + size * i : DATA_ADDR + size * (i + 1)] = _pack(storage, v)
    return _open(tmp_path, RomDefinition(_romid(), scalings, [table]), table, rom)


def _table_2d(tmp_path, scaling, raws):
    """2D table (float Y axis) with one data column of uint8 raw values."""
    scalings = {
        scaling.name: scaling,
        "Axis": _scaling("Axis", "float", fmt="%0.1f"),
    }
    y_axis = Table(
        name="Y",
        address=hex(Y_ADDR),
        elements=len(raws),
        scaling="Axis",
        type=TableType.ONE_D,
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name="Curve",
        address=hex(DATA_ADDR),
        elements=len(raws),
        scaling=scaling.name,
        type=TableType.TWO_D,
        children=[y_axis],
    )
    rom = bytearray(0x400)
    for i, raw in enumerate(raws):
        rom[Y_ADDR + 4 * i : Y_ADDR + 4 * i + 4] = struct.pack(">f", 10.0 * i)
        rom[DATA_ADDR + i] = raw
    return _open(tmp_path, RomDefinition(_romid(), scalings, [table]), table, rom)


def _open(tmp_path, definition, table, rom):
    rom_path = tmp_path / "rom.bin"
    rom_path.write_bytes(bytes(rom))
    reader = RomReader(str(rom_path), definition)
    return _viewer_for(definition, table, reader)


def _viewer_for(definition, table, reader):
    viewer = TableViewer(definition)
    viewer.display_table(table, reader.read_table_data(table))
    captured = {"cells": [], "axes": []}
    viewer.bulk_changes.connect(lambda _t, ch: captured["cells"].extend(ch))
    viewer.axis_bulk_changes.connect(lambda _t, ch: captured["axes"].extend(ch))
    return SimpleNamespace(
        reader=reader,
        table=table,
        viewer=viewer,
        captured=captured,
        original=bytes(reader.rom_data),
    )


@pytest.fixture
def cleanup():
    made = []
    yield made
    for s in made:
        s.viewer.deleteLater()


# --- helpers -----------------------------------------------------------------


def _select(viewer, top, left, bottom, right):
    tw = viewer.table_widget
    tw.clearSelection()
    tw.setRangeSelected(QTableWidgetSelectionRange(top, left, bottom, right), True)


def _select_all(viewer):
    tw = viewer.table_widget
    _select(viewer, 0, 0, tw.rowCount() - 1, tw.columnCount() - 1)


def _screen(viewer):
    """Every data/axis cell's text, keyed by its UserRole coords."""
    tw = viewer.table_widget
    out = {}
    for r in range(tw.rowCount()):
        for c in range(tw.columnCount()):
            item = tw.item(r, c)
            if item is not None and item.data(Qt.UserRole) is not None:
                out[tuple(item.data(Qt.UserRole))] = item.text()
    return out


def _memory(viewer):
    return {
        k: np.array(v, copy=True)
        for k, v in viewer._ctx.current_data.items()
        if isinstance(v, np.ndarray)
    }


def _apply_forward(s, cells, axes):
    """What MainWindow does on bulk_changes / axis_bulk_changes."""
    for row, col, _ov, _nv, _or, new_raw in cells:
        s.reader.write_cell_value(s.table, row, col, new_raw)
    for axis_type, idx, _ov, _nv, _or, new_raw in axes:
        s.reader.write_axis_value(s.table, axis_type, idx, new_raw)


def _undo(s, cells, axes):
    """Record the changes on a real undo stack wired like MainWindow, then undo."""
    manager = TableUndoManager()
    manager.set_callbacks(
        apply_cell=lambda ch: s.reader.write_cell_value(
            s.table, ch.row, ch.col, ch.new_raw
        ),
        apply_axis=lambda ch: s.reader.write_axis_value(
            s.table, ch.axis_type, ch.index, ch.new_raw
        ),
        update_pending=lambda ch, is_undo: None,
        update_pending_axis=lambda ch, is_undo: None,
        begin_bulk_update=lambda *a: None,
        end_bulk_update=lambda *a: None,
    )
    key = make_table_key(None, s.table.address)
    if cells:
        manager.record_bulk_cell_changes(s.table, cells, "edit")
    if axes:
        manager.record_axis_bulk_changes(s.table, axes, "edit axis")
    manager.set_active_stack(key)
    stack = manager.get_or_create_stack(key)
    while stack.canUndo():
        stack.undo()


def _assert_screen_memory_rom_agree(s):
    rom = s.reader.read_table_data(s.table)
    mem = s.viewer._ctx.current_data
    np.testing.assert_allclose(
        np.asarray(mem["values"], dtype=float),
        np.asarray(rom["values"], dtype=float),
        rtol=1e-6,
    )
    display = s.viewer._display
    value_fmt = display.get_value_format()
    values = np.asarray(rom["values"])
    for coords, text in _screen(s.viewer).items():
        if isinstance(coords[0], str):
            axis_type = AxisType.X_AXIS if coords[0] == "x_axis" else AxisType.Y_AXIS
            np.testing.assert_allclose(mem[coords[0]], rom[coords[0]], rtol=1e-6)
            expected = display.format_value(
                rom[coords[0]][coords[1]], display.get_axis_format(axis_type)
            )
        else:
            stored = values[coords[0]] if values.ndim == 1 else values[coords]
            expected = display.format_value(stored, value_fmt)
        assert text == expected, f"cell {coords}: screen {text!r} != ROM {expected!r}"


def _fake_exec(text):
    """Dialog.exec stand-in: type ``text``, press OK, report the outcome."""

    def fake(dialog):
        edit = getattr(dialog, "value_edit", None) or dialog.factor_edit
        edit.setText(text)
        dialog._on_accept()
        return dialog.result()

    return fake


# --- dialogs -------------------------------------------------------------------

BAD_VALUES = ["1e999", "-1e999", "1e39", "nan", "inf", "", "-", "abc"]


@pytest.mark.parametrize("dialog_cls", [SetValueDialog, AddValueDialog])
@pytest.mark.parametrize("text", BAD_VALUES)
def test_value_dialogs_refuse_unstorable_input(dialog_cls, text):
    dialog = dialog_cls()
    try:
        dialog.value_edit.setText(text)
        dialog._on_accept()
        assert dialog.result() != QDialog.Accepted
        assert not dialog.error_label.isHidden()
        assert dialog.error_label.text()
        assert dialog.get_value() == 0.0
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("dialog_cls", [SetValueDialog, AddValueDialog])
def test_value_dialogs_accept_a_normal_number(dialog_cls):
    dialog = dialog_cls()
    try:
        dialog.value_edit.setText("-12.5")
        dialog._on_accept()
        assert dialog.result() == QDialog.Accepted
        assert dialog.get_value() == -12.5
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("dialog_cls", [SetValueDialog, AddValueDialog])
@pytest.mark.parametrize("text", ["1,000", "-12,500", "+999,999"])
def test_value_dialogs_refuse_thousands_grouping(dialog_cls, text):
    # The comma decimal mark would read "1,000" as 1.0; refuse, don't guess.
    dialog = dialog_cls()
    try:
        dialog.value_edit.setText(text)
        dialog._on_accept()
        assert dialog.result() != QDialog.Accepted
        assert "1000" in dialog.error_label.text()
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("dialog_cls", [SetValueDialog, AddValueDialog])
@pytest.mark.parametrize(
    "text, expected",
    [("1,5", 1.5), ("0,125", 0.125), ("12,50", 12.5), ("1000", 1000.0)],
)
def test_value_dialogs_accept_comma_decimals(dialog_cls, text, expected):
    dialog = dialog_cls()
    try:
        dialog.value_edit.setText(text)
        dialog._on_accept()
        assert dialog.result() == QDialog.Accepted
        assert dialog.get_value() == expected
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("text", BAD_VALUES + ["0", "-1.5", "2000000"])
def test_multiply_dialog_refuses_unstorable_factor(text):
    dialog = MultiplyDialog()
    try:
        dialog.factor_edit.setText(text)
        dialog._on_accept()
        assert dialog.result() != QDialog.Accepted
        assert not dialog.error_label.isHidden()
        assert dialog.get_factor() == 1.0
    finally:
        dialog.deleteLater()


def test_multiply_dialog_accepts_a_normal_factor():
    dialog = MultiplyDialog()
    try:
        dialog.factor_edit.setText("1.1")
        dialog._on_accept()
        assert dialog.result() == QDialog.Accepted
        assert dialog.get_factor() == 1.1
    finally:
        dialog.deleteLater()


# --- Set 1e999 / overflowing multiply never reach the ROM ---------------------


@pytest.mark.parametrize("storage", ["float", "uint8"])
def test_set_1e999_is_refused_and_rom_unchanged(tmp_path, monkeypatch, storage):
    s = _table_3d(tmp_path, storage)
    try:
        monkeypatch.setattr(ops_module.SetValueDialog, "exec", _fake_exec("1e999"))
        screen, memory = _screen(s.viewer), _memory(s.viewer)
        _select_all(s.viewer)  # axes included: they used to change first
        s.viewer._ops.set_value_selection()

        assert s.captured == {"cells": [], "axes": []}
        assert bytes(s.reader.rom_data) == s.original
        assert _screen(s.viewer) == screen
        for key, arr in memory.items():
            np.testing.assert_array_equal(s.viewer._ctx.current_data[key], arr)
    finally:
        s.viewer.deleteLater()


@pytest.mark.parametrize("storage", ["float", "uint8"])
@pytest.mark.parametrize(
    "result", [float("inf"), float("-inf"), float("nan")], ids=["inf", "-inf", "nan"]
)
def test_bulk_set_non_finite_changes_nothing(tmp_path, storage, result):
    """The bulk path itself refuses a non-finite result, dialog or not.

    On a uint8 table this used to raise OverflowError after the axis cells
    were already changed on screen; on a float table it wrote 7f800000.
    """
    s = _table_3d(tmp_path, storage)
    try:
        screen, memory = _screen(s.viewer), _memory(s.viewer)
        _select_all(s.viewer)
        cells, axes = s.viewer._ops.apply_bulk_operation(lambda _v: result, "set")
        assert (cells, axes) == ([], [])
        assert _screen(s.viewer) == screen
        for key, arr in memory.items():
            np.testing.assert_array_equal(s.viewer._ctx.current_data[key], arr)
        assert bytes(s.reader.rom_data) == s.original
    finally:
        s.viewer.deleteLater()


def test_multiply_dialog_overflow_is_refused(tmp_path, monkeypatch):
    s = _table_3d(tmp_path, "float")
    try:
        monkeypatch.setattr(ops_module.MultiplyDialog, "exec", _fake_exec("1e999"))
        _select_all(s.viewer)
        s.viewer._ops.multiply_selection()
        assert s.captured == {"cells": [], "axes": []}
        assert bytes(s.reader.rom_data) == s.original
    finally:
        s.viewer.deleteLater()


@pytest.mark.parametrize("storage", ["float", "uint8"])
def test_multiply_overflow_skips_cells_and_rom_matches_screen(tmp_path, storage):
    """v * 1e308 is inf for |v| >= 2 and unstorable otherwise; 0 stays 0."""
    s = _table_3d(tmp_path, storage)
    try:
        _select_all(s.viewer)
        cells, axes = s.viewer._ops.apply_bulk_operation(
            lambda v: v * 1e308, "multiply"
        )
        assert (cells, axes) == ([], [])
        assert bytes(s.reader.rom_data) == s.original
        _assert_screen_memory_rom_agree(s)
    finally:
        s.viewer.deleteLater()


def test_unprogrammed_nan_cell_is_skipped_but_stays_editable(tmp_path):
    """NaN is what an unprogrammed (FFFFFFFF) float cell reads as.

    Arithmetic on it gives NaN, which must not be written (struct would store
    7fc00000, changing the bytes). Setting it to a real number still works,
    as before.
    """
    s = _table_3d(tmp_path, "float")
    s.viewer.deleteLater()
    nan_at = DATA_ADDR + 4 * (2 * N + 2)  # cell (2, 2)
    rom = bytearray(s.original)
    rom[nan_at : nan_at + 4] = b"\xff\xff\xff\xff"
    (tmp_path / "rom.bin").write_bytes(bytes(rom))
    s = _viewer_for(
        s.reader.definition,
        s.table,
        RomReader(str(tmp_path / "rom.bin"), s.reader.definition),
    )
    try:
        tw = s.viewer.table_widget
        _select(s.viewer, 1, 1, tw.rowCount() - 1, tw.columnCount() - 1)
        cells, axes = s.viewer._ops.apply_bulk_operation(lambda v: v + 1.0, "add")
        changed = {(r, c) for r, c, *_ in cells}
        assert (2, 2) not in changed
        assert len(changed) == N * N - 1
        _apply_forward(s, cells, axes)
        assert bytes(s.reader.rom_data[nan_at : nan_at + 4]) == b"\xff\xff\xff\xff"

        # Set the NaN cell itself to 7: still allowed, recorded with NaN old.
        _select(s.viewer, 3, 3, 3, 3)  # UI (3, 3) = data (2, 2)
        cells, axes = s.viewer._ops.apply_bulk_operation(lambda _v: 7.0, "set")
        assert [(r, c) for r, c, *_ in cells] == [(2, 2)]
        assert math.isnan(cells[0][4]) and cells[0][5] == 7.0
        _apply_forward(s, cells, axes)
        assert struct.unpack(">f", s.reader.rom_data[nan_at : nan_at + 4])[0] == 7.0
    finally:
        s.viewer.deleteLater()


# --- 2D interpolation with an axis in the selection ---------------------------


@pytest.mark.parametrize("storage", ["float", "uint8"])
@pytest.mark.parametrize("top", [1, 0], ids=["y-axis-column", "both-axes-and-corner"])
def test_2d_interpolation_with_axis_selected_matches_data_only(tmp_path, storage, top):
    """Selecting the Y-axis column (and X-axis row) interpolates the data
    region only: same result as selecting just the data cells, no exception,
    axes untouched, screen = memory = ROM, and undo restores the bytes."""
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    ref = _table_3d(ref_dir, storage)
    s = _table_3d(tmp_path, storage)
    try:
        _select(ref.viewer, 1, 1, N, N)
        ref.viewer._interp.interpolate_2d()
        assert ref.captured["cells"], "reference interpolation changed nothing"

        axes_before = {k: v for k, v in _memory(s.viewer).items() if k != "values"}
        _select(s.viewer, top, 0, N, N)
        s.viewer._interp.interpolate_2d()  # used to raise IndexError

        assert s.captured["axes"] == []
        assert s.captured["cells"] == ref.captured["cells"]
        for key, arr in axes_before.items():
            np.testing.assert_array_equal(s.viewer._ctx.current_data[key], arr)

        _apply_forward(s, s.captured["cells"], [])
        _assert_screen_memory_rom_agree(s)
        assert bytes(s.reader.rom_data) != s.original
        _undo(s, s.captured["cells"], [])
        assert bytes(s.reader.rom_data) == s.original
    finally:
        ref.viewer.deleteLater()
        s.viewer.deleteLater()


def test_2d_interpolation_two_ranges_emits_once(tmp_path):
    """Each selection range used to re-emit every change made so far."""
    s = _table_3d(tmp_path, "float")
    try:
        tw = s.viewer.table_widget
        tw.clearSelection()
        tw.setRangeSelected(QTableWidgetSelectionRange(1, 1, 3, 3), True)
        tw.setRangeSelected(QTableWidgetSelectionRange(1, 4, 3, 5), True)
        emits = []
        s.viewer.bulk_changes.connect(lambda _t, ch: emits.append(list(ch)))
        s.viewer._interp.interpolate_2d()
        assert len(emits) == 1
        coords = [(r, c) for r, c, *_ in emits[0]]
        assert len(coords) == len(set(coords))
        _apply_forward(s, emits[0], [])
        _assert_screen_memory_rom_agree(s)
    finally:
        s.viewer.deleteLater()


# --- integer table, offset scaling: memory follows the ROM --------------------


def test_offset_scaled_int_interpolation_memory_matches_rom(tmp_path):
    """x-41 on uint8: display 0..2 over 5 rows interpolates to 0.5/1/1.5.

    The array rounded 0.5 -> 0 while the ROM writer rounded raw 41.5 -> 42
    (display 1), so memory [0,0,1,2,2] disagreed with ROM [0,1,1,1,2].
    """
    scaling = _scaling("Offset", "uint8", toexpr="x-41", frexpr="x+41", fmt="%0.1f")
    s = _table_2d(tmp_path, scaling, [41, 50, 50, 50, 43])
    try:
        values = s.viewer._ctx.current_data["values"]
        assert np.issubdtype(values.dtype, np.integer)
        _select(s.viewer, 0, 1, 4, 1)
        s.viewer._interp.interpolate_vertical()
        assert {r for r, *_ in s.captured["cells"]} == {1, 2, 3}

        _apply_forward(s, s.captured["cells"], [])
        assert list(s.reader.read_table_data(s.table)["values"]) == [0, 0, 1, 2, 2]
        assert list(s.viewer._ctx.current_data["values"]) == [0, 0, 1, 2, 2]
        _assert_screen_memory_rom_agree(s)
        _undo(s, s.captured["cells"], [])
        assert bytes(s.reader.rom_data) == s.original
    finally:
        s.viewer.deleteLater()


# --- bulk operations on real integer tables (lf9veb) --------------------------


def _real_integer_tables(definition, reader):
    """First identity-scaled, integer-dtype 2D and 3D table in lf9veb."""
    found = {}
    for table in definition.tables:
        if table.type not in (TableType.TWO_D, TableType.THREE_D):
            continue
        if table.type in found:
            continue
        scaling = definition.get_scaling(table.scaling)
        if scaling is None or scaling.toexpr.strip() != "x":
            continue
        try:
            data = reader.read_table_data(table)
        except Exception:
            continue
        if data is None or np.size(data["values"]) < 4:
            continue
        if not np.issubdtype(np.asarray(data["values"]).dtype, np.integer):
            continue
        if len(np.unique(data["values"])) < 2:
            continue
        found[table.type] = table
    return list(found.values())


BULK_OPS = {
    "add_0.6": lambda v: v + 0.6,
    "mul_1.013": lambda v: v * 1.013,
    "mul_0.5": lambda v: v * 0.5,
    "smooth": None,
}


@pytest.mark.parametrize("op_name", list(BULK_OPS))
def test_bulk_ops_on_real_integer_tables_keep_memory_with_rom(
    tmp_path, sample_rom_path, sample_xml_path, op_name
):
    """Integer arrays truncate (7.6 -> 7) while the ROM writer rounds (-> 8).

    Bulk add/multiply/smooth now store the rounded value, so memory, screen
    and ROM agree and undo puts back the exact original bytes.
    """
    rom_path = tmp_path / "lf9veb.bin"
    shutil.copy(sample_rom_path, rom_path)
    definition = load_definition(str(sample_xml_path))
    probe_reader = RomReader(str(rom_path), definition)
    tables = _real_integer_tables(definition, probe_reader)
    assert {t.type for t in tables} == {TableType.TWO_D, TableType.THREE_D}

    for table in tables:
        s = _viewer_for(definition, table, RomReader(str(rom_path), definition))
        try:
            tw = s.viewer.table_widget
            if op_name == "smooth":
                _select_all(s.viewer)
                s.viewer._ops.smooth_selection()
                cells, axes = s.captured["cells"], s.captured["axes"]
            else:
                # Data cells only: axis scalings are not what this covers.
                top = 1 if table.type == TableType.THREE_D else 0
                _select(s.viewer, top, 1, tw.rowCount() - 1, tw.columnCount() - 1)
                cells, axes = s.viewer._ops.apply_bulk_operation(
                    BULK_OPS[op_name], op_name
                )
            assert cells, f"{op_name} changed nothing in {table.name}"
            for _r, _c, _ov, new_value, _or, new_raw in cells:
                assert new_value == round(new_value)
                assert new_raw == new_value  # identity scaling

            _apply_forward(s, cells, axes)
            _assert_screen_memory_rom_agree(s)
            _undo(s, cells, axes)
            assert bytes(s.reader.rom_data) == s.original
        finally:
            s.viewer.deleteLater()
