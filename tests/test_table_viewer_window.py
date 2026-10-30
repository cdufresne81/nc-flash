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
from PySide6.QtGui import QRegion

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

    def test_toggle_emits_table_object_not_address(self, window_1d):
        """Regression (DTC toggle switch): _on_toggle_changed must emit the
        Table object, not table.address. Consumers (_on_table_cell_changed,
        the modification tracker) call table.address themselves; a str arg
        raised AttributeError on every DTC-flag toggle."""
        received = []
        window_1d.viewer.cell_changed.connect(
            lambda table, *args: received.append(table)
        )

        # Drive the real toggle handler (OFF->ON), not a manual emit.
        window_1d.viewer._on_toggle_changed(True)

        assert len(received) == 1
        assert received[0] is window_1d.table
        assert not isinstance(received[0], str)


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


# ---------------------------------------------------------------------------
# Screenshot → clipboard first, then optionally save (#104)
# ---------------------------------------------------------------------------


class TestScreenshotToClipboard:
    def test_copies_to_clipboard_without_saving(self, window_2d):
        QApplication.clipboard().clear()
        with (
            patch.object(window_2d, "_ask_save_screenshot", return_value=False),
            patch("src.ui.table_viewer_window.QFileDialog") as dlg,
        ):
            window_2d._take_screenshot()
        assert not QApplication.clipboard().pixmap().isNull()
        dlg.getSaveFileName.assert_not_called()

    def test_cancelled_save_keeps_clipboard(self, window_2d):
        QApplication.clipboard().clear()
        with (
            patch.object(window_2d, "_ask_save_screenshot", return_value=True),
            patch("src.ui.table_viewer_window.QFileDialog") as dlg,
        ):
            dlg.getSaveFileName.return_value = ("", "")
            window_2d._take_screenshot()
        dlg.getSaveFileName.assert_called_once()
        assert not QApplication.clipboard().pixmap().isNull()

    def test_save_writes_the_captured_image(self, window_2d, mock_settings, tmp_path):
        mock_settings.get_screenshots_directory.return_value = str(tmp_path)
        out = tmp_path / "shot.png"
        with (
            patch.object(window_2d, "_ask_save_screenshot", return_value=True),
            patch("src.ui.table_viewer_window.QFileDialog") as dlg,
            patch.object(window_2d, "grab", wraps=window_2d.grab) as grab,
        ):
            dlg.getSaveFileName.return_value = (str(out), "")
            window_2d._take_screenshot()
        assert out.exists() and out.stat().st_size > 0
        assert grab.call_count == 1  # grabbed once, before any dialog
        assert not QApplication.clipboard().pixmap().isNull()


# --- live table cursor ---------------------------------------------------------


def _live_3d_table(flipx=False, flipy=False):
    table = _make_3d_table()
    table.flipx, table.flipy = flipx, flipy
    table.x_axis.logparam = "LOAD"
    table.y_axis.logparam = "RPM"
    return table


def _ui_cell(win, data_row, data_col):
    """UI (row, col) showing a data cell (follows the display flips)."""
    tw = win.viewer.table_widget
    for r in range(tw.rowCount()):
        for c in range(tw.columnCount()):
            item = tw.item(r, c)
            coords = item.data(Qt.UserRole) if item is not None else None
            # PySide6 may hand a stored tuple back as a list
            if coords is not None and tuple(coords) == (data_row, data_col):
                return r, c
    raise AssertionError(f"no UI cell for data {(data_row, data_col)}")


def _center(win, ui_row, ui_col):
    tw = win.viewer.table_widget
    return (
        tw.columnViewportPosition(ui_col) + tw.columnWidth(ui_col) / 2,
        tw.rowViewportPosition(ui_row) + tw.rowHeight(ui_row) / 2,
    )


@pytest.fixture
def live_window(qapp, mock_settings, request):
    flipx, flipy = getattr(request, "param", (False, False))
    data = {
        "values": np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
        "x_axis": np.array([10.0, 20.0, 30.0]),  # LOAD
        "y_axis": np.array([100.0, 200.0]),  # RPM
    }
    win = TableViewerWindow(
        _live_3d_table(flipx, flipy), data, _make_definition(), rom_path="/t.bin"
    )
    yield win
    win.close()


