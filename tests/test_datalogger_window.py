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
    # No edits during a run, but the table stays scrollable for live values.
    assert window._table.isEnabled() and not window._table.dragEnabled()
    assert not window._table.cellWidget(0, win_mod._COL_EVERY).isEnabled()
    window._btn_run.click()
    dl.stop.assert_called_once()


def test_push_button_only_for_the_wican(qtbot):
    window, _, _ = _window(qtbot, adapter="j2534")
    assert window._own_list_box.isHidden()
    window, dl, _ = _window(qtbot, adapter="wican")
    assert not window._own_list_box.isHidden()
    window._btn_push.click()
    dl.preview_wican_push.assert_called_once()  # read first, never push blind
    dl.push_to_wican.assert_not_called()


def test_wican_looks_like_a_cable_and_saving_its_list_is_optional(qtbot):
    window, dl, _ = _window(qtbot, adapter="wican")
    hint = window._adapter.text()
    assert "Start Logging sends these channels" in hint and "this run only" in hint
    # The saved list lives in its own section, which says Start Logging leaves it alone.
    assert "never changes it" in win_mod._OWN_LIST_TEXT
    assert "NC Flash's, not the one stored on the WiCAN" in win_mod._OWN_LIST_TEXT
    assert window._btn_push.text() == "Update WiCAN Channels..."
    assert window._btn_push.parent() is window._own_list_box
    assert window._btn_run.text() == "Start Logging" and window._btn_run.isEnabled()
    assert window._btn_push.isEnabled()
    # A run uses its own temporary list: the saved one can't change meanwhile.
    dl.last_backup = "C:/logs/auto_pid_x.json"
    dl.is_running = True
    dl.running_changed.emit(True)
    assert not window._btn_push.isEnabled() and not window._btn_restore.isEnabled()
    assert window._btn_run.text() == "Stop Logging" and window._btn_run.isEnabled()
    dl.is_running = False
    dl.running_changed.emit(False)
    assert window._btn_push.isEnabled() and window._btn_restore.isEnabled()


@pytest.mark.parametrize("answer,pushed", [("yes", True), ("cancel", False)])
def test_push_needs_confirming_the_preview(qtbot, monkeypatch, answer, pushed):
    window, dl, settings = _window(qtbot, adapter="wican")
    settings.get_wican_push_explained.return_value = False
    shown = []

    def confirm(text, explain, safe_default):
        shown.append((text, safe_default, explain))
        return answer == "yes", True

    monkeypatch.setattr(window, "_confirm_push", confirm)
    dl.push_preview.emit(
        {
            "removed": ["MAP", "BARO"],
            "added": [],
            "orphaned": ["BOOST"],
            "rpm": False,
            "count": 9,
        }
    )
    text, safe_default, explain = shown[0]
    assert "MAP, BARO" in text and "BOOST" in text and "RPM" in text
    assert safe_default is False  # no RPM: default is not to send
    assert explain is True
    settings.set_wican_push_explained.assert_called_once_with(True)
    assert dl.push_to_wican.called is pushed


def test_explanation_can_be_turned_off_but_changes_still_show(qtbot, monkeypatch):
    window, dl, settings = _window(qtbot, adapter="wican")
    settings.get_wican_push_explained.return_value = True
    shown = []

    def confirm(text, explain, safe_default):
        shown.append((text, explain))
        return True, False

    monkeypatch.setattr(window, "_confirm_push", confirm)
    dl.push_preview.emit({"removed": ["MAP"], "added": [], "rpm": True, "count": 9})
    text, explain = shown[0]
    assert explain is False and "MAP" in text  # the changes are never hidden
    settings.set_wican_push_explained.assert_not_called()
    dl.push_to_wican.assert_called_once()


def test_confirm_dialog_has_the_explanation_and_the_checkbox(qtbot, monkeypatch):
    window, _, _ = _window(qtbot, adapter="wican")
    seen = {}

    def fake_exec(box):
        seen["info"] = box.informativeText()
        box.findChild(win_mod.QCheckBox).setChecked(True)
        return 0

    monkeypatch.setattr(win_mod.QMessageBox, "exec", fake_exec)
    yes, dont = window._confirm_push("changes", True, safe_default=True)
    assert "Start Logging already sends" in seen["info"]
    assert dont is True and yes is False  # ticked, but nothing clicked


