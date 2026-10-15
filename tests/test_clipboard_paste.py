"""
Tests for clipboard paste into axis cells and hex-formatted cells.

Two bugs:
  1. Pasting into an axis cell crashed with IndexError: the axis coords
     ('x_axis', i) were used as numpy indices.
  2. Hex-formatted cells (Tire Size Correction, '%08x') copy as hex text but
     were parsed as decimal: letters were dropped, and an all-digit value
     like '02054517' was stored as decimal 2054517 (0x001F5975).
"""

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QApplication, QTableWidgetSelectionRange

from src.core.rom_definition import (
    AxisType,
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
)
from src.core.storage_types import raw_fits_storage
from src.ui.table_viewer_helpers.clipboard import CELLS_MIME
from src.ui.table_viewer_window import TableViewerWindow
from src.utils.formatting import is_hex_format, parse_cell_text

# ---------------------------------------------------------------------------
# parse_cell_text
# ---------------------------------------------------------------------------


class TestParseCellText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("02054517", 0x02054517),
            ("0205451a", 0x0205451A),
            ("FFFFFFFF", 0xFFFFFFFF),
            ("0x2054517", 0x2054517),
            ("  205451a ", 0x205451A),
        ],
    )
    def test_hex_format_parses_hex(self, text, expected):
        assert parse_cell_text(text, "8x") == expected

    @pytest.mark.parametrize("text", ["", "zz", "-1", "1.5", "nan", "0x", None])
    def test_hex_format_rejects_non_hex(self, text):
        assert parse_cell_text(text, "8x") is None

    def test_decimal_format_unchanged(self):
        assert parse_cell_text("10", ".2f") == 10.0
        assert parse_cell_text("1,5", ".2f") == 1.5
        assert parse_cell_text("1a", ".2f") is None

    @pytest.mark.parametrize(
        "spec,expected", [("8x", True), ("X", True), (".2f", False), ("d", False)]
    )
    def test_is_hex_format(self, spec, expected):
        assert is_hex_format(spec) is expected


class TestRawFitsStorage:
    @pytest.mark.parametrize(
        "raw,storage,expected",
        [
            (255, "uint8", True),
            (255.4, "uint8", True),  # writer rounds to 255
            (256, "uint8", False),
            (-1, "uint8", False),
            (-128, "int8", True),
            (0xFFFFFFFF, "uint32", True),
            (0x1FFFFFFFF, "UINT32", False),
            (1e39, "float", False),
            (1e39, "double", True),
            (float("nan"), "float", False),
        ],
    )
    def test_bounds(self, raw, storage, expected):
        assert raw_fits_storage(raw, storage) is expected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _scaling(name, fmt, storagetype="float"):
    return Scaling(
        name=name,
        units="",
        toexpr="x",
        frexpr="x",
        format=fmt,
        min=0.0,
        max=0.0,
        inc=1.0,
        storagetype=storagetype,
        endian="big",
    )


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
    return RomDefinition(
        romid=romid,
        scalings={
            "Val": _scaling("Val", "%0.2f"),
            "Hex": _scaling("Hex", "%08x", storagetype="uint32"),
            "U8": _scaling("U8", "%d", storagetype="uint8"),
        },
    )


@pytest.fixture
def mock_settings():
    mock = MagicMock()
    mock.get_colormap_path.return_value = None
    mock.get_show_type_column.return_value = True
    mock.get_show_address_column.return_value = True
    mock.get_auto_round.return_value = False
    mock.get_toggle_categories.return_value = []
    with patch("src.utils.settings.get_settings", return_value=mock):
        yield mock


def _make_3d_window(x_axis, y_axis, values, x_scaling="Val"):
    x = Table(
        name="X",
        address="0x300",
        type=TableType.THREE_D,
        elements=len(x_axis),
        scaling=x_scaling,
        axis_type=AxisType.X_AXIS,
    )
    y = Table(
        name="Y",
        address="0x200",
        type=TableType.THREE_D,
        elements=len(y_axis),
        scaling="Val",
        axis_type=AxisType.Y_AXIS,
    )
    table = Table(
        name="Load Scaling",
        address="0x100",
        type=TableType.THREE_D,
        elements=values.size,
        scaling="Val",
        children=[x, y],
    )
    data = {
        "values": values.astype(float),
        "x_axis": np.array(x_axis, dtype=float),
        "y_axis": np.array(y_axis, dtype=float),
    }
    return TableViewerWindow(table, data, _make_definition(), rom_path="/tmp/t.bin")


