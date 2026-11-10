"""Datalogger window: pick channels, start/stop a live log, follow it in MLV.

A thin surface over the main-window-owned :class:`~src.ui.datalogger.Datalogger`
(closing the window stops nothing). The channel list it edits is saved in
settings at every change; the owner reads it when a run starts (every adapter,
the WiCAN included) or when it is saved as the WiCAN's own list.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.datalog_channels import (
    KINDS,
    MAX_EVERY,
    MAX_NAME_LEN,
    MODE_DID,
    MODE_LABELS,
    MODE_MEM,
    MODE_OBD,
    Channel,
    ChannelError,
    catalog_channels,
    compile_formula,
    channels_to_saved,
    estimated_rows_per_second,
)
from src.core.live_csv import live_capture_dir
from src.ui import theme

logger = logging.getLogger(__name__)

_COLS = ["Log", "Name", "Mode", "ID", "Unit", "Every", "Formula", "Status"]
_COL_ON, _COL_NAME, _COL_MODE, _COL_ID, _COL_UNIT, _COL_EVERY, _COL_FORMULA, _COL_STATUS = range(8)  # fmt: skip

_ADAPTER_TEXT = {
    "j2534": "Tactrix OpenPort (J2534): NC Flash polls the ECU through the cable.",
    "obdx": "OBDX Pro: NC Flash polls the ECU through the adapter.",
    "wican": (
        "WiCAN: Start Logging sends these channels to the WiCAN for this run "
        "only, and NC Flash follows what it logs."
    ),
}


_PUSH_TITLE = "Update WiCAN Channels"

_OWN_LIST_TEXT = (
    "The list above is NC Flash's, not the one stored on the WiCAN. Without "
    "NC Flash, the WiCAN logs its own list on every drive, and Start Logging "
    "never changes it. Update WiCAN Channels replaces the WiCAN's polled "
    "channels with the ticked ones; its CAN bus and calculated channels stay."
)

# Shown under the list of changes until the user ticks "Don't explain this
# again". The list of changes itself is always shown: it differs every time.
_PUSH_EXPLAIN = (
    "This changes what the WiCAN logs by itself, when NC Flash is not "
    "connected: the trips it saves on its SD card on every drive.\n\n"
    "You do not need this to log from NC Flash. Start Logging already sends "
    "the ticked channels to the WiCAN for that run only, and then lets the "
    "WiCAN go back to its own list.\n\n"
    "Its broadcast and calculated channels and its other settings are kept. "
    "Its current list is saved on this PC first, and Put Back Its Previous "
    "List restores it."
)


class _ChannelTable(QTableWidget):
    """A table whose rows can be dragged to a new place.

    Qt's own row move would leave the cell widgets (the Every spin boxes) behind
    and overwrite cells, so the drop is taken here and reported as
    ``row_moved(from, to)``; the window reorders its list and refills.
    """

    row_moved = Signal(int, int)

    def __init__(self, *args):
        super().__init__(*args)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def drop_target(self, pos) -> int:
        """The row a drop at *pos* lands before (rowCount() = at the end)."""
        row = self.rowAt(pos.y())
        if row < 0:
            return self.rowCount()
        rect = self.visualRect(self.model().index(row, 0))
        return row + 1 if pos.y() > rect.center().y() else row

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        rows = self.selectionModel().selectedRows()
        if rows:
            src = rows[0].row()
            dst = self.drop_target(event.position().toPoint())
            if dst > src:
                dst -= 1  # the row leaves its old place first
            if dst != src:
                self.row_moved.emit(src, dst)
        event.setDropAction(Qt.IgnoreAction)  # nothing for Qt itself to move
        event.accept()


def _ident_text(ch: Channel) -> str:
    width = {MODE_OBD: 2, MODE_DID: 4, MODE_MEM: 8}[ch.mode]
    text = f"0x{ch.ident:0{width}X}"
    if ch.bit is not None:
        text += f" bit {ch.bit}"
    return text


class ChannelDialog(QDialog):
    """Add or edit a custom channel (mode 0x22 / 0x23, or an OBD PID)."""

    def __init__(self, parent=None, channel: Optional[Channel] = None, taken=()):
        super().__init__(parent)
        self.setWindowTitle("Edit Channel" if channel else "Add Channel")
        self._taken = set(taken)
        self.result_channel: Optional[Channel] = None
        ch = channel or Channel(name="", mode=MODE_DID, ident=0)

        form = QFormLayout()
        self._name = QLineEdit(ch.name)
        self._name.setMaxLength(MAX_NAME_LEN)
        form.addRow("Name", self._name)
        self._mode = QComboBox()
        for mode, text in (
            (MODE_DID, "0x22  Read data by identifier"),
            (MODE_MEM, "0x23  Read memory (patched ROM only)"),
            (MODE_OBD, "0x01  OBD-II PID"),
        ):
            self._mode.addItem(text, mode)
        self._mode.setCurrentIndex(self._mode.findData(ch.mode))
        form.addRow("Mode", self._mode)
        self._ident = QLineEdit(f"{ch.ident:X}" if channel else "")
        self._ident.setPlaceholderText("hex, e.g. 1746 or FFFFAC18")
        form.addRow("Identifier / address", self._ident)
        self._size = QSpinBox()
        self._size.setRange(1, 4)
        self._size.setValue(ch.size)
        form.addRow("Data bytes", self._size)
        self._offset = QSpinBox()
        self._offset.setRange(0, 5)
        self._offset.setValue(ch.offset)
        form.addRow("Skip bytes", self._offset)
        self._kind = QComboBox()
        self._kind.addItems(KINDS)
        self._kind.setCurrentText(ch.kind)
        form.addRow("Type", self._kind)
        self._bit = QComboBox()
        self._bit.addItem("whole value", None)
        for b in range(8):
            self._bit.addItem(f"bit {b}", b)
        self._bit.setCurrentIndex(self._bit.findData(ch.bit))
        form.addRow("Bit", self._bit)
        self._formula = QLineEdit(ch.formula)
        form.addRow("Formula", self._formula)
        self._unit = QLineEdit(ch.unit)
        form.addRow("Unit", self._unit)
        self._min = QLineEdit("" if ch.min is None else f"{ch.min:g}")
        self._max = QLineEdit("" if ch.max is None else f"{ch.max:g}")
        self._min.setPlaceholderText("none")
        self._max.setPlaceholderText("none")
        form.addRow("Min", self._min)
        form.addRow("Max", self._max)
        self._every = QSpinBox()
        self._every.setRange(1, MAX_EVERY)
        self._every.setValue(ch.every)
        form.addRow("Poll every Nth sweep", self._every)

        help_text = QLabel(
            "x is the raw value: the data bytes after the identifier, big-endian. "
            "The formula may use x, numbers, + - * / and parentheses, e.g. "
            "x*0.25 or x*0.5-64. Mode 0x01 and 0x23 channels poll fast; every "
            "mode 0x22 or 0x23 channel adds about 8 ms to each sweep."
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet(f"color: {theme.TEXT_MUTED};")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(help_text)
        root.addWidget(buttons)
        self.setMinimumWidth(420)

    def _number(self, edit: QLineEdit) -> Optional[float]:
        text = edit.text().strip()
        return None if not text else float(text)

    def _on_ok(self):
        try:
            ident_text = self._ident.text().strip()
            if ident_text.lower().startswith("0x"):
                ident_text = ident_text[2:]
            ch = Channel(
                name=self._name.text().strip(),
                mode=self._mode.currentData(),
                ident=int(ident_text, 16) if ident_text else -1,
                size=self._size.value(),
                offset=self._offset.value(),
                kind=self._kind.currentText(),
                bit=self._bit.currentData(),
                formula=self._formula.text().strip() or "x",
                unit=self._unit.text().strip(),
                min=self._number(self._min),
                max=self._number(self._max),
                every=self._every.value(),
            )
            if ch.name in self._taken:
                raise ChannelError(f"A channel named {ch.name} already exists.")
            ch.validate()
        except (ValueError, ChannelError) as exc:
            QMessageBox.warning(self, "Channel", str(exc))
            return
        self.result_channel = ch
        self.accept()


def _status_value(value: float, unit: str) -> str:
    """A live value as the Status column shows it."""
    if unit.replace(" ", "").lower() == "on/off":
        return "on" if value else "off"
    if abs(value) >= 100:
        text = f"{value:.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.3f}".rstrip("0").rstrip(".") or "0"
    return f"{text} {unit}".strip()


def _read_key(ch: Channel) -> tuple:
    """What a channel reads from the ECU, whatever its name."""
    return (ch.mode, ch.ident, ch.offset, ch.size, ch.kind, ch.bit)


def _same_formula(a: Channel, b: Channel) -> bool:
    """Same scaling: compare on a few raw values (parentheses don't matter)."""
    try:
        fa, fb = compile_formula(a.formula), compile_formula(b.formula)
    except ChannelError:
        return a.formula.replace(" ", "") == b.formula.replace(" ", "")
    # Relative: 100/255 written to 8 or 9 decimals is the same scaling.
    return all(
        abs(fa(x) - fb(x)) <= 1e-6 * max(1.0, abs(fa(x)))
        for x in (1, 7, 100, 255, 1000)
    )


class CatalogDialog(QDialog):
    """Pick channels from the bundled NC Miata list (search, tick, add).

    Channels whose name is already in the user's list are shown greyed out.
    """

    _COLS = ["Add", "Name", "Description", "Unit", "Mode", "ID"]

    def __init__(self, parent=None, catalog=None, taken=(), existing=()):
        super().__init__(parent)
        self.setWindowTitle("Add from NC Miata List")
        self.resize(820, 560)
        self._catalog = list(catalog if catalog is not None else catalog_channels())
        taken_lower = {t.lower() for t in taken}
        # The same read under another name (VT_ACT is the built-in VCT_ACT):
        # compare what is read, not only the name.
        by_read = {}
        for ex in existing:
            by_read.setdefault(_read_key(ex), ex)
        self.result_channels: list[Channel] = []

        root = QVBoxLayout(self)
        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Search by name or description (e.g. fan, voltage, VCT)"
        )
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        root.addWidget(self._search)

        self._table = QTableWidget(len(self._catalog), len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, ch in enumerate(self._catalog):
            twin = by_read.get(_read_key(ch))
            same = twin is not None and _same_formula(twin, ch)
            have = ch.name.lower() in taken_lower or same
            why = "Already in your list"
            if same and twin.name.lower() != ch.name.lower():
                why = f"Already in your list as {twin.name} (same reading)"
            elif twin is not None and not same:
                why = (
                    f"Reads the same value as {twin.name} in your list, "
                    "with a different formula"
                )
            box = QTableWidgetItem()
            if have or twin is not None:
                box.setToolTip(why)
            if have:
                box.setFlags(Qt.ItemIsSelectable)  # greyed out, can't be ticked
            else:
                box.setFlags(
                    Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
                )
                box.setCheckState(Qt.Unchecked)
            self._table.setItem(row, 0, box)
            for col, text in enumerate(
                (
                    ch.name,
                    ch.description,
                    ch.unit,
                    MODE_LABELS[ch.mode],
                    _ident_text(ch),
                ),
                1,
            ):
                item = QTableWidgetItem(text)
                if have:
                    item.setFlags(Qt.ItemIsSelectable)
                if twin is not None or have:
                    item.setToolTip(why)
                if (
                    col == 1
                    and twin is not None
                    and twin.name.lower() != ch.name.lower()
                ):
                    item.setText(f"{ch.name}  (= {twin.name})")
                self._table.setItem(row, col, item)
        for col, width in ((0, 40), (1, 150), (2, 330), (3, 70), (4, 50)):
            self._table.setColumnWidth(col, width)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemChanged.connect(self._update_ok)
        self._table.cellDoubleClicked.connect(self._toggle_row)
        root.addWidget(self._table, stretch=1)

        note = QLabel(
            "From a community list of NC MX-5 channels. Standard OBD channels are "
            "well known; Mazda-specific ones (mode 22) are not all checked on a car yet."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._update_ok()

    def _toggle_row(self, row: int, _col: int):
        box = self._table.item(row, 0)
        if box.flags() & Qt.ItemIsUserCheckable:
            box.setCheckState(
                Qt.Unchecked if box.checkState() == Qt.Checked else Qt.Checked
            )

    def _filter(self, text: str):
        words = text.lower().split()
        for row, ch in enumerate(self._catalog):
            hay = f"{ch.name} {ch.description} {ch.unit}".lower()
            self._table.setRowHidden(row, not all(w in hay for w in words))

    def _checked(self) -> list[Channel]:
        return [
            ch
            for row, ch in enumerate(self._catalog)
            if self._table.item(row, 0).checkState() == Qt.Checked
            and self._table.item(row, 0).flags() & Qt.ItemIsUserCheckable
        ]

    def _update_ok(self, *_):
        n = len(self._checked())
        self._ok.setText(f"Add {n} Channel{'s' if n != 1 else ''}" if n else "Add")
        self._ok.setEnabled(n > 0)

    def _on_ok(self):
        picked = []
        for ch in self._checked():
            ch = replace(ch, enabled=True, builtin=False)
            picked.append(ch)
        self.result_channels = picked
        self.accept()


class DataloggerWindow(QMainWindow):
    """Channel picker + run controls for the main window's Datalogger."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self._datalogger = main_window.datalogger
        self._settings = main_window.settings
        self._channels: list[Channel] = self._datalogger.channels()
        self._dropped: dict[str, str] = {}
        self._filling = False

        self.setWindowTitle("Datalogger")
        self.setMinimumSize(720, 480)
        self.resize(820, 600)
        self._build_ui()
        self._fill_table()

        dl = self._datalogger
        dl.running_changed.connect(self._update_state)
        dl.status_changed.connect(self._status.setText)
        dl.capture_changed.connect(self._update_state)
        dl.channel_dropped.connect(self._on_channel_dropped)
        dl.push_running_changed.connect(self._update_state)
        dl.push_preview.connect(self._on_push_preview)
        dl.push_finished.connect(self._on_push_finished)
        if hasattr(dl, "restore_changed"):
            dl.restore_changed.connect(self._on_restore_changed)
        # Status column: live values during a run, refreshed a few times a
        # second from the values both engines publish for the table cursor.
        self._live = getattr(dl, "live_values", None)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(250)
        self._status_timer.timeout.connect(self._refresh_status)
        dl.running_changed.connect(self._on_running_changed)
        self._update_state()
        # Opened while a run is going (the window is rebuilt on every open).
        self._on_running_changed(bool(dl.is_running))

    # --- UI -------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self._adapter = QLabel("")
        self._adapter.setWordWrap(True)
        root.addWidget(self._adapter)

        self._table = _ChannelTable(0, len(_COLS))
        self._table.setHorizontalHeaderLabels(_COLS)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        for col, width in (
            (_COL_ON, 40),
            (_COL_NAME, 130),
            (_COL_MODE, 50),
            (_COL_ID, 100),
            (_COL_UNIT, 60),
            (_COL_EVERY, 90),
            (_COL_FORMULA, 150),
        ):
            self._table.setColumnWidth(col, width)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._update_state)
        self._table.cellDoubleClicked.connect(lambda *_: self._on_edit())
        self._table.row_moved.connect(self._move_row)
        root.addWidget(self._table, stretch=1)

        edit_row = QHBoxLayout()
        self._btn_add = QPushButton("Add Channel...")
        self._btn_add.clicked.connect(self._on_add)
        edit_row.addWidget(self._btn_add)
        self._btn_catalog = QPushButton("Add from NC Miata List...")
        self._btn_catalog.setToolTip(
            "Pick more channels from a list of known NC MX-5 channels"
        )
        self._btn_catalog.clicked.connect(self._on_add_from_catalog)
        edit_row.addWidget(self._btn_catalog)
        self._btn_edit = QPushButton("Edit...")
        self._btn_edit.clicked.connect(self._on_edit)
        edit_row.addWidget(self._btn_edit)
        self._btn_remove = QPushButton("Remove")
        self._btn_remove.clicked.connect(self._on_remove)
        edit_row.addWidget(self._btn_remove)
        self._btn_up = QPushButton("Move Up")
        self._btn_up.setToolTip(
            "Move the selected channel up (Alt+Up). You can also drag a row."
        )
        self._btn_up.setShortcut("Alt+Up")
        self._btn_up.clicked.connect(partial(self._on_move, -1))
        edit_row.addWidget(self._btn_up)
        self._btn_down = QPushButton("Move Down")
        self._btn_down.setToolTip(
            "Move the selected channel down (Alt+Down). The log file's columns "
            "follow this order."
        )
        self._btn_down.setShortcut("Alt+Down")
        self._btn_down.clicked.connect(partial(self._on_move, 1))
        edit_row.addWidget(self._btn_down)
        edit_row.addStretch()
        self._estimate = QLabel("")
        self._estimate.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        edit_row.addWidget(self._estimate)
        root.addLayout(edit_row)

        run_row = QHBoxLayout()
        self._btn_run = QPushButton("Start Logging")
        self._btn_run.setMinimumHeight(32)
        self._btn_run.clicked.connect(self._on_run_toggle)
        run_row.addWidget(self._btn_run)
        self._btn_trail = QPushButton("Open in MegaLogViewerHD")
        self._btn_trail.setToolTip(
            "Follow the live capture in MegaLogViewerHD (Trail Live File mode)"
        )
        self._btn_trail.clicked.connect(self._on_trail)
        run_row.addWidget(self._btn_trail)
        self._btn_folder = QPushButton("Open Live Logs Folder")
        self._btn_folder.setToolTip(
            "Open the folder the live captures are saved in "
            "(the capture being written is selected)"
        )
        self._btn_folder.clicked.connect(self._on_open_folder)
        run_row.addWidget(self._btn_folder)
        run_row.addStretch()
        root.addLayout(run_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._chk_auto_trail = QCheckBox(
            "Open MegaLogViewerHD automatically when a capture starts"
        )
        self._chk_auto_trail.setChecked(self._settings.get_live_datalog_auto_trail())
        self._chk_auto_trail.toggled.connect(self._settings.set_live_datalog_auto_trail)
        root.addWidget(self._chk_auto_trail)

        router = self._main_window.live_cursor
        self._chk_live_cursor = QCheckBox(
            "Show the live position on open tables (e.g. RPM and LOAD on Load Scaling)"
        )
        self._chk_live_cursor.setToolTip(
            "Tables whose axes match logged channels light up the cells the "
            "engine is using, in the table and its graph."
        )
        self._chk_live_cursor.setChecked(router.enabled)
        self._chk_live_cursor.toggled.connect(router.set_enabled)
        root.addWidget(self._chk_live_cursor)

        # The WiCAN's own list is a setting, not a logging step: it gets its own
        # section, away from Start Logging, and only for the WiCAN.
        self._own_list_box = QGroupBox("WiCAN on its own (without NC Flash)")
        own = QVBoxLayout(self._own_list_box)
        own_text = QLabel(_OWN_LIST_TEXT)
        own_text.setWordWrap(True)
        own_text.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        own.addWidget(own_text)
        own_row = QHBoxLayout()
        self._btn_push = QPushButton(_PUSH_TITLE + "...")
        self._btn_push.setToolTip(
            "Make the channels ticked above the list the WiCAN logs on its own"
        )
        self._btn_push.clicked.connect(self._on_push)
        own_row.addWidget(self._btn_push)
        self._btn_restore = QPushButton("Put Back Its Previous List")
        self._btn_restore.setToolTip(
            "Put back the list the WiCAN had before the last copy"
        )
        self._btn_restore.clicked.connect(self._on_restore)
        own_row.addWidget(self._btn_restore)
        own_row.addStretch()
        own.addLayout(own_row)
        root.addWidget(self._own_list_box)

    def _fill_table(self):
        self._filling = True
        try:
            self._table.setRowCount(len(self._channels))
            for row, ch in enumerate(self._channels):
                on = QTableWidgetItem()
                on.setFlags(
                    Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
                )
                on.setCheckState(Qt.Checked if ch.enabled else Qt.Unchecked)
                self._table.setItem(row, _COL_ON, on)
                name = QTableWidgetItem(ch.name)
                name.setToolTip(
                    ch.description or ("Custom channel" if not ch.builtin else "")
                )
                self._table.setItem(row, _COL_NAME, name)
                self._table.setItem(
                    row, _COL_MODE, QTableWidgetItem(MODE_LABELS[ch.mode])
                )
                self._table.setItem(row, _COL_ID, QTableWidgetItem(_ident_text(ch)))
                self._table.setItem(row, _COL_UNIT, QTableWidgetItem(ch.unit))
                spin = QSpinBox()
                spin.setRange(1, MAX_EVERY)
                spin.setValue(ch.every)
                spin.setToolTip(
                    "Poll this channel on every Nth sweep (1 = every sweep)"
                )
                spin.valueChanged.connect(partial(self._on_every_changed, row))
                spin.setEnabled(not self._datalogger.is_running)
                self._table.setCellWidget(row, _COL_EVERY, spin)
                self._table.setItem(row, _COL_FORMULA, QTableWidgetItem(ch.formula))
                self._table.setItem(
                    row, _COL_STATUS, QTableWidgetItem(self._dropped.get(ch.name, ""))
                )
        finally:
            self._filling = False
        self._update_estimate()

    # --- editing --------------------------------------------------------------

    def _save(self):
        self._settings.set_datalog_channels(channels_to_saved(self._channels))
        self._update_estimate()

    def _selected_row(self) -> int:
        rows = self._table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._filling or item.column() != _COL_ON:
            return
        if self._datalogger.is_running or self._datalogger.push_running:
            # No edits during a run: put the box back as it was.
            self._filling = True
            try:
                on = self._channels[item.row()].enabled
                item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            finally:
                self._filling = False
            return
        self._channels[item.row()].enabled = item.checkState() == Qt.Checked
        self._save()

    def _on_every_changed(self, row: int, value: int):
        if self._filling:
            return
        self._channels[row].every = int(value)
        self._save()

    def _on_add(self):
        dialog = ChannelDialog(self, taken=[c.name for c in self._channels])
        if dialog.exec() == QDialog.Accepted and dialog.result_channel:
            self._channels.append(dialog.result_channel)
            self._save()
            self._fill_table()
            self._table.selectRow(len(self._channels) - 1)

    def _on_add_from_catalog(self):
        dialog = CatalogDialog(
            self, taken=[c.name for c in self._channels], existing=self._channels
        )
        if dialog.exec() == QDialog.Accepted and dialog.result_channels:
            first = len(self._channels)
            self._channels.extend(dialog.result_channels)
            self._save()
            self._fill_table()
            self._table.selectRow(first)
            self._table.scrollToBottom()
            n = len(dialog.result_channels)
            self._status.setText(
                f"Added {n} channel{'s' if n != 1 else ''} at the end of the list."
            )

    def _on_edit(self):
        row = self._selected_row()
        if row < 0 or self._datalogger.is_running:
            return
        ch = self._channels[row]
        if ch.builtin:
            QMessageBox.information(
                self,
                "Datalogger",
                "Built-in channels can only be turned on or off and polled less "
                "often. Add a custom channel to change how a value is read.",
            )
            return
        taken = [c.name for i, c in enumerate(self._channels) if i != row]
        dialog = ChannelDialog(self, channel=ch, taken=taken)
        if dialog.exec() == QDialog.Accepted and dialog.result_channel:
            new = dialog.result_channel
            new.enabled = ch.enabled
            self._channels[row] = new
            self._save()
            self._fill_table()
            self._table.selectRow(row)

    def _on_remove(self):
        row = self._selected_row()
        if row < 0 or self._channels[row].builtin:
            return
        del self._channels[row]
        self._save()
        self._fill_table()

    def _on_move(self, step: int):
        row = self._selected_row()
        if row >= 0:
            self._move_row(row, row + step)

    def _move_row(self, row: int, to: int):
        """Move one channel (buttons, keys or drag and drop)."""
        n = len(self._channels)
        if not (0 <= row < n and 0 <= to < n) or row == to:
            return
        if self._datalogger.is_running or self._datalogger.push_running:
            return
        self._channels.insert(to, self._channels.pop(row))
        # The Status column follows the channel name, so it moves with the row.
        self._save()
        self._fill_table()
        self._table.selectRow(to)

    def _update_estimate(self):
        rate = estimated_rows_per_second(self._channels)
        enabled = sum(1 for c in self._channels if c.enabled)
        self._estimate.setText(
            f"{enabled} channels, about {rate:.0f} rows/s (ECU limit, estimate)"
            if enabled
            else "No channel selected"
        )

    # --- run ------------------------------------------------------------------

    def _on_run_toggle(self):
        dl = self._datalogger
        if dl.is_running:
            self._btn_run.setEnabled(False)  # re-enabled by running_changed
            dl.stop()
            return
        self._dropped.clear()
        self._fill_table()
        dl.start()
        self._update_state()

    def _on_trail(self):
        if not self._datalogger.trail_in_mlv():
            self._status.setText("MegaLogViewerHD is not installed")

    def _on_open_folder(self):
        """Open the live captures folder; select the capture being written."""
        folder = live_capture_dir(self._settings.get_logs_directory())
        folder.mkdir(parents=True, exist_ok=True)
        capture = self._datalogger.current_capture
        if capture and sys.platform == "win32" and Path(capture).is_file():
            try:
                subprocess.Popen(["explorer", "/select,", str(Path(capture))])
                return
            except OSError:
                logger.debug("Explorer /select failed", exc_info=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_running_changed(self, running: bool):
        if running:
            self._status_timer.start()
        else:
            self._status_timer.stop()
        self._refresh_status()

    def _status_text(self, ch: Channel, values: dict, running: bool) -> str:
        if ch.name in self._dropped:
            return self._dropped[ch.name]  # a refused channel says why
        if not running or not ch.enabled:
            return ""
        if ch.name in values:
            return _status_value(values[ch.name], ch.unit)
        return "waiting for data"

    def _refresh_status(self):
        running = bool(self._datalogger.is_running)
        values = self._live.current() if self._live is not None and running else {}
        for row, ch in enumerate(self._channels):
            item = self._table.item(row, _COL_STATUS)
            if item is None:
                continue
            text = self._status_text(ch, values, running)
            if item.text() != text:
                item.setText(text)

    def _on_channel_dropped(self, name: str, why: str):
        self._dropped[name] = why
        for row, ch in enumerate(self._channels):
            if ch.name == name:
                self._table.item(row, _COL_STATUS).setText(why)

    # --- WiCAN push -----------------------------------------------------------

    def _on_push(self):
        if not any(c.enabled for c in self._channels):
            self._status.setText("Select at least one channel to save.")
            return
        # Read the WiCAN's list first and show what would change.
        if self._datalogger.preview_wican_push():
            self._status.setText("Reading the WiCAN's channel list...")

    def _on_push_preview(self, preview: dict):
        lines = [
            f"The WiCAN will log these {preview.get('count', 0)} channels "
            "from now on, on every trip, also without NC Flash."
        ]
        if preview.get("removed"):
            lines.append("It stops logging: " + ", ".join(preview["removed"]) + ".")
        if preview.get("added"):
            lines.append("It starts logging: " + ", ".join(preview["added"]) + ".")
        if preview.get("clashes"):
            lines.append(
                "Saved turned off, because the WiCAN already reads them from the "
                "CAN bus or calculates them: " + ", ".join(preview["clashes"]) + "."
            )
        if preview.get("orphaned"):
            lines.append(
                "These calculated channels lose an input and stay empty: "
                + ", ".join(preview["orphaned"])
                + "."
            )
        if not preview.get("rpm"):
            lines.append(
                "RPM is not selected, and the WiCAN does not read it from the CAN "
                "bus either. NC Flash will refuse this list: without RPM the WiCAN "
                "goes by battery voltage, and a trip can start and never end."
            )
        explain = not self._settings.get_wican_push_explained()
        yes, dont_explain = self._confirm_push(
            "\n\n".join(lines), explain, safe_default=bool(preview.get("rpm"))
        )
        if explain and dont_explain:
            self._settings.set_wican_push_explained(True)
        if yes and self._datalogger.push_to_wican():
            self._status.setText("Updating the WiCAN's channels...")
        elif not yes:
            self._status.setText("Nothing was changed.")

    def _confirm_push(self, changes: str, explain: bool, safe_default: bool):
        """Ask before updating; returns (update, don't explain again)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(_PUSH_TITLE)
        box.setText(changes)
        # Keep our own reference: box.checkBox() can hand back a bare QObject.
        check = QCheckBox("Don't explain this again") if explain else None
        if check is not None:
            box.setInformativeText(_PUSH_EXPLAIN)
            box.setCheckBox(check)
        update = box.addButton("Update WiCAN", QMessageBox.AcceptRole)
        cancel = box.addButton(QMessageBox.Cancel)
        # No RPM: the WiCAN could not tell the engine runs, so default to Cancel.
        box.setDefaultButton(update if safe_default else cancel)
        box.exec()
        ticked = check is not None and check.isChecked()
        return box.clickedButton() is update, ticked

    def _on_restore(self):
        backup = self._datalogger.last_backup
        if not backup:
            return
        answer = QMessageBox.question(
            self,
            "Put Back Its Previous List",
            "Put back the channel list the WiCAN had before NC Flash last updated "
            "it?\n\nIts current list is saved on this PC first.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes and self._datalogger.restore_wican_backup(backup):
            self._status.setText("Putting back the WiCAN's previous channels...")

    def _on_restore_changed(self, backup: str, why: str):
        answer = QMessageBox.warning(
            self,
            "Put Back Its Previous List",
            f"{why}\n\nPut back the older list anyway? What the WiCAN has now is "
            "saved on this PC first.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            self._datalogger.restore_wican_backup(backup, force=True)

    def _on_push_finished(self, ok: bool, message: str):
        self._status.setText(message)
        self._update_state()
        # Only a push that already changed the WiCAN (its backup was written)
        # may offer a restore, and only of that push's backup.
        backup = self._datalogger.failed_push_backup
        if ok or not backup:
            return
        answer = QMessageBox.warning(
            self,
            _PUSH_TITLE,
            f"{message}\n\nThe WiCAN may already have the new list. "
            "Put back the list it had before this change?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self._datalogger.restore_wican_backup(backup)

    # --- state ----------------------------------------------------------------

    def _update_state(self, *_args):
        dl = self._datalogger
        adapter = self._settings.get_ecu_adapter()
        self._adapter.setText(_ADAPTER_TEXT.get(adapter, _ADAPTER_TEXT["j2534"]))
        running = dl.is_running
        pushing = dl.push_running
        self._btn_run.setText("Stop Logging" if running else "Start Logging")
        if running:
            self._btn_run.setEnabled(True)  # it must always be stoppable
            self._btn_run.setToolTip("")
        else:
            # Always clickable: the ECU window's link can change while this
            # window shows; a refused start says why in the status line.
            self._btn_run.setEnabled(not pushing)
            self._btn_run.setToolTip(dl.blocked_reason() or "")
        self._btn_trail.setEnabled(bool(dl.current_capture))
        is_wican = adapter == "wican"
        self._own_list_box.setVisible(is_wican)
        # The saved list is not what a run uses, and the WiCAN would not
        # switch to it mid-run anyway.
        self._btn_push.setEnabled(is_wican and not pushing and not running)
        self._btn_restore.setVisible(bool(dl.last_backup))
        self._btn_restore.setEnabled(not pushing and not running)
        editable = not running and not pushing
        # Not setEnabled(False): that also freezes scrolling, and the Status
        # column (live values) must stay readable during a run.
        self._table.setDragEnabled(editable)
        for row in range(self._table.rowCount()):
            spin = self._table.cellWidget(row, _COL_EVERY)
            if spin is not None:
                spin.setEnabled(editable)
        self._btn_add.setEnabled(editable)
        self._btn_catalog.setEnabled(editable)
        row = self._selected_row()
        custom = row >= 0 and not self._channels[row].builtin
        self._btn_edit.setEnabled(editable and custom)
        self._btn_remove.setEnabled(editable and custom)
        self._btn_up.setEnabled(editable and row > 0)
        self._btn_down.setEnabled(editable and 0 <= row < len(self._channels) - 1)

    def showEvent(self, event):
        # Adapter or ECU window state may have changed while hidden.
        self._update_state()
        super().showEvent(event)