@pytest.mark.parametrize(
    "value, text",
    [(3250.4, "3250"), (-40.0, "-40"), (55.25, "55.2"), (0.47123, "0.471"), (0.0, "0")],
)
def test_live_tags_show_readable_numbers(value, text):
    from src.ui.table_viewer_window import _tag_number

    assert _tag_number(value) == text


class TestLiveCursor:
    def test_crosshair_goes_through_the_exact_point(self, live_window):
        assert live_window.has_live_cursor_axes
        live_window.set_live_values({"RPM": 100.0, "LOAD": 25.0})
        assert live_window.viewer.has_live_cursor()
        geo = live_window.viewer.live_cursor.geometry()
        x1, y = _center(live_window, *_ui_cell(live_window, 0, 1))
        x2, _ = _center(live_window, *_ui_cell(live_window, 0, 2))
        assert (geo["x"], geo["y"]) == pytest.approx(((x1 + x2) / 2, y))
        assert live_window.viewer.live_cursor.tag_texts() == ["100", "25"]

    @pytest.mark.parametrize(
        "live_window", [(True, False), (False, True), (True, True)], indirect=True
    )
    def test_flipped_display_points_at_the_same_data_cell(self, live_window):
        live_window.set_live_values({"RPM": 200.0, "LOAD": 30.0})
        geo = live_window.viewer.live_cursor.geometry()
        ui = _ui_cell(live_window, 1, 2)
        assert (geo["x"], geo["y"]) == pytest.approx(_center(live_window, *ui))
        assert geo["peak"].center().x() == pytest.approx(geo["x"])
        assert geo["peak"].center().y() == pytest.approx(geo["y"])

    def test_off_the_table_and_approx_tags(self, live_window):
        live_window._live_maps = (
            live_window._live_maps[0].__class__("LOAD", lambda x: x, approx=True),
            live_window._live_maps[1],
        )
        live_window.set_live_values({"RPM": 999.0, "LOAD": 15.0})
        state = live_window.viewer.live_cursor.state
        assert state.y_tag == ("999", True)  # past the last RPM breakpoint
        assert state.x_tag == ("≈15", False)

    def test_missing_channel_says_so_and_draws_no_crosshair(self, live_window):
        live_window.set_live_values({"RPM": 150.0})
        assert not live_window.viewer.has_live_cursor()
        assert "needs: LOAD" in live_window.viewer.live_cursor.state.message

    def test_clearing_hides_everything(self, live_window):
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        live_window.set_live_values({})
        assert live_window.viewer.live_cursor.state is None

    def test_the_estimated_value_is_off_by_default(self, live_window):
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        assert live_window.viewer.has_live_cursor()
        assert live_window.viewer.live_cursor.value_text() == ""

    def test_turning_the_estimate_on_shows_it_at_once(self, live_window):
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        live_window.set_live_estimate(True)
        assert live_window.viewer.live_cursor.value_text() == "3.50"
        live_window.set_live_estimate(False)
        assert live_window.viewer.live_cursor.value_text() == ""

    def test_value_chip_shows_the_table_value_at_the_point(self, live_window):
        live_window.set_live_estimate(True)
        # RPM 150 is halfway between rows 0 and 1, LOAD 20 is on column 1:
        # (2 + 5) / 2, formatted like the cells (%0.2f)
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        assert live_window.viewer.live_cursor.state.value_tag == "3.50"
        assert live_window.viewer.live_cursor.value_text() == "3.50"

    def test_value_chip_is_approximate_when_an_axis_is(self, live_window):
        live_window.set_live_estimate(True)
        live_window._live_maps = (
            live_window._live_maps[0].__class__("LOAD", lambda x: x, approx=True),
            live_window._live_maps[1],
        )
        live_window.set_live_values({"RPM": 100.0, "LOAD": 10.0})
        assert live_window.viewer.live_cursor.state.value_tag == "≈1.00"

    def test_no_value_chip_on_a_non_number(self, live_window):
        live_window.set_live_estimate(True)
        live_window.data["values"][0, 0] = np.nan
        live_window.set_live_values({"RPM": 100.0, "LOAD": 10.0})
        assert live_window.viewer.has_live_cursor()
        assert live_window.viewer.live_cursor.value_text() == ""

    @pytest.mark.parametrize(
        "rpm, load", [(100.0, 10.0), (100.0, 30.0), (200.0, 10.0), (200.0, 30.0)]
    )
    def test_value_chip_stays_off_the_axis_headers(self, live_window, qtbot, rpm, load):
        live_window.set_live_estimate(True)
        live_window.show()
        qtbot.waitExposed(live_window)
        live_window.set_live_values({"RPM": rpm, "LOAD": load})
        tw = live_window.viewer.table_widget
        chip = live_window.viewer.live_cursor.geometry()["value"][0]
        assert chip.left() >= tw.columnWidth(0)  # clear of the Y-axis column
        assert chip.top() >= tw.rowHeight(0)  # clear of the X-axis row
        assert chip.right() <= tw.viewport().width()

    def test_a_new_value_on_the_same_spot_repaints_the_chip(self, live_window):
        live_window.set_live_estimate(True)
        viewport = live_window.viewer.table_widget.viewport()
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        live_window.data["values"][0, 1] = 9.0  # e.g. the cell was edited
        with patch.object(viewport, "update") as update:
            live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0001})
            update.assert_called_once()
        assert live_window.viewer.live_cursor.value_text() == "7.00"

    def test_the_nearest_cell_number_is_redrawn_over_the_cursor(self, live_window):
        live_window.set_live_values({"RPM": 200.0, "LOAD": 20.0})
        overlay = live_window.viewer.live_cursor
        geo = overlay.geometry()
        tw = live_window.viewer.table_widget
        item = tw.item(*geo["peak_ui"])
        painter = MagicMock()
        overlay._paint_peak_text(painter, geo["peak_ui"])
        rect, align, _, text, _, _ = tw.cell_text_layout(tw.indexFromItem(item))
        assert text == item.text()
        assert painter.drawText.call_args_list[-1].args == (rect, align, text)

    def test_repaints_only_when_the_point_moves(self, live_window):
        viewport = live_window.viewer.table_widget.viewport()
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        with patch.object(viewport, "update") as update:
            live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
            update.assert_not_called()
            live_window.set_live_values({"RPM": 160.0, "LOAD": 20.0})
            update.assert_called_once()

    def test_live_values_never_change_the_window_layout(self, live_window, qtbot):
        live_window.show()
        qtbot.waitExposed(live_window)
        qtbot.wait(200)  # let the window settle (offscreen platforms resize late)
        before = (live_window.size(), live_window.viewer.table_widget.size())
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        live_window.set_live_values({"RPM": 999.0})  # the "needs" hint
        qtbot.wait(50)
        assert (live_window.size(), live_window.viewer.table_widget.size()) == before

    def test_the_dot_is_painted_over_the_cells(self, live_window, qtbot):
        from src.ui import theme
        from PySide6.QtGui import QColor

        live_window.show()
        qtbot.waitExposed(live_window)
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        geo = live_window.viewer.live_cursor.geometry()
        image = live_window.viewer.table_widget.viewport().grab().toImage()
        ratio = image.devicePixelRatio()
        pixel = image.pixelColor(int(geo["x"] * ratio), int(geo["y"] * ratio))
        assert pixel.name() == QColor(theme.LIVE_CURSOR).name()

    def test_a_move_nobody_can_see_repaints_nothing(self, live_window):
        live_window.set_live_estimate(True)
        viewport = live_window.viewer.table_widget.viewport()
        overlay = live_window.viewer.live_cursor
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        drawn = overlay.state
        chip = overlay.geometry()["value"][0]
        with patch.object(viewport, "update") as update:
            live_window.set_live_values({"RPM": 150.01, "LOAD": 20.001})
            update.assert_not_called()  # same tags, same value, sub-pixel move
            # The value chip follows the live value (3.50 -> 3.51); the
            # cross and the tags stay put.
            live_window.set_live_values({"RPM": 150.2, "LOAD": 20.01})
        (region,), _ = update.call_args
        assert region.boundingRect() == chip.toAlignedRect().adjusted(-1, -1, 1, 1)
        assert (overlay.state.fr, overlay.state.fc) == (drawn.fr, drawn.fc)

    def test_only_a_changed_tag_is_repainted(self, live_window):
        live_window.set_live_estimate(True)
        viewport = live_window.viewer.table_widget.viewport()
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        geo = live_window.viewer.live_cursor.geometry()
        tag_rect, chip = geo["tags"][0][0], geo["value"][0]
        with patch.object(viewport, "update") as update:
            live_window.set_live_values({"RPM": 151.0, "LOAD": 20.0})
        (region,), _ = update.call_args
        # The RPM tag and the value chip (3.50 -> 3.53), not the cross
        expected = QRegion(tag_rect.toAlignedRect()).united(
            QRegion(chip.toAlignedRect().adjusted(-1, -1, 1, 1))
        )
        assert region == expected
        assert live_window.viewer.live_cursor.state.y_tag == ("151", False)

    def test_a_slow_drift_still_moves_the_cross(self, live_window):
        overlay = live_window.viewer.live_cursor
        live_window.set_live_values({"RPM": 100.0, "LOAD": 20.0})
        start_y = overlay.geometry()["y"]
        rpm = 100.0
        while rpm < 150.0:  # each step is far below a pixel
            rpm += 0.5
            live_window.set_live_values({"RPM": rpm, "LOAD": 20.0})
        y = overlay.geometry()["y"]
        assert y != start_y  # it moved once the drift added up
        rows = [_ui_cell(live_window, r, 0)[0] for r in (0, 1)]
        px_per_row = abs(
            _center(live_window, rows[1], 0)[1] - _center(live_window, rows[0], 0)[1]
        )
        true_fr = 0.5  # RPM 150 on a 100/200 axis
        assert abs(overlay.state.fr - true_fr) * px_per_row < overlay.MOVE_PX

    def test_background_window_moves_at_most_every_interval(self, live_window, qtbot):
        from src.ui import table_viewer_window as tvw

        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0}, False)
        first = live_window.viewer.live_cursor.state
        live_window.set_live_values({"RPM": 160.0, "LOAD": 20.0}, False)
        live_window.set_live_values({"RPM": 170.0, "LOAD": 20.0}, False)
        assert live_window.viewer.live_cursor.state == first  # held back
        qtbot.waitUntil(
            lambda: live_window.viewer.live_cursor.state.y_tag[0] == "170",
            timeout=int(tvw.LIVE_BACKGROUND_INTERVAL_S * 1000) + 1000,
        )  # then the latest values, not each one

    def test_hiding_is_never_delayed(self, live_window):
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0}, False)
        live_window.set_live_values({"RPM": 160.0, "LOAD": 20.0}, False)  # held
        live_window.set_live_values({}, False)
        assert live_window.viewer.live_cursor.state is None
        assert not live_window._live_timer.isActive()

    def test_values_that_dont_match_the_axes_point_at_nothing(self, live_window):
        live_window.data["values"] = np.zeros(6)  # not reshaped to 2 x 3
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        assert live_window.viewer.live_cursor.state is None

    def test_a_cursor_that_cant_be_drawn_is_hidden_not_stuck(self, live_window, qtbot):
        live_window.show()
        qtbot.waitExposed(live_window)
        overlay = live_window.viewer.live_cursor
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        with patch.object(overlay, "geometry", side_effect=ValueError("bad")):
            overlay.set_state(overlay.state.__class__(2, 3, fr=0.5, fc=0.5))
            live_window.viewer.table_widget.viewport().grab()  # forces a paint
            assert overlay.state is None  # the paint failure hid it
            live_window.set_live_values({})  # and a clear still works

    def test_minimized_window_skips_updates(self, live_window):
        live_window.isMinimized = lambda: True
        live_window.set_live_values({"RPM": 150.0, "LOAD": 20.0})
        assert live_window.viewer.live_cursor.state is None

    def test_tcm_tables_ignore_the_shared_map(self, qapp, mock_settings):
        table = _make_3d_table()
        table.x_axis.name, table.y_axis.name = "LOAD_6F44", "RPM_6DD8"
        defn = _make_definition()
        defn.romid.model = "MX5 NC TCM"
        data = {
            "values": np.zeros((2, 3)),
            "x_axis": np.array([0.2, 0.4, 0.6]),
            "y_axis": np.array([1000.0, 2000.0]),
        }
        win = TableViewerWindow(table, data, defn, rom_path="/t.bin")
        assert not win.has_live_cursor_axes
        win.close()

    def test_unmapped_tables_and_diff_windows_get_no_cursor(
        self, window_3d, qapp, mock_settings
    ):
        assert not window_3d.has_live_cursor_axes  # "X Axis"/"Y Axis": no map
        window_3d.set_live_values({"RPM": 1.0, "LOAD": 1.0})
        assert not window_3d.viewer.has_live_cursor()
        diff = TableViewerWindow(
            _live_3d_table(),
            {
                "values": np.zeros((2, 3)),
                "x_axis": np.array([1.0, 2.0, 3.0]),
                "y_axis": np.array([1.0, 2.0]),
            },
            _make_definition(),
            rom_path="/t.bin",
            diff_mode=True,
            diff_base_data={"values": np.zeros((2, 3))},
        )
        assert not diff.has_live_cursor_axes
        diff.close()


