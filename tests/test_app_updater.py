"""Tests for the in-app updater controller and dialog (src/ui/app_updater.py).

The network layer is replaced by fakes; the QThreads are real, so a thread
lifecycle bug (destroyed while running) fails the test.
"""

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QDialog

import src.ui.app_updater as app_updater
from main import MainWindow
from src.ui.app_updater import UpdateController
from src.utils.update_check import InstallerAsset, ReleaseInfo, UpdateError

INSTALLER = InstallerAsset(
    name="NCFlash-2.18.0-Setup.exe",
    url="https://github.com/cdufresne81/nc-flash/releases/download/v2.18.0/"
    "NCFlash-2.18.0-Setup.exe",
    size=1000,
    sha256="a" * 64,
)


def _release(version="2.18.0", installer=INSTALLER):
    return ReleaseInfo(
        version=version,
        tag=f"v{version}",
        notes="### Added\n- Things",
        page_url="https://github.com/cdufresne81/nc-flash/releases/tag/v" + version,
        installer=installer,
    )


@pytest.fixture
def qt_thread_guard():
    """Fail the test if Qt reports a QThread destroyed while running."""
    messages = []

    def handler(mode, ctx, msg):
        messages.append(msg)

    old = qInstallMessageHandler(handler)
    yield
    qInstallMessageHandler(old)
    aborts = [m for m in messages if "Destroyed while thread" in m]
    assert not aborts, f"QThread lifecycle violation: {aborts}"


@pytest.fixture
def settings():
    s = MagicMock()
    s.get_check_updates_on_startup.return_value = True
    s.get_last_update_check.return_value = 0.0
    s.get_skipped_update_version.return_value = ""
    return s


@pytest.fixture
def boxes(monkeypatch):
    """Record QMessageBox calls instead of showing them."""
    calls = []
    box = app_updater.QMessageBox
    for name in ("information", "warning"):
        monkeypatch.setattr(
            box,
            name,
            staticmethod(lambda *a, _n=name, **k: calls.append((_n, a[1], a[2]))),
        )
    return calls


@pytest.fixture
def make_controller(qtbot, settings, tmp_path, qt_thread_guard):
    created = []

    def make(version="2.17.0", blocker=None, close_ok=True):
        ctl = UpdateController(
            settings,
            version,
            install_blocker=blocker or (lambda: None),
            close_app=MagicMock(return_value=close_ok),
            dialog_parent=None,
            download_dir=tmp_path / "dl",
        )
        ctl._launch = MagicMock()
        ctl._quit_app = MagicMock()
        created.append(ctl)
        return ctl

    yield make
    for ctl in created:
        if ctl._dialog is not None:
            ctl._dialog.close()
        ctl.shutdown()


def _fake_fetch(monkeypatch, result):
    def fetch(current_version):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_updater, "fetch_latest_release", fetch)


def _wait_check(qtbot, ctl):
    qtbot.waitUntil(lambda: not ctl.is_checking, timeout=5000)


def _wait_download(qtbot, ctl):
    qtbot.waitUntil(lambda: not ctl.is_downloading, timeout=5000)


# --- startup check gating --------------------------------------------------


