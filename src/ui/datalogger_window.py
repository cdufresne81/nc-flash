"""Datalogger window: pick channels, start/stop a live log, follow it in MLV.

A thin surface over the main-window-owned :class:`~src.ui.datalogger.Datalogger`
(closing the window stops nothing). The channel list it edits is saved in
settings at every change; the owner reads it when a run starts or when the
list is sent to the WiCAN.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
    channels_to_saved,
    estimated_rows_per_second,
)
from src.ui import theme

logger = logging.getLogger(__name__)

_COLS = ["Log", "Name", "Mode", "ID", "Unit", "Every", "Formula", "Status"]
_COL_ON, _COL_NAME, _COL_MODE, _COL_ID, _COL_UNIT, _COL_EVERY, _COL_FORMULA, _COL_STATUS = range(8)  # fmt: skip

_ADAPTER_TEXT = {
    "j2534": "Tactrix OpenPort (J2534): NC Flash polls the ECU through the cable.",
    "obdx": "OBDX Pro: NC Flash polls the ECU through the adapter.",
    "wican": (
        "WiCAN: the WiCAN polls the ECU itself and NC Flash follows the trip it "
        "records. Send the channels to the WiCAN to change what it logs."
    ),
}


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
        self._update_state()

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

        self._table = QTableWidget(0, len(_COLS))
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
        root.addWidget(self._table, stretch=1)

        edit_row = QHBoxLayout()
        self._btn_add = QPushButton("Add Channel...")
        self._btn_add.clicked.connect(self._on_add)
        edit_row.addWidget(self._btn_add)
        self._btn_edit = QPushButton("Edit...")
        self._btn_edit.clicked.connect(self._on_edit)
        edit_row.addWidget(self._btn_edit)
        self._btn_remove = QPushButton("Remove")
        self._btn_remove.clicked.connect(self._on_remove)
        edit_row.addWidget(self._btn_remove)
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
        self._btn_push = QPushButton("Send Channels to WiCAN")
        self._btn_push.setToolTip(
            "Replace the channel list the WiCAN polls and records with this one. "
            "The WiCAN's previous list is saved first and can be put back."
        )
        self._btn_push.clicked.connect(self._on_push)
        run_row.addWidget(self._btn_push)
        self._btn_restore = QPushButton("Restore WiCAN's Previous Channels")
        self._btn_restore.setToolTip(
            "Put back the channel list the WiCAN had before the last send"
        )
        self._btn_restore.clicked.connect(self._on_restore)
        run_row.addWidget(self._btn_restore)
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

    def _on_channel_dropped(self, name: str, why: str):
        self._dropped[name] = why
        for row, ch in enumerate(self._channels):
            if ch.name == name:
                self._table.item(row, _COL_STATUS).setText(why)

    # --- WiCAN push -----------------------------------------------------------

    def _on_push(self):
        if not any(c.enabled for c in self._channels):
            self._status.setText("Select at least one channel to send.")
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
        if preview.get("orphaned"):
            lines.append(
                "These calculated channels lose an input and stay empty: "
                + ", ".join(preview["orphaned"])
                + "."
            )
        if not preview.get("rpm"):
            lines.append(
                "RPM is not selected: the WiCAN uses RPM to tell that the engine "
                "runs, so it would start and stop trips on battery voltage only."
            )
        lines.append("Its current list is saved first, and Restore puts it back.")
        answer = QMessageBox.question(
            self,
            "Send Channels to WiCAN",
            "\n\n".join(lines),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel if not preview.get("rpm") else QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes and self._datalogger.push_to_wican():
            self._status.setText("Sending the channels to the WiCAN...")
        elif answer != QMessageBox.Yes:
            self._status.setText("Nothing was sent.")

    def _on_restore(self):
        backup = self._datalogger.last_backup
        if backup and self._datalogger.restore_wican_backup(backup):
            self._status.setText("Putting back the WiCAN's previous channels...")

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
            "Send Channels to WiCAN",
            f"{message}\n\nThe WiCAN may already have the new list. "
            "Put back the list it had before this send?",
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
        self._btn_push.setVisible(is_wican)
        self._btn_push.setEnabled(is_wican and not pushing)
        self._btn_restore.setVisible(is_wican and bool(dl.last_backup))
        self._btn_restore.setEnabled(not pushing)
        editable = not running and not pushing
        self._table.setEnabled(editable)
        self._btn_add.setEnabled(editable)
        row = self._selected_row()
        custom = row >= 0 and not self._channels[row].builtin
        self._btn_edit.setEnabled(editable and custom)
        self._btn_remove.setEnabled(editable and custom)

    def showEvent(self, event):
        # Adapter or ECU window state may have changed while hidden.
        self._update_state()
        super().showEvent(event)