@pytest.mark.parametrize("answer,called", [("yes", True), ("cancel", False)])
def test_restore_button_asks_first(qtbot, monkeypatch, answer, called):
    window, dl, _ = _window(qtbot, adapter="wican")
    assert window._btn_restore.isHidden()
    dl.last_backup = "C:/logs/auto_pid_x.json"
    dl.push_finished.emit(True, "The WiCAN now polls the new channel list.")
    assert not window._btn_restore.isHidden()
    yes = win_mod.QMessageBox.Yes if answer == "yes" else win_mod.QMessageBox.Cancel
    monkeypatch.setattr(win_mod.QMessageBox, "question", lambda *a, **k: yes)
    window._btn_restore.click()
    assert dl.restore_wican_backup.called is called
    if called:
        dl.restore_wican_backup.assert_called_once_with("C:/logs/auto_pid_x.json")


@pytest.mark.parametrize("answer,forced", [("yes", True), ("cancel", False)])
def test_changed_list_needs_a_second_yes(qtbot, monkeypatch, answer, forced):
    window, dl, _ = _window(qtbot, adapter="wican")
    yes = win_mod.QMessageBox.Yes if answer == "yes" else win_mod.QMessageBox.Cancel
    monkeypatch.setattr(win_mod.QMessageBox, "warning", lambda *a, **k: yes)
    window._on_restore_changed("C:/logs/b.json", "The WiCAN's channel list changed.")
    if forced:
        dl.restore_wican_backup.assert_called_once_with("C:/logs/b.json", force=True)
    else:
        dl.restore_wican_backup.assert_not_called()


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


def test_move_up_and_down_reorders_and_is_saved(qtbot):
    window, dl, settings = _window(qtbot)
    first, second = window._channels[0].name, window._channels[1].name
    window._table.selectRow(0)
    assert not window._btn_up.isEnabled() and window._btn_down.isEnabled()
    window._btn_down.click()
    assert [c.name for c in window._channels[:2]] == [second, first]
    assert window._selected_row() == 1  # the selection follows the channel
    assert settings.saved["order"][:2] == [second, first]
    assert [c.name for c in dl.channels()][:2] == [second, first]  # what a run uses
    window._btn_up.click()
    assert [c.name for c in window._channels[:2]] == [first, second]


def test_moving_is_off_during_a_run(qtbot):
    window, dl, _ = _window(qtbot)
    window._table.selectRow(1)
    dl.is_running = True
    dl.running_changed.emit(True)
    assert not window._btn_up.isEnabled() and not window._btn_down.isEnabled()


def test_drag_and_drop_moves_a_channel_and_its_settings(qtbot):
    window, dl, settings = _window(qtbot)
    names = [c.name for c in window._channels]
    row = _row(window, "ECT")
    every = window._channels[row].every
    window._table.row_moved.emit(row, 0)  # what a drop on the first row reports
    assert window._channels[0].name == "ECT" and window._channels[0].every == every
    assert window._table.cellWidget(0, win_mod._COL_EVERY).value() == every
    assert window._table.item(0, win_mod._COL_NAME).text() == "ECT"
    assert sorted(c.name for c in window._channels) == sorted(names)  # nothing lost
    assert settings.saved["order"][0] == "ECT"
    assert window._selected_row() == 0


def test_drop_target_counts_the_row_leaving_its_place(qtbot):
    window, _, _ = _window(qtbot)
    window.resize(900, 700)
    window.show()
    table = window._table
    moved = []
    table.row_moved.connect(lambda a, b: moved.append((a, b)))
    rect = table.visualRect(table.model().index(3, 0))
    # Lower half of row 3 means "after row 3".
    assert table.drop_target(rect.center() + type(rect.center())(0, 3)) == 4
    assert table.drop_target(rect.center() - type(rect.center())(0, 3)) == 3