class TestStartupCheck:
    def test_skipped_for_dev_build(self, make_controller):
        assert make_controller(version="dev").schedule_startup_check(0) is False

    def test_skipped_when_turned_off(self, make_controller, settings):
        settings.get_check_updates_on_startup.return_value = False
        assert make_controller().schedule_startup_check(0) is False

    def test_skipped_when_checked_within_a_day(self, make_controller, settings):
        settings.get_last_update_check.return_value = 1_000_000.0
        ctl = make_controller()
        assert ctl.schedule_startup_check(0, now=1_000_000.0 + 3600) is False

    def test_runs_after_a_day_and_shows_new_release(
        self, qtbot, make_controller, settings, monkeypatch, boxes
    ):
        _fake_fetch(monkeypatch, _release())
        settings.get_last_update_check.return_value = 1_000_000.0
        ctl = make_controller()
        assert ctl.schedule_startup_check(0, now=1_000_000.0 + 90_000) is True
        qtbot.waitUntil(lambda: ctl._dialog is not None, timeout=5000)
        assert ctl._dialog.release.version == "2.18.0"
        settings.set_last_update_check.assert_called_once()
        assert boxes == []

    def test_quiet_when_up_to_date(self, qtbot, make_controller, monkeypatch, boxes):
        _fake_fetch(monkeypatch, _release(version="2.17.0"))
        ctl = make_controller()
        ctl.check_for_updates(manual=False)
        _wait_check(qtbot, ctl)
        assert ctl._dialog is None
        assert boxes == []

    def test_quiet_when_offline(self, qtbot, make_controller, monkeypatch, boxes):
        _fake_fetch(monkeypatch, UpdateError("Could not reach GitHub"))
        ctl = make_controller()
        ctl.check_for_updates(manual=False)
        _wait_check(qtbot, ctl)
        assert ctl._dialog is None
        assert boxes == []

    def test_skipped_version_stays_quiet_at_startup(
        self, qtbot, make_controller, settings, monkeypatch
    ):
        _fake_fetch(monkeypatch, _release())
        settings.get_skipped_update_version.return_value = "2.18.0"
        ctl = make_controller()
        ctl.check_for_updates(manual=False)
        _wait_check(qtbot, ctl)
        assert ctl._dialog is None


# --- manual check ----------------------------------------------------------


class TestManualCheck:
    def test_up_to_date_is_reported(self, qtbot, make_controller, monkeypatch, boxes):
        _fake_fetch(monkeypatch, _release(version="2.17.0"))
        ctl = make_controller()
        ctl.check_for_updates(manual=True)
        _wait_check(qtbot, ctl)
        assert boxes and boxes[0][0] == "information"
        assert "latest version" in boxes[0][2]

    def test_error_is_reported(self, qtbot, make_controller, monkeypatch, boxes):
        _fake_fetch(monkeypatch, UpdateError("Could not reach GitHub"))
        ctl = make_controller()
        ctl.check_for_updates(manual=True)
        _wait_check(qtbot, ctl)
        assert boxes and boxes[0][0] == "warning"
        assert "Could not reach GitHub" in boxes[0][2]

    def test_skipped_version_still_shown(
        self, qtbot, make_controller, settings, monkeypatch
    ):
        _fake_fetch(monkeypatch, _release())
        settings.get_skipped_update_version.return_value = "2.18.0"
        ctl = make_controller()
        ctl.check_for_updates(manual=True)
        qtbot.waitUntil(lambda: ctl._dialog is not None, timeout=5000)

    def test_dev_build_reports_latest_without_dialog(
        self, qtbot, make_controller, monkeypatch, boxes
    ):
        _fake_fetch(monkeypatch, _release())
        ctl = make_controller(version="dev")
        ctl.check_for_updates(manual=True)
        _wait_check(qtbot, ctl)
        assert ctl._dialog is None
        assert "development build" in boxes[0][2]


# --- install safety --------------------------------------------------------


