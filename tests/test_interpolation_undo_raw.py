"""Regression for #145: undoing an interpolation must restore the original ROM bytes.

Interpolation used to record each cell's DISPLAY value as its "old raw" value.
Undo and the bulk-write rollback write old_raw straight to the ROM, so on a
non-identity scaling (e.g. lambda ``100/(100+x)``) the bytes came back wrong
while the screen still showed the original value.

These tests drive the real TableViewer interpolation on a real RomReader,
replay the emitted changes through the real undo stack (wired the way
MainWindow wires it) and through MainWindow's bulk-write rollback, and compare
the ROM bytes with the originals.
"""

import struct
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableWidgetSelectionRange

from main import MainWindow
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
from src.ui.table_viewer import TableViewer
from src.ui.table_viewer_helpers import interpolation as interp_module

_app = QApplication.instance() or QApplication([])

X_ADDR, Y_ADDR, DATA_ADDR = 0x100, 0x200, 0x300
N = 5  # 5x5 table

# Raw values chosen so every interior cell differs from its interpolation.
X_RAW = [100.0, 130.0, 500.0, 610.0, 1000.0]
Y_RAW = [0.0, 7.0, 40.0, 41.0, 80.0]
DATA_RAW = [
    [10.0, 3.0, 25.0, 60.0, 0.0],
    [37.0, 90.0, 1.0, 12.0, 44.0],
    [5.0, 70.0, 33.0, 8.0, 21.0],
    [64.0, 2.0, 99.0, 17.0, 50.0],
    [20.0, 45.0, 6.0, 81.0, 30.0],
]


def _definition(data_storage):
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
    scalings = {
        # Same expressions as the bundled lambda scalings.
        "Lambda": Scaling(
            name="Lambda",
            units="",
            toexpr="100/(100+x)",
            frexpr="(100/x) - 100",
            format="%0.3f",
            min=0.5,
            max=1.0,
            inc=0.01,
            storagetype=data_storage,
            endian="big",
        ),
        "AxisScale": Scaling(
            name="AxisScale",
            units="",
            toexpr="x/1.0189",
            frexpr="x*1.0189",
            format="%0.2f",
            min=0.0,
            max=10000.0,
            inc=1.0,
            storagetype="float",
            endian="big",
        ),
    }
    x_axis = Table(
        name="X",
        address=hex(X_ADDR),
        elements=N,
        scaling="AxisScale",
        type=TableType.ONE_D,
        axis_type=AxisType.X_AXIS,
    )
    y_axis = Table(
        name="Y",
        address=hex(Y_ADDR),
        elements=N,
        scaling="AxisScale",
        type=TableType.ONE_D,
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name="Lambda Target",
        address=hex(DATA_ADDR),
        elements=N * N,
        scaling="Lambda",
        type=TableType.THREE_D,
        children=[x_axis, y_axis],
    )
    definition = RomDefinition(romid=romid, scalings=scalings, tables=[table])
    return definition, table


def _write_rom(path, data_storage):
    rom = bytearray(0x400)
    for i, v in enumerate(X_RAW):
        rom[X_ADDR + 4 * i : X_ADDR + 4 * i + 4] = struct.pack(">f", v)
    for i, v in enumerate(Y_RAW):
        rom[Y_ADDR + 4 * i : Y_ADDR + 4 * i + 4] = struct.pack(">f", v)
    flat = [v for row in DATA_RAW for v in row]
    for i, v in enumerate(flat):
        if data_storage == "float":
            rom[DATA_ADDR + 4 * i : DATA_ADDR + 4 * i + 4] = struct.pack(">f", v)
        else:
            rom[DATA_ADDR + i] = int(v)
    path.write_bytes(bytes(rom))


@pytest.fixture(autouse=True)
def _no_auto_round(monkeypatch):
    monkeypatch.setattr(
        interp_module,
        "get_settings",
        lambda: SimpleNamespace(get_auto_round=lambda: False),
    )


@pytest.fixture(params=["float", "uint8"])
def setup(request, tmp_path):
    storage = request.param
    definition, table = _definition(storage)
    rom_path = tmp_path / "rom.bin"
    _write_rom(rom_path, storage)
    reader = RomReader(str(rom_path), definition)
    original = bytes(reader.rom_data)

    viewer = TableViewer(definition)
    viewer.display_table(table, reader.read_table_data(table))

    captured = {"cells": [], "axes": []}
    viewer.bulk_changes.connect(lambda _t, ch: captured["cells"].extend(ch))
    viewer.axis_bulk_changes.connect(lambda _t, ch: captured["axes"].extend(ch))

    yield SimpleNamespace(
        reader=reader,
        table=table,
        viewer=viewer,
        original=original,
        captured=captured,
    )
    viewer.deleteLater()


def _ui_cells(viewer):
    """Map UserRole coords -> (ui_row, ui_col) for every data/axis cell."""
    tw = viewer.table_widget
    found = {}
    for r in range(tw.rowCount()):
        for c in range(tw.columnCount()):
            item = tw.item(r, c)
            if item is not None and item.data(Qt.UserRole) is not None:
                found[tuple(item.data(Qt.UserRole))] = (r, c)
    return found


def _select(viewer, top, left, bottom, right):
    tw = viewer.table_widget
    tw.clearSelection()
    tw.setRangeSelected(QTableWidgetSelectionRange(top, left, bottom, right), True)