@pytest.fixture
def window_3d(qapp, mock_settings):
    """3x3 table. UI row 0 = X axis (cols 1..3), UI col 0 = Y axis (rows 1..3)."""
    win = _make_3d_window(
        [1.0, 2.0, 3.0], [10.0, 20.0, 30.0], np.arange(9.0).reshape(3, 3)
    )
    yield win
    win.close()


@pytest.fixture
def window_hex(qapp, mock_settings):
    """1D uint32 table displayed as '%08x', like Tire Size Correction."""
    table = Table(
        name="Tire Size Correction [hex]",
        address="0xf3e84",
        type=TableType.ONE_D,
        elements=1,
        scaling="Hex",
    )
    data = {"values": np.array([0x0205451A], dtype=np.int64)}
    win = TableViewerWindow(table, data, _make_definition(), rom_path="/tmp/t.bin")
    yield win
    win.close()


def _select(viewer, top, left, bottom, right):
    tw = viewer.table_widget
    tw.clearSelection()
    tw.setRangeSelected(QTableWidgetSelectionRange(top, left, bottom, right), True)


def _paste(viewer, text):
    QApplication.clipboard().setText(text)
    viewer.paste_selection()


# ---------------------------------------------------------------------------
# Axis paste
# ---------------------------------------------------------------------------


class TestPasteIntoAxis:
    def test_paste_x_axis_row(self, window_3d):
        viewer = window_3d.viewer
        axis_signals, data_signals = [], []
        viewer.axis_bulk_changes.connect(lambda t, c: axis_signals.append(c))
        viewer.bulk_changes.connect(lambda t, c: data_signals.append(c))

        _select(viewer, 0, 1, 0, 3)
        _paste(viewer, "100\t200\t300")

        assert list(viewer.current_data["x_axis"]) == [100.0, 200.0, 300.0]
        assert viewer.table_widget.item(0, 1).text() == "100.00"
        assert data_signals == []
        assert axis_signals == [
            [
                ("x_axis", 0, 1.0, 100.0, 1.0, 100.0),
                ("x_axis", 1, 2.0, 200.0, 2.0, 200.0),
                ("x_axis", 2, 3.0, 300.0, 3.0, 300.0),
            ]
        ]

    def test_paste_y_axis_column(self, window_3d):
        viewer = window_3d.viewer
        _select(viewer, 1, 0, 3, 0)
        _paste(viewer, "5\n6\n7")

        assert list(viewer.current_data["y_axis"]) == [5.0, 6.0, 7.0]

    def test_paste_rejects_non_numeric_axis_text(self, window_3d):
        viewer = window_3d.viewer
        _select(viewer, 0, 1, 0, 2)
        _paste(viewer, "nan\t9")

        assert list(viewer.current_data["x_axis"]) == [1.0, 9.0, 3.0]

    def test_whole_table_copies_to_another_table(self, window_3d, mock_settings):
        """Copy axes + data from one table and paste over another."""
        src = window_3d.viewer
        tw = src.table_widget
        _select(src, 0, 0, tw.rowCount() - 1, tw.columnCount() - 1)
        src.copy_selection()

        dst_win = _make_3d_window([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], np.zeros((3, 3)))
        try:
            dst = dst_win.viewer
            _select(dst, 0, 0, 0, 0)
            dst.paste_selection()

            assert list(dst.current_data["x_axis"]) == [1.0, 2.0, 3.0]
            assert list(dst.current_data["y_axis"]) == [10.0, 20.0, 30.0]
            np.testing.assert_array_equal(
                dst.current_data["values"], np.arange(9.0).reshape(3, 3)
            )
        finally:
            dst_win.close()


# ---------------------------------------------------------------------------
# Exact values: displayed text is rounded, the ROM must not be
# ---------------------------------------------------------------------------


@pytest.fixture
def window_precise(qapp, mock_settings):
    """X axis 1.5625 / 3.125 is displayed rounded ('1.56', '3.12'/'3.13')."""
    win = _make_3d_window(
        [0.0, 1.5625, 3.125], [10.0, 20.0, 30.0], np.full((3, 3), 0.0625)
    )
    yield win
    win.close()


