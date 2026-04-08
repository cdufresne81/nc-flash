"""
Tests for TableViewerWindow logic.

Tests _get_selected_data_cells coordinate extraction and
signal forwarding (cell_changed, bulk_changes, axis_changed).
Requires QApplication for widget instantiation.
"""

import pytest
from unittest.mock import patch, MagicMock

import numpy as np
from PySide6.QtWidgets import QApplication, QTableWidgetItem
from PySide6.QtCore import Qt

from src.core.rom_definition import (
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
    AxisType,
)
from src.ui.table_viewer_window import TableViewerWindow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    """Create QApplication for Qt widget tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_romid():
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


def _make_scaling():
    return Scaling(
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


def _make_definition(scaling=None):
    return RomDefinition(
        romid=_make_romid(),
        scalings={"TestScaling": scaling or _make_scaling()},
    )


def _make_2d_table():
    y_axis = Table(
        name="Y Axis",
        address="0x200",
        type=TableType.TWO_D,
        elements=3,
        scaling="TestScaling",
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name="Test Table",
        address="0x100",
        type=TableType.TWO_D,
        elements=3,
        scaling="TestScaling",
        children=[y_axis],
    )
    return table


def _make_3d_table():
    x_axis = Table(
        name="X Axis",
        address="0x300",
        type=TableType.THREE_D,
        elements=3,
        scaling="TestScaling",
        axis_type=AxisType.X_AXIS,
    )
    y_axis = Table(
        name="Y Axis",
        address="0x400",
        type=TableType.THREE_D,
        elements=2,
        scaling="TestScaling",
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name="Test 3D Table",
        address="0x500",
        type=TableType.THREE_D,
        elements=6,
        scaling="TestScaling",
        children=[x_axis, y_axis],
    )
    return table


def _make_1d_table():
    return Table(
        name="Test 1D",
        address="0x600",
        type=TableType.ONE_D,
        elements=1,
        scaling="TestScaling",
    )


@pytest.fixture
def mock_settings():
    """Mock get_settings to avoid QSettings side effects."""
    mock = MagicMock()
    mock.get_colormap_path.return_value = None
    mock.get_show_type_column.return_value = True
    mock.get_show_address_column.return_value = True
    mock.get_auto_round.return_value = False
    with patch("src.utils.settings.get_settings", return_value=mock):
        yield mock


@pytest.fixture
def window_2d(qapp, mock_settings):
    """Create a 2D TableViewerWindow."""
    table = _make_2d_table()
    data = {
        "values": np.array([10.0, 20.0, 30.0]),
        "y_axis": np.array([100.0, 200.0, 300.0]),
    }
    defn = _make_definition()
    win = TableViewerWindow(table, data, defn, rom_path="/tmp/test.bin")
    yield win
    win.close()


@pytest.fixture
def window_3d(qapp, mock_settings):
    """Create a 3D TableViewerWindow."""
    table = _make_3d_table()
    data = {
        "values": np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
        "x_axis": np.array([10.0, 20.0, 30.0]),
        "y_axis": np.array([100.0, 200.0]),
    }
    defn = _make_definition()
    win = TableViewerWindow(table, data, defn, rom_path="/tmp/test.bin")
    yield win
    win.close()


@pytest.fixture
def window_1d(qapp, mock_settings):
    """Create a 1D TableViewerWindow."""
    table = _make_1d_table()
    data = {"values": np.array([42.0])}
    defn = _make_definition()
    win = TableViewerWindow(table, data, defn, rom_path="/tmp/test.bin")
    yield win
    win.close()


# ---------------------------------------------------------------------------
# Tests: _get_selected_data_cells
# ---------------------------------------------------------------------------


class TestGetSelectedDataCells:
    def test_no_selection_returns_empty(self, window_2d):
        window_2d.viewer.table_widget.clearSelection()
        result = window_2d._get_selected_data_cells()
        assert result == []

    def test_single_data_cell_selected(self, window_3d):
        """Select a single data cell and verify coordinates are returned."""
        tw = window_3d.viewer.table_widget
        tw.clearSelection()

        # Find a cell with data coordinates (not axis)
        found = False
        for row in range(tw.rowCount()):
            for col in range(tw.columnCount()):
                item = tw.item(row, col)
                if item and item.data(Qt.UserRole) is not None:
                    coords = item.data(Qt.UserRole)
                    if not isinstance(coords[0], str):
                        tw.setCurrentCell(row, col)
                        result = window_3d._get_selected_data_cells()
                        assert len(result) == 1
                        assert isinstance(result[0], tuple)
                        assert len(result[0]) == 2
                        found = True
                        break
            if found:
                break
        assert found, "No data cells found in 3D table"

    def test_axis_cells_excluded(self, window_3d):
        """Axis cells (UserRole starts with string) should not be returned."""
        tw = window_3d.viewer.table_widget
        tw.clearSelection()

        # Find an axis cell and select it
        for row in range(tw.rowCount()):
            for col in range(tw.columnCount()):
                item = tw.item(row, col)
                if item and item.data(Qt.UserRole) is not None:
                    coords = item.data(Qt.UserRole)
                    if isinstance(coords[0], str):
                        tw.setCurrentCell(row, col)
                        result = window_3d._get_selected_data_cells()
                        # Axis cells should be excluded
                        assert len(result) == 0
                        return
        # If no axis cell found in this layout, that's OK — skip test
        pytest.skip("No axis cells found in table layout")


# ---------------------------------------------------------------------------
# Tests: Signal forwarding
# ---------------------------------------------------------------------------


class TestSignalForwarding:
    """Verify viewer signals emit Table object directly (no window forwarding hop)."""

    def test_cell_changed_includes_table_object(self, window_2d):
        """viewer.cell_changed signal should include the Table object."""
        received = []
        window_2d.viewer.cell_changed.connect(
            lambda table, *args: received.append((table, args))
        )

        # Emit directly from viewer (simulates editing.py behavior)
        window_2d.viewer.cell_changed.emit(
            window_2d.table, 0, 0, 10.0, 20.0, 10.0, 20.0
        )
        assert len(received) == 1
        table_obj, args = received[0]
        assert table_obj is window_2d.table
        assert args == (0, 0, 10.0, 20.0, 10.0, 20.0)

    def test_bulk_changes_includes_table_object(self, window_2d):
        received = []
        window_2d.viewer.bulk_changes.connect(
            lambda table, changes: received.append((table, changes))
        )

        changes = [(0, 0, 10.0, 20.0, 10.0, 20.0)]
        window_2d.viewer.bulk_changes.emit(window_2d.table, changes)
        assert len(received) == 1
        assert received[0][0] is window_2d.table
        assert len(received[0][1]) == 1
        assert list(received[0][1][0]) == list(changes[0])

    def test_axis_changed_includes_table_object(self, window_2d):
        received = []
        window_2d.viewer.axis_changed.connect(
            lambda table, *args: received.append((table, args))
        )

        window_2d.viewer.axis_changed.emit(
            window_2d.table, "y_axis", 0, 100.0, 150.0, 100.0, 150.0
        )
        assert len(received) == 1
        table_obj, args = received[0]
        assert table_obj is window_2d.table
        assert args == ("y_axis", 0, 100.0, 150.0, 100.0, 150.0)

    def test_axis_bulk_changes_includes_table_object(self, window_2d):
        received = []
        window_2d.viewer.axis_bulk_changes.connect(
            lambda table, changes: received.append((table, changes))
        )

        changes = [("y_axis", 0, 100.0, 150.0, 100.0, 150.0)]
        window_2d.viewer.axis_bulk_changes.emit(window_2d.table, changes)
        assert len(received) == 1
        assert received[0][0] is window_2d.table
        assert len(received[0][1]) == 1
        assert list(received[0][1][0]) == list(changes[0])


# ---------------------------------------------------------------------------
# Tests: Window properties
# ---------------------------------------------------------------------------


class TestWindowProperties:
    def test_window_title_contains_table_name(self, window_2d):
        assert "Test Table" in window_2d.windowTitle()

    def test_window_title_contains_address(self, window_2d):
        assert "0x100" in window_2d.windowTitle()

    def test_diff_mode_title(self, qapp, mock_settings):
        """Diff mode window title should include '(Changes)'."""
        table = _make_2d_table()
        data = {
            "values": np.array([10.0, 20.0, 30.0]),
            "y_axis": np.array([100.0, 200.0, 300.0]),
        }
        defn = _make_definition()
        win = TableViewerWindow(
            table, data, defn, rom_path="/tmp/test.bin", diff_mode=True
        )
        assert "(Changes)" in win.windowTitle()
        win.close()

    def test_1d_table_has_no_graph_widget(self, window_1d):
        assert window_1d.graph_widget is None

    def test_3d_table_has_graph_widget(self, window_3d):
        assert window_3d.graph_widget is not None


# ---------------------------------------------------------------------------
# Tests: Clipboard paste ignores XML scaling min/max clamp
# ---------------------------------------------------------------------------


def _first_data_coords(tw):
    """Select the first non-axis cell and return its (data_row, data_col)."""
    for row in range(tw.rowCount()):
        for col in range(tw.columnCount()):
            item = tw.item(row, col)
            if item is None:
                continue
            coords = item.data(Qt.UserRole)
            if coords is not None and not isinstance(coords[0], str):
                tw.clearSelection()
                tw.setCurrentCell(row, col)
                return coords
    raise AssertionError("no data cell found")


@pytest.mark.parametrize(
    "scaling_max,pasted,expected",
    [
        # VCT Target → [Flex] VCT Target: source held 35, scaling max=25.
        (25.0, "35", 35.0),
        # Speed Density - Volumetric Efficiency: placeholder min=0/max=0 scaling
        # disabled paste entirely for any non-zero value.
        (0.0, "42", 42.0),
    ],
    ids=["above_xml_max", "placeholder_zero_max"],
)
def test_paste_ignores_scaling_min_max_clamp(
    qapp, mock_settings, scaling_max, pasted, expected
):
    """
    Regression: paste_selection used to silently skip cells whose value fell
    outside the XML-declared scaling min/max. display_to_raw is the real
    safety net — see clipboard.py::paste_selection.
    """
    scaling = Scaling(
        name="TestScaling",
        units="",
        toexpr="x",
        frexpr="x",
        format="%0.0f",
        min=0.0,
        max=scaling_max,
        inc=1.0,
        storagetype="float",
        endian="big",
    )
    table = _make_3d_table()
    data = {
        "values": np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
        "x_axis": np.array([10.0, 20.0, 30.0]),
        "y_axis": np.array([100.0, 200.0]),
    }
    win = TableViewerWindow(
        table, data, _make_definition(scaling), rom_path="/tmp/test.bin"
    )
    try:
        dr, dc = _first_data_coords(win.viewer.table_widget)
        QApplication.clipboard().setText(pasted)

        win.viewer.paste_selection()

        assert win.viewer.current_data["values"][dr, dc] == pytest.approx(expected)
    finally:
        win.close()
