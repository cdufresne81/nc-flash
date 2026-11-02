"""
Data Operation Dialogs

Dialogs for entering values for data manipulation operations.
"""

import re
from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QDialogButtonBox,
)
from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QDoubleValidator, QRegularExpressionValidator

from ..utils.formatting import (
    HEX_INPUT_PATTERN,
    is_hex_format,
    parse_cell_text,
    parse_numeric_text,
)
from . import theme

# Shown by Add/Multiply on a table that displays hex values (e.g. Tire Size
# Correction): their operand is always a plain decimal number.
HEX_OPERAND_NOTE = "This table shows hex values. Enter the amount as a decimal number."


# Largest magnitude any table can hold: float tables are 32-bit floats and
# integer tables top out far below this. Anything bigger (or "1e999", which
# Python reads as infinity) would only ever write garbage into the ROM.
MAX_ABS_VALUE = 3.4028234663852886e38

MULTIPLY_MAX_FACTOR = 1000000.0

VALUE_ERROR = (
    "Enter a number. Values beyond 3.4e38 either way (including infinity) "
    "can't be stored in a table. Type thousands without a separator "
    "(1000, not 1,000)."
)

# "1,000" or "-12,500": a comma followed by exactly three digits after a
# non-zero lead reads as thousands grouping to many people, while the comma
# decimal mark would turn it into 1.0. Refused rather than guessed.
_GROUPED_NUMBER_RE = re.compile(r"^[+-]?[1-9]\d{0,2},\d{3}$")


def parse_dialog_number(text: str) -> Optional[float]:
    """The finite number typed in a dialog, or None.

    QDoubleValidator lets out-of-range text such as "1e999" through as
    "intermediate", so every dialog value goes through the same gate as cell
    edits (no inf/nan, comma as decimal mark), and a number that looks like
    thousands grouping is refused instead of being read as a decimal.
    """
    if text is None:
        return None
    if _GROUPED_NUMBER_RE.match(text.strip()):
        return None
    return parse_numeric_text(text)


def _make_error_label() -> QLabel:
    label = QLabel("")
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.WARNING_AMBER};")
    label.setVisible(False)
    return label


def _show_error(label: QLabel, edit: QLineEdit, message: str):
    label.setText(message)
    label.setVisible(True)
    edit.selectAll()
    edit.setFocus()