class TestExactValues:
    def test_self_paste_changes_nothing(self, window_precise):
        viewer = window_precise.viewer
        signals = []
        viewer.axis_bulk_changes.connect(lambda t, c: signals.append(c))
        viewer.bulk_changes.connect(lambda t, c: signals.append(c))
        tw = viewer.table_widget
        _select(viewer, 0, 0, tw.rowCount() - 1, tw.columnCount() - 1)
        viewer.copy_selection()

        viewer.paste_selection()

        assert signals == []
        assert list(viewer.current_data["x_axis"]) == [0.0, 1.5625, 3.125]

    def test_copy_to_another_table_keeps_full_precision(
        self, window_precise, mock_settings
    ):
        _select(window_precise.viewer, 0, 1, 1, 3)  # X axis row + first data row
        window_precise.viewer.copy_selection()

        dst_win = _make_3d_window([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], np.zeros((3, 3)))
        try:
            dst = dst_win.viewer
            _select(dst, 0, 1, 0, 1)
            dst.paste_selection()

            assert list(dst.current_data["x_axis"]) == [0.0, 1.5625, 3.125]
            assert list(dst.current_data["values"][0]) == [0.0625] * 3
        finally:
            dst_win.close()

    def test_copy_keeps_display_text_for_other_apps(self, window_precise):
        _select(window_precise.viewer, 0, 1, 0, 2)
        window_precise.viewer.copy_selection()
        assert QApplication.clipboard().text() == "0.00\t1.56"

    def test_pasting_shown_text_keeps_exact_value(self, window_precise):
        """Text equal to what the cell shows (e.g. back from Excel) is a no-op."""
        viewer = window_precise.viewer
        _select(viewer, 0, 2, 0, 2)
        _paste(viewer, "1.56")
        assert viewer.current_data["x_axis"][1] == 1.5625


class TestClipboardText:
    def test_leading_newline_does_not_shift_rows(self, window_3d):
        viewer = window_3d.viewer
        _select(viewer, 1, 1, 1, 1)
        _paste(viewer, "\n55\t66")

        assert list(viewer.current_data["values"][0]) == [55.0, 66.0, 2.0]

    def test_excel_crlf_rows(self, window_3d):
        viewer = window_3d.viewer
        _select(viewer, 1, 1, 1, 1)
        _paste(viewer, "55\t66\r\n77\t88\r\n")

        assert list(viewer.current_data["values"][0]) == [55.0, 66.0, 2.0]
        assert list(viewer.current_data["values"][1]) == [77.0, 88.0, 5.0]

    def test_shown_number_in_other_spelling_is_a_no_op(self, qapp, mock_settings):
        """'0' for a cell showing '0.00' (Excel drops trailing zeros)."""
        win = _make_3d_window([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], np.full((3, 3), 1e-4))
        try:
            viewer = win.viewer
            assert viewer.table_widget.item(1, 1).text() == "0.00"
            _select(viewer, 1, 1, 1, 1)
            _paste(viewer, "0")
            assert viewer.current_data["values"][0, 0] == 1e-4
        finally:
            win.close()

    @pytest.mark.parametrize(
        "payload", [b"5", b"[1,2]", b"[[" + b"9" * 400 + b"]]", b"\xff", b"{}"]
    )
    def test_malformed_exact_data_does_not_crash(self, window_3d, payload):
        mime = QMimeData()
        mime.setText("")
        mime.setData(CELLS_MIME, payload)
        QApplication.clipboard().setMimeData(mime)
        _select(window_3d.viewer, 1, 1, 1, 1)

        window_3d.viewer.paste_selection()

        assert window_3d.viewer.current_data["values"][0, 0] == 0.0


# ---------------------------------------------------------------------------
# Values that do not fit storage, integer arrays
# ---------------------------------------------------------------------------