def _apply_forward(reader, table, cells, axes):
    """What MainWindow does on bulk_changes / axis_bulk_changes."""
    for row, col, _ov, _nv, _or, new_raw in cells:
        reader.write_cell_value(table, row, col, new_raw)
    for axis_type, idx, _ov, _nv, _or, new_raw in axes:
        reader.write_axis_value(table, axis_type, idx, new_raw)


def _undo_through_stack(reader, table, cells, axes):
    """Record the changes on a real undo stack wired like MainWindow, then undo."""
    manager = TableUndoManager()
    manager.set_callbacks(
        apply_cell=lambda ch: reader.write_cell_value(
            table, ch.row, ch.col, ch.new_raw
        ),
        apply_axis=lambda ch: reader.write_axis_value(
            table, ch.axis_type, ch.index, ch.new_raw
        ),
        update_pending=lambda ch, is_undo: None,
        update_pending_axis=lambda ch, is_undo: None,
        begin_bulk_update=lambda *a: None,
        end_bulk_update=lambda *a: None,
    )
    key = make_table_key(None, table.address)
    if cells:
        manager.record_bulk_cell_changes(table, cells, "Interpolate")
    if axes:
        manager.record_axis_bulk_changes(table, axes, "Interpolate axis")
    manager.set_active_stack(key)
    stack = manager.get_or_create_stack(key)
    while stack.canUndo():
        stack.undo()


def _assert_changed_then_restored(s, undo):
    assert s.captured["cells"] or s.captured["axes"], "interpolation made no change"
    _apply_forward(s.reader, s.table, s.captured["cells"], s.captured["axes"])
    assert bytes(s.reader.rom_data) != s.original, "forward write changed nothing"
    undo(s.reader, s.table, s.captured["cells"], s.captured["axes"])
    assert bytes(s.reader.rom_data) == s.original


def test_vertical_interpolation_undo_restores_rom_bytes(setup):
    cells = _ui_cells(setup.viewer)
    top, col = cells[(0, 2)]
    bottom, _ = cells[(N - 1, 2)]
    _select(setup.viewer, top, col, bottom, col)
    setup.viewer._interp.interpolate_vertical()
    assert setup.captured["cells"]
    _assert_changed_then_restored(setup, _undo_through_stack)


def test_horizontal_interpolation_undo_restores_rom_bytes(setup):
    cells = _ui_cells(setup.viewer)
    row, left = cells[(1, 0)]
    _, right = cells[(1, N - 1)]
    _select(setup.viewer, row, left, row, right)
    setup.viewer._interp.interpolate_horizontal()
    assert setup.captured["cells"]
    _assert_changed_then_restored(setup, _undo_through_stack)


def test_2d_interpolation_undo_restores_rom_bytes(setup):
    cells = _ui_cells(setup.viewer)
    top, left = cells[(0, 0)]
    bottom, right = cells[(N - 1, N - 1)]
    _select(setup.viewer, top, left, bottom, right)
    setup.viewer._interp.interpolate_2d()
    assert len(setup.captured["cells"]) > 1
    _assert_changed_then_restored(setup, _undo_through_stack)


@pytest.mark.parametrize("axis_key", ["x_axis", "y_axis"])
def test_axis_interpolation_undo_restores_rom_bytes(setup, axis_key):
    cells = _ui_cells(setup.viewer)
    first = cells[(axis_key, 0)]
    last = cells[(axis_key, N - 1)]
    _select(setup.viewer, first[0], first[1], last[0], last[1])
    if first[1] == last[1]:  # axis laid out down a column
        setup.viewer._interp.interpolate_vertical()
    else:  # axis laid out along a row
        setup.viewer._interp.interpolate_horizontal()
    assert setup.captured["axes"]
    assert not setup.captured["cells"]
    _assert_changed_then_restored(setup, _undo_through_stack)


def _rollback(reader, table, cells, axes):
    """MainWindow's rollback after a failed bulk write."""
    document = SimpleNamespace(rom_reader=reader)
    MainWindow._rollback_failed_cell_writes(None, document, table, cells)
    MainWindow._rollback_failed_axis_writes(None, document, table, axes)


def test_bulk_write_rollback_restores_rom_bytes(setup):
    cells = _ui_cells(setup.viewer)
    top, left = cells[(0, 0)]
    bottom, right = cells[(N - 1, N - 1)]
    _select(setup.viewer, top, left, bottom, right)
    setup.viewer._interp.interpolate_2d()
    _assert_changed_then_restored(setup, _rollback)


def test_axis_bulk_write_rollback_restores_rom_bytes(setup):
    cells = _ui_cells(setup.viewer)
    first = cells[("x_axis", 0)]
    last = cells[("x_axis", N - 1)]
    _select(setup.viewer, first[0], first[1], last[0], last[1])
    if first[1] == last[1]:
        setup.viewer._interp.interpolate_vertical()
    else:
        setup.viewer._interp.interpolate_horizontal()
    _assert_changed_then_restored(setup, _rollback)


def test_recorded_old_raw_is_raw_not_display(setup):
    """The old raw value in each change is the ROM's raw value, not the display."""
    cells = _ui_cells(setup.viewer)
    top, left = cells[(0, 0)]
    bottom, right = cells[(N - 1, N - 1)]
    _select(setup.viewer, top, left, bottom, right)
    setup.viewer._interp.interpolate_2d()
    for row, col, old_value, _nv, old_raw, _nr in setup.captured["cells"]:
        assert old_raw == pytest.approx(DATA_RAW[row][col], abs=1e-6)
        assert old_value == pytest.approx(100.0 / (100.0 + DATA_RAW[row][col]))
    assert np.isfinite([c[4] for c in setup.captured["cells"]]).all()