def _verified(ctl, tmp_path, payload=b"MZ installer bytes"):
    """Give *ctl* a real verified download, as a finished download would."""
    path = tmp_path / "NCFlash-2.18.0-Setup.exe"
    path.write_bytes(payload)
    asset = InstallerAsset(
        name=path.name,
        url=INSTALLER.url,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    ctl._verified = (path, asset)
    return path


def _asset_of(ctl):
    return ctl._verified[1] if ctl._verified else INSTALLER


class TestInstall:
    def test_nothing_to_install_without_a_verified_download(self, make_controller):
        ctl = make_controller()
        assert ctl.install(_asset_of(ctl)) is False
        ctl._close_app.assert_not_called()

    def test_refuses_an_installer_for_another_version(self, make_controller, tmp_path):
        # verified 2.18 earlier; the open dialog is now for 2.19
        ctl = make_controller()
        _verified(ctl, tmp_path)
        assert ctl.install(INSTALLER) is False  # different asset
        ctl._close_app.assert_not_called()
        ctl._launch.assert_not_called()

    def test_refused_while_blocked(self, make_controller, boxes, tmp_path):
        ctl = make_controller(blocker=lambda: "The ECU Programming window is open")
        _verified(ctl, tmp_path)
        assert ctl.install(_asset_of(ctl)) is False
        ctl._close_app.assert_not_called()
        ctl._launch.assert_not_called()
        ctl._quit_app.assert_not_called()
        assert boxes[0][0] == "warning"
        assert "ECU Programming" in boxes[0][2]

    def test_refused_while_another_dialog_is_modal(
        self, qtbot, make_controller, boxes, tmp_path
    ):
        # e.g. the app's own "Unsaved Changes" prompt, or Settings / Save As
        modal = QDialog()
        qtbot.addWidget(modal)
        modal.setModal(True)
        modal.show()
        qtbot.waitUntil(lambda: QApplication.activeModalWidget() is modal)
        ctl = make_controller()
        _verified(ctl, tmp_path)
        assert ctl.install(_asset_of(ctl)) is False
        ctl._close_app.assert_not_called()
        ctl._launch.assert_not_called()
        assert "dialog is open" in boxes[0][2]

    def test_nothing_launched_when_close_is_cancelled(self, make_controller, tmp_path):
        ctl = make_controller(close_ok=False)
        _verified(ctl, tmp_path)
        assert ctl.install(_asset_of(ctl)) is False
        ctl._close_app.assert_called_once()
        ctl._launch.assert_not_called()
        ctl._quit_app.assert_not_called()

    def test_blocker_rechecked_after_close(self, make_controller, tmp_path, boxes):
        answers = iter([None, "An ECU operation started"])
        ctl = make_controller(blocker=lambda: next(answers))
        _verified(ctl, tmp_path)
        assert ctl.install(_asset_of(ctl)) is False
        ctl._close_app.assert_called_once()
        ctl._launch.assert_not_called()
        ctl._quit_app.assert_not_called()

    def test_installer_changed_before_click_is_not_started(
        self, make_controller, tmp_path
    ):
        ctl = make_controller()
        path = _verified(ctl, tmp_path)
        path.write_bytes(b"MZ tampered bytes!")  # same size, different hash
        assert ctl.install(_asset_of(ctl)) is False
        ctl._close_app.assert_not_called()
        ctl._launch.assert_not_called()

    def test_installer_changed_during_close_is_not_started(
        self, make_controller, tmp_path
    ):
        ctl = make_controller()
        path = _verified(ctl, tmp_path)
        ctl._close_app.side_effect = lambda: path.write_bytes(b"MZ tampered bytes!")
        assert ctl.install(_asset_of(ctl)) is False
        ctl._launch.assert_not_called()
        ctl._quit_app.assert_not_called()

    def test_closes_then_launches_then_quits(self, make_controller, tmp_path):
        order = []
        ctl = make_controller()
        path = _verified(ctl, tmp_path)
        ctl._close_app.side_effect = lambda: order.append("close") or True
        ctl._launch.side_effect = lambda p: order.append(("launch", p))
        ctl._quit_app.side_effect = lambda: order.append("quit")
        assert ctl.install(_asset_of(ctl)) is True
        assert order == ["close", ("launch", path), "quit"]

    def test_launch_failure_opens_folder_without_explicit_quit(
        self, make_controller, tmp_path, monkeypatch
    ):
        opened = []
        monkeypatch.setattr(
            app_updater.QDesktopServices, "openUrl", staticmethod(opened.append)
        )
        ctl = make_controller()
        _verified(ctl, tmp_path)
        ctl._launch.side_effect = OSError("The operation was canceled by the user")
        assert ctl.install(_asset_of(ctl)) is False
        ctl._quit_app.assert_not_called()
        assert Path(opened[0].toLocalFile()) == tmp_path


# --- download --------------------------------------------------------------


class TestDownload:
    def test_download_refused_while_blocked(self, make_controller, boxes, monkeypatch):
        started = []
        monkeypatch.setattr(
            app_updater, "download_installer", lambda *a, **k: started.append(1)
        )
        ctl = make_controller(blocker=lambda: "A WiCAN trip-log download is running")
        assert ctl.start_download(_release()) is False
        assert not ctl.is_downloading
        assert started == []
        assert boxes[0][0] == "warning"

    def test_no_installer_means_no_download(self, make_controller):
        assert make_controller().start_download(_release(installer=None)) is False

    def test_finished_download_never_installs_by_itself(
        self, qtbot, make_controller, monkeypatch, tmp_path
    ):
        target = tmp_path / "dl" / INSTALLER.name

        def fake_download(asset, dest_dir, current_version, **kwargs):
            kwargs["progress"](0, asset.size)
            kwargs["progress"](asset.size, asset.size)
            return target

        monkeypatch.setattr(app_updater, "download_installer", fake_download)
        monkeypatch.setattr(app_updater, "can_self_install", lambda: True)
        ctl = make_controller()
        ctl._show_dialog(_release())
        assert ctl.start_download(_release()) is True
        _wait_download(qtbot, ctl)
        assert ctl._verified == (target, INSTALLER)
        ctl._close_app.assert_not_called()
        ctl._launch.assert_not_called()
        assert ctl._dialog._install_btn.text() == "Install Now"
        assert ctl._dialog._install_btn.isEnabled()

    def test_closing_dialog_cancels_download(
        self, qtbot, make_controller, monkeypatch, settings
    ):
        started = threading.Event()

        def slow_download(asset, dest_dir, current_version, **kwargs):
            started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if kwargs["should_abort"]():
                    raise app_updater.UpdateCancelled("Download cancelled.")
                time.sleep(0.01)
            raise AssertionError("download was never cancelled")

        monkeypatch.setattr(app_updater, "download_installer", slow_download)
        monkeypatch.setattr(app_updater, "can_self_install", lambda: True)
        ctl = make_controller()
        ctl._show_dialog(_release())
        dialog = ctl._dialog
        dialog._on_install()
        assert started.wait(2)
        dialog.reject()  # Esc / X / Cancel all end in done()
        _wait_download(qtbot, ctl)
        qtbot.waitUntil(lambda: ctl._dialog is None, timeout=2000)
        assert ctl._verified is None
        ctl._launch.assert_not_called()
        ctl._close_app.assert_not_called()


# --- startup dialog vs a busy app ------------------------------------------


def test_startup_check_stays_quiet_while_blocked(
    qtbot, make_controller, monkeypatch, boxes
):
    _fake_fetch(monkeypatch, _release())
    ctl = make_controller(blocker=lambda: "The ECU Programming window is open")
    ctl.check_for_updates(manual=False)
    _wait_check(qtbot, ctl)
    assert ctl._dialog is None
    assert boxes == []


def test_manual_check_shows_dialog_even_while_blocked(
    qtbot, make_controller, monkeypatch
):
    _fake_fetch(monkeypatch, _release())
    ctl = make_controller(blocker=lambda: "The ECU Programming window is open")
    ctl.check_for_updates(manual=True)
    qtbot.waitUntil(lambda: ctl._dialog is not None, timeout=5000)


# --- the main window's blocker ---------------------------------------------


class TestMainWindowBlocker:
    """MainWindow._update_install_blocker: the rule the whole install obeys."""

    @staticmethod
    def _call(ecu_window=None, log_sync_running=False):
        fake = SimpleNamespace(
            ecu_window=ecu_window,
            wican_log_sync=SimpleNamespace(is_running=log_sync_running),
        )
        return MainWindow._update_install_blocker(fake)

    def test_safe_when_nothing_runs(self):
        assert self._call() is None

    def test_any_open_ecu_window_blocks_even_when_idle(self):
        idle = SimpleNamespace(is_busy=False)
        reason = self._call(ecu_window=idle)
        assert reason and "ECU Programming window" in reason
        assert "finished" in reason  # tells the user to wait, not just close

    def test_running_trip_log_download_blocks(self):
        reason = self._call(log_sync_running=True)
        assert reason and "trip-log download" in reason


# --- settings --------------------------------------------------------------


def test_last_check_timestamp_round_trips_exactly(tmp_path):
    from PySide6.QtCore import QSettings

    from src.utils.settings import AppSettings

    s = AppSettings.__new__(AppSettings)
    s.settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    assert s.get_last_update_check() == 0.0
    s.set_last_update_check(1790366283.039193)
    s.settings.sync()
    fresh = AppSettings.__new__(AppSettings)
    fresh.settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    assert fresh.get_last_update_check() == 1790366283.039193


# --- dialog ----------------------------------------------------------------


class TestDialog:
    def test_install_button_hidden_without_installer(self, make_controller):
        ctl = make_controller()
        ctl._show_dialog(_release(installer=None))
        assert ctl._dialog._install_btn.isHidden()

    def test_install_button_hidden_when_not_self_installable(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app_updater, "can_self_install", lambda: False)
        ctl = make_controller()
        ctl._show_dialog(_release())
        assert ctl._dialog._install_btn.isHidden()

    def test_install_button_shown_for_installer_build(
        self, make_controller, monkeypatch
    ):
        monkeypatch.setattr(app_updater, "can_self_install", lambda: True)
        ctl = make_controller()
        ctl._show_dialog(_release())
        assert not ctl._dialog._install_btn.isHidden()

    def test_skip_records_version_and_closes(self, qtbot, make_controller, settings):
        ctl = make_controller()
        ctl._show_dialog(_release())
        ctl._dialog._on_skip()
        settings.set_skipped_update_version.assert_called_once_with("2.18.0")
        qtbot.waitUntil(lambda: ctl._dialog is None, timeout=2000)

    def test_replacing_the_dialog_keeps_the_new_reference(self, qtbot, make_controller):
        ctl = make_controller()
        ctl._show_dialog(_release("2.18.0"))
        ctl._show_dialog(_release("2.19.0"))
        newest = ctl._dialog
        # let the old dialog's deferred destroy run
        qtbot.wait(50)
        assert ctl._dialog is newest
        assert newest.release.version == "2.19.0"


# --- second-review regressions ---------------------------------------------


def test_late_download_result_never_reaches_another_versions_dialog(
    qtbot, make_controller, monkeypatch, tmp_path
):
    """A cancelled 2.18 download that still finishes must not arm a 2.19 dialog."""
    release_gate = threading.Event()
    target = tmp_path / "dl" / INSTALLER.name

    def finishing_download(asset, dest_dir, current_version, **kwargs):
        release_gate.wait(5)  # ignores the cancel: it was already hashing
        return target

    monkeypatch.setattr(app_updater, "download_installer", finishing_download)
    monkeypatch.setattr(app_updater, "can_self_install", lambda: True)
    ctl = make_controller()
    ctl._show_dialog(_release("2.18.0"))
    ctl._dialog._on_install()
    ctl._dialog.reject()  # cancel; the worker keeps going
    qtbot.waitUntil(lambda: ctl._dialog is None, timeout=2000)

    # While it finishes, a check for another version opens no dialog.
    ctl._show_dialog(_release("2.19.0", installer=None))
    assert ctl._dialog is None

    release_gate.set()
    _wait_download(qtbot, ctl)
    newer = _release(
        "2.19.0",
        installer=InstallerAsset(
            name="NCFlash-2.19.0-Setup.exe",
            url=INSTALLER.url.replace("2.18.0", "2.19.0"),
            size=1000,
            sha256="b" * 64,
        ),
    )
    ctl._show_dialog(newer)
    dialog = ctl._dialog
    assert dialog._install_btn.text() == "Download && Install"
    assert not ctl.has_verified(newer.installer)
    ctl._launch.assert_not_called()


def test_manual_check_during_a_download_says_so(
    qtbot, make_controller, monkeypatch, boxes
):
    gate = threading.Event()
    monkeypatch.setattr(
        app_updater, "download_installer", lambda *a, **k: gate.wait(5) and None
    )
    ctl = make_controller()
    assert ctl.start_download(_release("2.18.0")) is True
    ctl._show_dialog(_release("2.19.0"), manual=True)
    assert boxes and "still finishing" in boxes[-1][2]
    gate.set()
    _wait_download(qtbot, ctl)


def test_startup_check_held_back_by_busy_app_is_not_recorded(
    qtbot, make_controller, monkeypatch, settings
):
    _fake_fetch(monkeypatch, _release())
    ctl = make_controller(blocker=lambda: "The ECU Programming window is open")
    ctl.check_for_updates(manual=False)
    _wait_check(qtbot, ctl)
    settings.set_last_update_check.assert_not_called()


def test_failed_check_is_not_recorded(qtbot, make_controller, monkeypatch, settings):
    _fake_fetch(monkeypatch, UpdateError("Could not reach GitHub"))
    ctl = make_controller()
    ctl.check_for_updates(manual=False)
    _wait_check(qtbot, ctl)
    settings.set_last_update_check.assert_not_called()