class TestStorageFit:
    def test_out_of_range_axis_is_skipped_and_data_still_applies(
        self, qapp, mock_settings
    ):
        """One paste, uint8 X axis: 3000 can't be stored. Before, the data
        write committed and the axis write failed with 'values reverted'."""
        win = _make_3d_window(
            [1.0, 2.0, 3.0], [10.0, 20.0, 30.0], np.zeros((3, 3)), x_scaling="U8"
        )
        try:
            viewer = win.viewer
            axis_signals, data_signals = [], []
            viewer.axis_bulk_changes.connect(lambda t, c: axis_signals.append(c))
            viewer.bulk_changes.connect(lambda t, c: data_signals.append(c))
            _select(viewer, 0, 1, 0, 1)
            _paste(viewer, "3000\t200\n11\t12")

            assert list(viewer.current_data["x_axis"]) == [1.0, 200.0, 3.0]
            assert list(viewer.current_data["values"][0]) == [11.0, 12.0, 0.0]
            assert [c[:4] for c in axis_signals[0]] == [("x_axis", 1, 2.0, 200.0)]
            assert len(data_signals[0]) == 2
        finally:
            win.close()

    def test_unprogrammed_nan_cell_stays_editable(self, qapp, mock_settings):
        """0xFFFFFFFF patch-area floats read as NaN; they must stay editable."""
        win = _make_3d_window([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], np.full((3, 3), np.nan))
        try:
            viewer = win.viewer
            viewer.table_widget.item(1, 1).setText("5")
            _select(viewer, 2, 1, 2, 1)
            _paste(viewer, "6")

            assert viewer.current_data["values"][0, 0] == 5.0
            assert viewer.current_data["values"][1, 0] == 6.0
        finally:
            win.close()

    def test_typed_out_of_range_value_reverts(self, window_hex):
        viewer = window_hex.viewer
        viewer.table_widget.item(0, 0).setText("1FFFFFFFF")  # > uint32
        assert viewer.current_data["values"][0] == 0x0205451A
        assert viewer.table_widget.item(0, 0).text().strip() == "205451a"

    def test_fraction_into_int_array_rounds_like_the_rom(self, window_hex):
        """int64 arrays truncate on assignment; the ROM writer rounds."""
        viewer = window_hex.viewer
        changes = []
        viewer.bulk_changes.connect(lambda t, c: changes.append(c))
        mime = QMimeData()
        mime.setText("x")
        mime.setData(CELLS_MIME, b"[[100.7]]")
        QApplication.clipboard().setMimeData(mime)
        _select(viewer, 0, 0, 0, 0)

        viewer.paste_selection()

        assert viewer.current_data["values"][0] == 101
        assert changes[0][0][3] == 101.0  # new value carried to undo / ROM
        assert viewer.table_widget.item(0, 0).text().strip() == "65"


# ---------------------------------------------------------------------------
# Hex cells
# ---------------------------------------------------------------------------


class TestHexCells:
    def test_copied_text_pastes_into_another_rom(self, window_hex, mock_settings):
        """The rendered hex text (as another app would paste it) round-trips."""
        viewer = window_hex.viewer
        _select(viewer, 0, 0, 0, 0)
        viewer.copy_selection()
        copied = QApplication.clipboard().text()
        assert copied.strip() == "205451a"

        other = TableViewerWindow(
            window_hex.table,
            {"values": np.array([0xFFFFFFFF], dtype=np.int64)},
            _make_definition(),
            rom_path="/tmp/other.bin",
        )
        try:
            _select(other.viewer, 0, 0, 0, 0)
            _paste(other.viewer, copied)  # text only, no exact values
            assert other.viewer.current_data["values"][0] == 0x0205451A
        finally:
            other.close()

    def test_overlong_hex_is_rejected_without_crash(self, window_hex):
        viewer = window_hex.viewer
        _select(viewer, 0, 0, 0, 0)
        _paste(viewer, "F" * 300)

        assert viewer.current_data["values"][0] == 0x0205451A
        assert parse_cell_text("F" * 17, "8x") is None

    def test_all_digit_hex_is_not_read_as_decimal(self, window_hex):
        viewer = window_hex.viewer
        changes = []
        viewer.bulk_changes.connect(lambda t, c: changes.append(c))
        _select(viewer, 0, 0, 0, 0)
        _paste(viewer, "02054517")

        assert viewer.current_data["values"][0] == 0x02054517
        assert changes[0][0][3] == 0x02054517  # new display value
        assert changes[0][0][5] == 0x02054517  # new raw value

    def test_typed_hex_edit(self, window_hex):
        viewer = window_hex.viewer
        viewer.table_widget.item(0, 0).setText("2054517")

        assert viewer.current_data["values"][0] == 0x02054517

    def test_editor_accepts_hex_digits(self, window_hex):
        tw = window_hex.viewer.table_widget
        index = tw.model().index(0, 0)
        editor = tw.itemDelegate().createEditor(tw, None, index)
        try:
            validator = editor.validator()
            assert validator.validate("0205451a", 0)[0] == QValidator.Acceptable
            # '%08x' renders space-padded; the editor opens with that text
            assert validator.validate(" 205451b", 0)[0] == QValidator.Acceptable
            assert validator.validate("1.5", 0)[0] == QValidator.Invalid
        finally:
            editor.deleteLater()

    def test_decimal_editor_still_rejects_letters(self, window_3d):
        tw = window_3d.viewer.table_widget
        index = tw.model().index(1, 1)
        assert index.data(Qt.UserRole) == (0, 0)
        editor = tw.itemDelegate().createEditor(tw, None, index)
        try:
            assert editor.validator().validate("1a", 0)[0] == QValidator.Invalid
        finally:
            editor.deleteLater()