def test_no_drag_moves_during_a_run(qtbot):
    window, dl, _ = _window(qtbot)
    first = window._channels[0].name
    dl.is_running = True
    window._table.row_moved.emit(0, 3)
    assert window._channels[0].name == first


def _catalog():
    from src.core.datalog_channels import catalog_channels

    return catalog_channels()


def test_catalog_dialog_greys_out_channels_already_in_the_list(qtbot):
    dlg = win_mod.CatalogDialog(None, catalog=_catalog(), taken=["RPM", "ect"])
    qtbot.addWidget(dlg)
    names = [c.name for c in dlg._catalog]
    rpm = dlg._table.item(names.index("RPM"), 0)
    assert not rpm.flags() & Qt.ItemIsUserCheckable
    assert not dlg._table.item(names.index("ECT"), 0).flags() & Qt.ItemIsUserCheckable
    assert not dlg._ok.isEnabled()  # nothing ticked yet


def test_catalog_dialog_search_and_add(qtbot):
    dlg = win_mod.CatalogDialog(None, catalog=_catalog(), taken=[])
    qtbot.addWidget(dlg)
    dlg._search.setText("fan relay")
    shown = [
        c.name for r, c in enumerate(dlg._catalog) if not dlg._table.isRowHidden(r)
    ]
    assert shown and all(n.startswith("FAN") for n in shown)
    names = [c.name for c in dlg._catalog]
    dlg._toggle_row(names.index("FAN1_SW"), 1)
    dlg._table.item(names.index("GEN_V"), 0).setCheckState(Qt.Checked)
    assert dlg._ok.text() == "Add 2 Channels"
    dlg._on_ok()
    added = {c.name: c for c in dlg.result_channels}
    assert set(added) == {"FAN1_SW", "GEN_V"}
    assert all(c.enabled and not c.builtin for c in added.values())


def test_add_from_catalog_appends_and_saves(qtbot, monkeypatch):
    window, dl, settings = _window(qtbot)
    picked = [c for c in _catalog() if c.name == "GEN_V"]

    class _Fake:
        def __init__(self, *a, **k):
            from dataclasses import replace

            self.result_channels = [replace(picked[0], enabled=True, builtin=False)]

        def exec(self):
            return win_mod.QDialog.Accepted

    monkeypatch.setattr(win_mod, "CatalogDialog", _Fake)
    before = len(window._channels)
    window._btn_catalog.click()
    assert len(window._channels) == before + 1 and window._channels[-1].name == "GEN_V"
    assert any(d.get("name") == "GEN_V" for d in settings.saved["custom"])
    assert [c.name for c in dl.channels()][-1] == "GEN_V"  # what a run uses


@pytest.mark.parametrize("adapter", ["j2534", "obdx", "wican"])
def test_catalog_picker_is_offered_for_every_adapter(qtbot, adapter):
    window, _, _ = _window(qtbot, adapter=adapter)
    window.show()
    assert window._btn_catalog.isVisible() and window._btn_catalog.isEnabled()


def test_catalog_spots_the_same_reading_under_another_name(qtbot):
    from src.core.datalog_channels import builtin_channels

    mine = builtin_channels()  # has VCT_ACT (0x22 0x16CD) and FUELSYS (x*0.00390625)
    dlg = win_mod.CatalogDialog(
        None, catalog=_catalog(), taken=[c.name for c in mine], existing=mine
    )
    qtbot.addWidget(dlg)
    names = [c.name for c in dlg._catalog]
    vt = names.index("VT_ACT")
    assert not dlg._table.item(vt, 0).flags() & Qt.ItemIsUserCheckable
    assert "VCT_ACT" in dlg._table.item(vt, 0).toolTip()
    assert dlg._table.item(vt, 1).text() == "VT_ACT  (= VCT_ACT)"
    # TP1 reads the same as the built-in TPS, constant differs only at the 8th decimal.
    assert not dlg._table.item(names.index("TP1"), 0).flags() & Qt.ItemIsUserCheckable