# --- cell marks (painted by the table widget, not a Python delegate) ----------


def _cells_painted(win, region):
    """Cells whose marks CellDecorations considers for a repaint of ``region``."""
    painted = []
    decorations = win.viewer.table_widget.decorations
    with patch.object(
        decorations, "_paint_cell", lambda p, tw, item, r, c: painted.append((r, c))
    ):
        decorations.paint(MagicMock(), region)
    return painted


class TestCellMarks:
    def test_the_delegate_does_not_paint(self):
        # A Python paint() override runs once per cell on every repaint
        # (~57 of ~169 us per cell); the marks are painted once per repaint.
        from src.ui.table_viewer_helpers.cell_delegate import CellEditorDelegate

        assert "paint" not in vars(CellEditorDelegate)

    def test_modified_border_is_painted(self, window_3d, qtbot):
        from PySide6.QtGui import QColor

        window_3d.show()
        qtbot.waitExposed(window_3d)
        viewer, tw = window_3d.viewer, window_3d.viewer.table_widget
        ui_row, ui_col = _ui_cell(window_3d, 1, 1)
        rect = tw.visualItemRect(tw.item(ui_row, ui_col))

        def border_pixel():
            image = tw.viewport().grab().toImage()
            ratio = image.devicePixelRatio()
            return image.pixelColor(
                int((rect.left() + 1) * ratio), int(rect.center().y() * ratio)
            ).name()

        gray = QColor(100, 100, 100).name()
        assert border_pixel() != gray
        viewer.mark_cell_modified(window_3d.table.address, 1, 1)
        assert viewer.has_modified_cells()
        assert border_pixel() == gray

    def test_a_small_repaint_looks_only_at_the_cells_it_covers(self, window_3d):
        from PySide6.QtGui import QRegion

        tw = window_3d.viewer.table_widget
        window_3d.viewer.mark_cell_modified(window_3d.table.address, 0, 0)
        ui = _ui_cell(window_3d, 1, 2)
        region = QRegion(tw.visualItemRect(tw.item(*ui)))
        assert _cells_painted(window_3d, region) == [ui]

    def test_without_marks_only_the_axis_cells_are_looked_at(self, window_3d):
        from PySide6.QtGui import QRegion

        tw = window_3d.viewer.table_widget
        tw.clearSelection()
        assert not window_3d.viewer.has_modified_cells()
        everything = QRegion(tw.viewport().rect())
        cells = _cells_painted(window_3d, everything)
        assert cells and all(r == 0 or c == 0 for r, c in cells)
        assert len(cells) == tw.rowCount() + tw.columnCount() - 1