class AddValueDialog(QDialog):
    """Dialog to enter value to add/subtract from selected cells"""

    def __init__(self, parent=None, hex_table: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Add to Data")
        self.setMinimumWidth(300)
        self._value = 0.0
        self._hex_table = hex_table
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Instructions
        info = QLabel(
            "Enter value to add to selected cells\n(use negative values to subtract)"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        if self._hex_table:
            note = QLabel(HEX_OPERAND_NOTE)
            note.setWordWrap(True)
            layout.addWidget(note)

        # Value input
        input_layout = QHBoxLayout()
        input_layout.addWidget(QLabel("Value:"))

        self.value_edit = QLineEdit()
        self.value_edit.setValidator(QDoubleValidator())
        self.value_edit.setText("0.0")
        self.value_edit.selectAll()
        input_layout.addWidget(self.value_edit)

        layout.addLayout(input_layout)

        self.error_label = _make_error_label()
        layout.addWidget(self.error_label)

        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # Focus on input
        self.value_edit.setFocus()

    def _on_accept(self):
        """Validate and accept: only a finite, storable number gets through."""
        value = parse_dialog_number(self.value_edit.text())
        if value is None or abs(value) > MAX_ABS_VALUE:
            _show_error(
                self.error_label,
                self.value_edit,
                VALUE_ERROR,
            )
            return
        self._value = value
        self.accept()

    def get_value(self) -> float:
        """Get the entered value"""
        return self._value


class MultiplyDialog(QDialog):
    """Dialog to enter multiplier factor"""

    def __init__(self, parent=None, hex_table: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Multiply Data")
        self.setMinimumWidth(350)
        self._factor = 1.0
        self._hex_table = hex_table
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Instructions
        info = QLabel("Enter multiplier factor for selected cells")
        info.setWordWrap(True)
        layout.addWidget(info)
        if self._hex_table:
            note = QLabel(HEX_OPERAND_NOTE)
            note.setWordWrap(True)
            layout.addWidget(note)

        # Factor input
        input_layout = QHBoxLayout()
        input_layout.addWidget(QLabel("Factor:"))

        self.factor_edit = QLineEdit()
        self.factor_edit.setValidator(QDoubleValidator(0.0, MULTIPLY_MAX_FACTOR, 10))
        self.factor_edit.setText("1.0")
        self.factor_edit.selectAll()
        self.factor_edit.textChanged.connect(self._update_percentage)
        input_layout.addWidget(self.factor_edit)

        layout.addLayout(input_layout)

        # Percentage equivalent label
        self.percentage_label = QLabel("(0% change)")
        self.percentage_label.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.percentage_label)

        self.error_label = _make_error_label()
        layout.addWidget(self.error_label)

        # Examples
        examples = QLabel(
            "<b>Examples:</b><br>"
            "1.1 = +10% increase<br>"
            "0.9 = -10% decrease<br>"
            "2.0 = double values"
        )
        examples.setWordWrap(True)
        examples.setStyleSheet("margin-top: 10px; padding: 5px; background: #f0f0f0;")
        layout.addWidget(examples)

        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # Focus on input
        self.factor_edit.setFocus()

    def _update_percentage(self, text):
        """Update percentage label based on factor"""
        try:
            factor = float(text)
            percentage = (factor - 1.0) * 100
            if percentage >= 0:
                self.percentage_label.setText(f"(+{percentage:.1f}% change)")
            else:
                self.percentage_label.setText(f"({percentage:.1f}% change)")
        except ValueError:
            self.percentage_label.setText("")

    def _on_accept(self):
        """Validate and accept: a finite factor in (0, 1,000,000]."""
        factor = parse_dialog_number(self.factor_edit.text())
        if factor is None or not 0 < factor <= MULTIPLY_MAX_FACTOR:
            _show_error(
                self.error_label,
                self.factor_edit,
                "Enter a factor greater than 0 and at most 1,000,000.",
            )
            return
        self._factor = factor
        self.accept()

    def get_factor(self) -> float:
        """Get the entered factor"""
        return self._factor


class SetValueDialog(QDialog):
    """Dialog to set all selected cells to a specific value"""

    def __init__(self, selected_count: int = 0, parent=None, format_spec: str = ""):
        """
        Args:
            selected_count: Number of cells the value will be applied to.
            parent: Parent widget.
            format_spec: Python format spec the selected cells display in.
                A hex spec ('8x', e.g. Tire Size Correction) makes the dialog
                take hex text, the same way those cells are typed.
        """
        super().__init__(parent)
        self.setWindowTitle("Set Value")
        self.setMinimumWidth(300)
        self._value = 0.0
        self._selected_count = selected_count
        self._format_spec = format_spec
        self._init_ui()

    def is_hex(self) -> bool:
        """True if the dialog takes hex text."""
        return is_hex_format(self._format_spec)

    def _init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)
        hex_input = self.is_hex()

        # Instructions
        info = QLabel(
            "Set all selected cells to this hex value:"
            if hex_input
            else "Set all selected cells to this value:"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # Value input
        input_layout = QHBoxLayout()
        input_layout.addWidget(QLabel("Value (hex):" if hex_input else "Value:"))

        self.value_edit = QLineEdit()
        if hex_input:
            self.value_edit.setValidator(
                QRegularExpressionValidator(
                    QRegularExpression(HEX_INPUT_PATTERN), self.value_edit
                )
            )
            self.value_edit.setText("0")
        else:
            self.value_edit.setValidator(QDoubleValidator())
            self.value_edit.setText("0.0")
        self.value_edit.selectAll()
        input_layout.addWidget(self.value_edit)

        layout.addLayout(input_layout)

        self.error_label = _make_error_label()
        layout.addWidget(self.error_label)

        # Preview label
        if self._selected_count > 0:
            self.preview_label = QLabel(
                f"This will affect {self._selected_count} cell(s)"
            )
            self.preview_label.setStyleSheet("color: #666; font-size: 11px;")
            layout.addWidget(self.preview_label)

        # Buttons
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # Focus on input
        self.value_edit.setFocus()

    def _on_accept(self):
        """Validate and accept: only a finite, storable number gets through."""
        if self.is_hex():
            # Same parser the table cells use: hex text, optional '0x'.
            value = parse_cell_text(self.value_edit.text(), self._format_spec)
            message = "Enter a hex value, for example 2054517 or 0x2054517."
        else:
            value = parse_dialog_number(self.value_edit.text())
            message = VALUE_ERROR
        if value is None or abs(value) > MAX_ABS_VALUE:
            _show_error(self.error_label, self.value_edit, message)
            return
        self._value = value
        self.accept()

    def get_value(self) -> float:
        """Get the entered value"""
        return self._value
