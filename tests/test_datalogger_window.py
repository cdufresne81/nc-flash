"""Datalogger window: channel table editing, run toggle, WiCAN push surface."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject, Qt, Signal

from src.core.datalog_channels import MODE_MEM, builtin_channels, channels_from_saved
from src.ui import datalogger_window as win_mod
from src.ui.datalogger_window import ChannelDialog, DataloggerWindow


class _FakeDatalogger(QObject):
    running_changed = Signal(bool)
    status_changed = Signal(str)
    capture_changed = Signal(str)
    channel_dropped = Signal(str, str)
    push_running_changed = Signal(bool)
    push_preview = Signal(dict)
    push_finished = Signal(bool, str)

    def __init__(self, settings):
        super().__init__()
        self._settings = settings
        self.is_running = False
        self.push_running = False
        self.current_capture = ""
        self.last_backup = ""
        self.failed_push_backup = ""
        self.start = MagicMock(return_value=True)
        self.stop = MagicMock()
        self.trail_in_mlv = MagicMock(return_value=True)
        self.push_to_wican = MagicMock(return_value=True)
        self.preview_wican_push = MagicMock(return_value=True)
        self.restore_wican_backup = MagicMock(return_value=True)

    def channels(self):
        return channels_from_saved(self._settings.saved)

    def blocked_reason(self):
        return None


def _window(qtbot, adapter="j2534"):
    settings = MagicMock()
    settings.saved = {}
    settings.get_ecu_adapter.return_value = adapter
    settings.get_datalog_channels.side_effect = lambda: settings.saved
    settings.set_datalog_channels.side_effect = lambda d: setattr(settings, "saved", d)
    settings.get_live_datalog_auto_trail.return_value = True
    dl = _FakeDatalogger(settings)
    live_cursor = SimpleNamespace(enabled=True, set_enabled=MagicMock())
    main_window = SimpleNamespace(
        settings=settings, datalogger=dl, live_cursor=live_cursor
    )
    window = DataloggerWindow(main_window=main_window)
    qtbot.addWidget(window)
    return window, dl, settings


def _row(window, name):
    return next(i for i, c in enumerate(window._channels) if c.name == name)


def test_table_lists_the_catalog_with_the_default_selection(qtbot):
    window, _, _ = _window(qtbot)
    assert window._table.rowCount() == len(builtin_channels())
    on = window._table.item(_row(window, "RPM"), win_mod._COL_ON)
    assert on.checkState() == Qt.Checked
    off = window._table.item(_row(window, "VSS"), win_mod._COL_ON)
    assert off.checkState() == Qt.Unchecked
    assert "rows/s" in window._estimate.text()


def test_toggling_and_every_n_are_saved(qtbot):
    window, _, settings = _window(qtbot)
    row = _row(window, "VSS")
    window._table.item(row, win_mod._COL_ON).setCheckState(Qt.Checked)
    assert settings.saved["builtin"]["VSS"]["enabled"] is True
    window._table.cellWidget(row, win_mod._COL_EVERY).setValue(4)
    assert settings.saved["builtin"]["VSS"]["every"] == 4


def test_run_button_starts_and_stops(qtbot):
    window, dl, _ = _window(qtbot)
    window._btn_run.click()
    dl.start.assert_called_once()
    dl.is_running = True
    dl.running_changed.emit(True)
    assert window._btn_run.text() == "Stop Logging"
    assert not window._table.isEnabled()  # no edits during a run
    window._btn_run.click()
    dl.stop.assert_called_once()


def test_push_button_only_for_the_wican(qtbot):
    window, _, _ = _window(qtbot, adapter="j2534")
    assert window._btn_push.isHidden()
    window, dl, _ = _window(qtbot, adapter="wican")
    assert not window._btn_push.isHidden()
    window._btn_push.click()
    dl.preview_wican_push.assert_called_once()  # read first, never push blind
    dl.push_to_wican.assert_not_called()


@pytest.mark.parametrize("answer,pushed", [("yes", True), ("cancel", False)])
def test_push_needs_confirming_the_preview(qtbot, monkeypatch, answer, pushed):
    window, dl, _ = _window(qtbot, adapter="wican")
    shown = []

    def question(parent, title, text, buttons, default):
        shown.append((text, default))
        return (
            win_mod.QMessageBox.Yes if answer == "yes" else win_mod.QMessageBox.Cancel
        )

    monkeypatch.setattr(win_mod.QMessageBox, "question", question)
    dl.push_preview.emit(
        {
            "removed": ["MAP", "BARO"],
            "added": [],
            "orphaned": ["BOOST"],
            "rpm": False,
            "count": 9,
        }
    )
    text, default = shown[0]
    assert "MAP, BARO" in text and "BOOST" in text and "RPM" in text
    assert default == win_mod.QMessageBox.Cancel  # no RPM: default is not to send
    assert dl.push_to_wican.called is pushed


def test_restore_button_after_a_push(qtbot):
    window, dl, _ = _window(qtbot, adapter="wican")
    assert window._btn_restore.isHidden()
    dl.last_backup = "C:/logs/auto_pid_x.json"
    dl.push_finished.emit(True, "The WiCAN now polls the new channel list.")
    assert not window._btn_restore.isHidden()
    window._btn_restore.click()
    dl.restore_wican_backup.assert_called_once_with("C:/logs/auto_pid_x.json")


def test_dropped_channel_shows_why(qtbot):
    window, dl, _ = _window(qtbot)
    dl.channel_dropped.emit("KNOCKR", "the ECU doesn't know this identifier")
    status = window._table.item(_row(window, "KNOCKR"), win_mod._COL_STATUS)
    assert status.text() == "the ECU doesn't know this identifier"


def test_push_failure_offers_to_restore_the_backup(qtbot, monkeypatch):
    window, dl, _ = _window(qtbot, adapter="wican")
    dl.last_backup = dl.failed_push_backup = "C:/logs/auto_pid_x.json"
    asked = []

    def warning(parent, title, text, buttons, default):
        asked.append(default)
        return win_mod.QMessageBox.Yes

    monkeypatch.setattr(win_mod.QMessageBox, "warning", warning)
    dl.push_finished.emit(False, "The WiCAN can't poll 1 of the channels")
    dl.restore_wican_backup.assert_called_once_with("C:/logs/auto_pid_x.json")
    assert asked == [win_mod.QMessageBox.No]  # never restore on a bare Enter


def test_failure_that_changed_nothing_never_offers_a_restore(qtbot, monkeypatch):
    window, dl, _ = _window(qtbot, adapter="wican")
    dl.last_backup = "C:/logs/older_successful_push.json"
    dl.failed_push_backup = ""
    asked = []
    monkeypatch.setattr(win_mod.QMessageBox, "warning", lambda *a: asked.append(a))
    dl.push_finished.emit(False, "timed out")
    assert asked == []
    dl.restore_wican_backup.assert_not_called()


def test_builtin_rows_cannot_be_edited_or_removed(qtbot):
    window, _, _ = _window(qtbot)
    window._table.selectRow(_row(window, "RPM"))
    assert not window._btn_edit.isEnabled()
    assert not window._btn_remove.isEnabled()


def _fill(dialog, **fields):
    dialog._name.setText(fields.get("name", "VOLEFF"))
    dialog._mode.setCurrentIndex(dialog._mode.findData(fields.get("mode", MODE_MEM)))
    dialog._ident.setText(fields.get("ident", "FFFFAC18"))
    dialog._size.setValue(fields.get("size", 4))
    dialog._kind.setCurrentText(fields.get("kind", "float"))
    dialog._formula.setText(fields.get("formula", "x*100"))


def test_channel_dialog_builds_a_custom_channel(qtbot):
    dialog = ChannelDialog(taken=["RPM"])
    qtbot.addWidget(dialog)
    _fill(dialog)
    dialog._on_ok()
    ch = dialog.result_channel
    assert (ch.name, ch.mode, ch.ident, ch.kind) == (
        "VOLEFF",
        MODE_MEM,
        0xFFFFAC18,
        "float",
    )
    assert ch.formula == "x*100" and ch.builtin is False


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "RPM"},
        {"ident": "zz"},
        {"formula": "x**2"},
        {"kind": "unsigned", "size": 4, "ident": "1"},
    ],
)
def test_channel_dialog_refuses_bad_input(qtbot, monkeypatch, fields):
    warned = []
    monkeypatch.setattr(win_mod.QMessageBox, "warning", lambda *a: warned.append(a))
    dialog = ChannelDialog(taken=["RPM"])
    qtbot.addWidget(dialog)
    _fill(dialog, **fields)
    if fields.get("kind") == "unsigned":
        dialog._offset.setValue(3)  # 4 bytes past offset 3 overflows the frame
    dialog._on_ok()
    assert dialog.result_channel is None and warned


def test_custom_channel_add_and_remove(qtbot, monkeypatch):
    window, _, settings = _window(qtbot)

    class _Accepting(ChannelDialog):
        def exec(self):
            _fill(self)
            self._on_ok()
            return win_mod.QDialog.Accepted

    monkeypatch.setattr(win_mod, "ChannelDialog", _Accepting)
    window._on_add()
    assert [c["name"] for c in settings.saved["custom"]] == ["VOLEFF"]
    assert window._btn_remove.isEnabled()
    window._on_remove()
    assert settings.saved["custom"] == []


def test_open_live_logs_folder_creates_and_opens_it(qtbot, tmp_path, monkeypatch):
    window, dl, settings = _window(qtbot)
    settings.get_logs_directory.return_value = str(tmp_path / "logs")
    dl.current_capture = ""
    opened = []
    monkeypatch.setattr(
        win_mod.QDesktopServices, "openUrl", lambda url: opened.append(url)
    )
    window._btn_folder.click()
    live = tmp_path / "logs" / "live"
    assert live.is_dir()
    assert [u.toLocalFile() for u in opened] == [live.as_posix()]


def test_open_live_logs_folder_selects_the_capture_being_written(
    qtbot, tmp_path, monkeypatch
):
    window, dl, settings = _window(qtbot)
    settings.get_logs_directory.return_value = str(tmp_path / "logs")
    capture = tmp_path / "logs" / "live" / "live_1.csv"
    capture.parent.mkdir(parents=True)
    capture.write_text("Time,RPM\n")
    dl.current_capture = str(capture)
    calls = []
    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    monkeypatch.setattr(win_mod.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(
        win_mod.QDesktopServices, "openUrl", lambda url: calls.append(url)
    )
    window._btn_folder.click()
    assert calls == [["explorer", "/select,", str(capture)]]
