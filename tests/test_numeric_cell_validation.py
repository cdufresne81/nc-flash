"""
Tests for numeric-only enforcement in table cells (issue #92).

Covers the three routes text can take into table data:
  1. the cell editor itself (delegate validator)
  2. committed cell / axis edits (TableEditHelper)
  3. clipboard paste (TableClipboardHelper)

Alphabetic spellings float() would happily accept ('nan', 'inf') are the
dangerous case: they used to land in the ROM image.
"""

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator, QValidator
from PySide6.QtWidgets import QApplication, QTableWidgetSelectionRange

from src.core.rom_definition import (
    AxisType,
    RomDefinition,
    RomID,
    Scaling,
    Table,
    TableType,
)
from src.ui.table_viewer_helpers.cell_delegate import ModifiedCellDelegate
from src.ui.table_viewer_window import TableViewerWindow
from src.utils.formatting import NUMERIC_INPUT_PATTERN, parse_numeric_text

# ---------------------------------------------------------------------------
# parse_numeric_text
# ---------------------------------------------------------------------------


class TestParseNumericText:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0", 0.0),
            ("42", 42.0),
            ("-1.5", -1.5),
            ("+3", 3.0),
            (".5", 0.5),
            ("12.", 12.0),
            ("1e3", 1000.0),
            ("2.5E-2", 0.025),
            ("  7  ", 7.0),
            ("1,5", 1.5),
            ("-0,25", -0.25),
        ],
    )
    def test_accepts_numeric_text(self, text, expected):
        assert parse_numeric_text(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text",
        [
            "abc",
            "a",
            "Z",
            "12abc",
            "1.2.3",
            "0x10",
            "1_0",
            "",
            "   ",
            "-",
            "1e",
            None,
        ],
    )
    def test_rejects_non_numeric_text(self, text):
        assert parse_numeric_text(text) is None

    @pytest.mark.parametrize("text", ["nan", "NaN", "inf", "-inf", "Infinity", "+INF"])
    def test_rejects_alphabetic_float_spellings(self, text):
        """float() accepts these; a ROM cell cannot hold them."""
        assert parse_numeric_text(text) is None


# ---------------------------------------------------------------------------
# Delegate editor validator
# ---------------------------------------------------------------------------


@pytest.fixture
def input_validator(qapp):
    """The validator the cell editor installs (keystroke level)."""
    return QRegularExpressionValidator(QRegularExpression(NUMERIC_INPUT_PATTERN))


class TestNumericValidatorPattern:
    @pytest.mark.parametrize(
        "text",
        ["a", "Z", "abc", "nan", "inf", "12a", "1_0", "0x10", "1e3", "1E3", "$5"],
    )
    def test_letters_are_invalid(self, input_validator, text):
        """No a-Z keystroke may be entered in a cell."""
        state, _, _ = input_validator.validate(text, len(text))
        assert state == QValidator.Invalid

    @pytest.mark.parametrize("text", ["0", "42", "-1.5", "1,5", "+3", ".5", "12."])
    def test_digits_sign_and_separator_are_typeable(self, input_validator, text):
        state, _, _ = input_validator.validate(text, len(text))
        assert state == QValidator.Acceptable

    @pytest.mark.parametrize("text", ["", "-", "+", "-."])
    def test_partial_entry_stays_typeable(self, input_validator, text):
        """Mid-typing states must not be rejected keystroke by keystroke.

        These never commit: parse_numeric_text rejects them and the cell
        reverts to its previous value.
        """
        state, _, _ = input_validator.validate(text, len(text))
        assert state != QValidator.Invalid
        assert parse_numeric_text(text) is None


# ---------------------------------------------------------------------------
# Fixtures for the widget-level tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


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


@pytest.fixture
def mock_settings():
    mock = MagicMock()
    mock.get_colormap_path.return_value = None
    mock.get_show_type_column.return_value = True
    mock.get_show_address_column.return_value = True
    mock.get_auto_round.return_value = False
    with patch("src.utils.settings.get_settings", return_value=mock):
        yield mock


@pytest.fixture
def window_2d(qapp, mock_settings):
    """2D table: column 0 is the Y axis, column 1 the values."""
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
    data = {
        "values": np.array([10.0, 20.0, 30.0]),
        "y_axis": np.array([100.0, 200.0, 300.0]),
    }
    win = TableViewerWindow(table, data, _make_definition(), rom_path="/tmp/test.bin")
    yield win
    win.close()


# ---------------------------------------------------------------------------
# Committed edits
# ---------------------------------------------------------------------------


class TestCellEditRejectsNonNumeric:
    @pytest.mark.parametrize("text", ["abc", "nan", "inf", "-inf", "12abc", "1_0"])
    def test_value_cell_reverts(self, window_2d, text):
        tw = window_2d.viewer.table_widget
        tw.item(0, 1).setText(text)

        assert window_2d.viewer.current_data["values"][0] == pytest.approx(10.0)
        assert tw.item(0, 1).text() == "10.00"

    @pytest.mark.parametrize("text", ["abc", "nan", "inf"])
    def test_axis_cell_reverts(self, window_2d, text):
        tw = window_2d.viewer.table_widget
        tw.item(0, 0).setText(text)

        assert window_2d.viewer.current_data["y_axis"][0] == pytest.approx(100.0)
        assert tw.item(0, 0).text() == "100.00"

    def test_numeric_edit_still_applies(self, window_2d):
        tw = window_2d.viewer.table_widget
        tw.item(0, 1).setText("55.5")

        assert window_2d.viewer.current_data["values"][0] == pytest.approx(55.5)

    def test_numeric_axis_edit_still_applies(self, window_2d):
        tw = window_2d.viewer.table_widget
        tw.item(0, 0).setText("150")

        assert window_2d.viewer.current_data["y_axis"][0] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# Clipboard paste
# ---------------------------------------------------------------------------


class TestPasteRejectsNonNumeric:
    def test_paste_skips_non_numeric_cells(self, window_2d, qapp, fake_clipboard):
        tw = window_2d.viewer.table_widget
        tw.clearSelection()
        tw.setRangeSelected(QTableWidgetSelectionRange(0, 1, 2, 1), True)

        QApplication.clipboard().setText("nan\ninf\n77")

        window_2d.viewer.paste_selection()

        values = window_2d.viewer.current_data["values"]
        assert values[0] == pytest.approx(10.0)
        assert values[1] == pytest.approx(20.0)
        assert values[2] == pytest.approx(77.0)


# ---------------------------------------------------------------------------
# Editor creation
# ---------------------------------------------------------------------------


class TestDelegateEditor:
    def test_editor_has_numeric_validator(self, window_2d):
        tw = window_2d.viewer.table_widget
        delegate = tw.itemDelegate()
        assert isinstance(delegate, ModifiedCellDelegate)

        index = tw.model().index(0, 1)
        editor = delegate.createEditor(tw, None, index)
        try:
            validator = editor.validator()
            assert validator is not None
            assert validator.validate("abc", 3)[0] == QValidator.Invalid
            assert validator.validate("nan", 3)[0] == QValidator.Invalid
            assert validator.validate("12.5", 4)[0] == QValidator.Acceptable
        finally:
            editor.deleteLater()