def test_catalog_same_bytes_other_formula_stays_pickable_with_a_note(qtbot):
    from dataclasses import replace

    tp1 = next(c for c in _catalog() if c.name == "TP1")
    raw = replace(tp1, name="MY_TPS_RAW", formula="x", builtin=False)  # 0x01 0x11, raw
    dlg = win_mod.CatalogDialog(
        None, catalog=_catalog(), taken=[raw.name], existing=[raw]
    )
    qtbot.addWidget(dlg)
    row = [c.name for c in dlg._catalog].index("TP1")
    assert dlg._table.item(row, 0).flags() & Qt.ItemIsUserCheckable
    assert "MY_TPS_RAW" in dlg._table.item(row, 0).toolTip()
    assert "different formula" in dlg._table.item(row, 0).toolTip()


def test_same_formula_handles_a_division():
    from src.core.datalog_channels import Channel

    a = Channel(name="A", mode=1, ident=0x11, formula="100/x")
    b = Channel(name="B", mode=1, ident=0x11, formula="100 / x")
    assert win_mod._same_formula(a, b)  # x=0 must not make them differ


def test_status_shows_live_values_during_a_run(qtbot):
    from src.ui.live_values import LiveValues

    window, dl, _ = _window(qtbot)
    # The fake has no live values; give it the real holder both engines feed.
    dl.live_values = LiveValues(dl)
    window._live = dl.live_values
    rpm, ect = _row(window, "RPM"), _row(window, "ECT")
    status = lambda r: window._table.item(r, win_mod._COL_STATUS).text()
    dl.is_running = True
    dl.running_changed.emit(True)
    assert status(rpm) == "waiting for data"
    dl.live_values.update({"RPM": 812.25, "ECT": 88.0}, expire=False)
    window._refresh_status()
    assert status(rpm) == "812 rpm" and status(ect) == "88.0 C"
    window._on_channel_dropped("ECT", "the ECU doesn't know this identifier")
    window._refresh_status()
    assert status(ect) == "the ECU doesn't know this identifier"  # a reason wins
    dl.is_running = False
    dl.running_changed.emit(False)
    assert status(rpm) == ""  # no stale value after the run
    assert status(ect) == "the ECU doesn't know this identifier"


def test_status_value_format():
    f = win_mod._status_value
    assert f(1, "on/off") == "on" and f(0, "on / off") == "off"
    assert f(0.21, "g/rev") == "0.21 g/rev" and f(14.7, "") == "14.7"
    assert f(-9.84, "PSI") == "-9.84 PSI" and f(3000.4, "rpm") == "3000 rpm"


def test_checkbox_edits_are_undone_during_a_run(qtbot):
    window, dl, settings = _window(qtbot)
    row = _row(window, "VSS")
    dl.is_running = True
    dl.running_changed.emit(True)
    window._table.item(row, win_mod._COL_ON).setCheckState(Qt.Checked)
    assert window._table.item(row, win_mod._COL_ON).checkState() == Qt.Unchecked
    assert window._channels[row].enabled is False


def test_window_opened_during_a_run_shows_live_values(qtbot):
    from src.ui.live_values import LiveValues

    settings = MagicMock()
    settings.saved = {}
    settings.get_ecu_adapter.return_value = "j2534"
    settings.get_datalog_channels.side_effect = lambda: settings.saved
    settings.get_live_datalog_auto_trail.return_value = True
    dl = _FakeDatalogger(settings)
    dl.live_values = LiveValues(dl)
    dl.is_running = True  # already logging when the window opens
    main_window = SimpleNamespace(
        settings=settings,
        datalogger=dl,
        live_cursor=SimpleNamespace(enabled=True, set_enabled=MagicMock()),
    )
    window = DataloggerWindow(main_window=main_window)
    qtbot.addWidget(window)
    assert window._status_timer.isActive()
    dl.live_values.update({"RPM": 900.0}, expire=False)
    window._refresh_status()
    assert (
        window._table.item(_row(window, "RPM"), win_mod._COL_STATUS).text() == "900 rpm"
    )
