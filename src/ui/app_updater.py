"""In-app updater: check GitHub for a newer release, download it, install it (GitHub issue 104).

One :class:`UpdateController` is owned by the main window (an owned
collaborator, not a mixin). It runs the check at launch (at most once a day,
opt-out in Settings) and from *Help > Check for Updates*, shows the
non-modal :class:`UpdateDialog` with the release notes, and drives the
download and install. The network and file work lives in
``src/utils/update_check.py``; every call runs on a worker QThread, so the GUI
thread is never blocked and an offline machine only gets an Activity Log line.

Safety: installing closes NC Flash. Quitting during an ECU read or flash can
brick the ECU, so :meth:`UpdateController.install` asks the main window's
``install_blocker`` before closing, and asks again after the close was
accepted. While it reports a reason, the install is refused outright. There
is no "install anyway" button.
"""

import logging
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from src.utils.constants import APP_NAME
from src.utils.transfer import format_size
from src.utils.update_check import (
    RELEASES_PAGE_URL,
    ReleaseInfo,
    UpdateCancelled,
    UpdateError,
    can_self_install,
    verify_installer,
    download_installer,
    fetch_latest_release,
    is_newer,
    is_release_build,
    launch_installer,
)

logger = logging.getLogger(__name__)

#: The startup check waits this long so it never competes with session
#: restore / ROM parsing for the first paint.
_STARTUP_CHECK_DELAY_MS = 5000

#: The startup check runs at most once per this period.
_STARTUP_CHECK_INTERVAL_S = 24 * 3600

#: Socket timeout for each installer read (a stalled download gives up).
_DOWNLOAD_TIMEOUT_S = 15.0

#: How long app exit waits for a check or download worker to stop. Longer
#: than every socket timeout above, so no QThread outlives the app.
_SHUTDOWN_WAIT_MS = 20000

#: Progress emits are throttled to this period.
_PROGRESS_MIN_INTERVAL_S = 0.1


def default_download_dir() -> Path:
    """Where installers are downloaded (a folder under the system temp dir)."""
    return Path(tempfile.gettempdir()) / "NCFlash-update"


class _CheckWorker(QObject):
    """Fetches the latest release off the GUI thread."""

    finished = Signal(object)  # ReleaseInfo
    error = Signal(str)

    def __init__(self, current_version: str):
        super().__init__()
        self._current_version = current_version

    def run(self):
        try:
            release = fetch_latest_release(self._current_version)
        except UpdateError as e:
            self.error.emit(str(e))
            return
        except Exception as e:  # never let a worker exception kill the thread
            logger.exception("Update check failed unexpectedly")
            self.error.emit(f"Unexpected error: {e}")
            return
        self.finished.emit(release)


class _DownloadWorker(QObject):
    """Downloads and verifies the installer off the GUI thread."""

    finished = Signal(str)  # path of the verified installer
    error = Signal(str)
    cancelled = Signal()
    progress = Signal(int, int)  # bytes done, bytes total

    def __init__(self, asset, dest_dir: Path, current_version: str):
        super().__init__()
        self.asset = asset
        self._dest_dir = dest_dir
        self._current_version = current_version
        self._last_progress_emit = 0.0

    def run(self):
        try:
            path = download_installer(
                self.asset,
                self._dest_dir,
                self._current_version,
                progress=self._on_progress,
                should_abort=self._abort_requested,
                timeout=_DOWNLOAD_TIMEOUT_S,
            )
        except UpdateCancelled:
            self.cancelled.emit()
            return
        except UpdateError as e:
            self.error.emit(str(e))
            return
        except Exception as e:
            logger.exception("Update download failed unexpectedly")
            self.error.emit(f"Unexpected error: {e}")
            return
        self.finished.emit(str(path))

    def _on_progress(self, done: int, total: int):
        now = time.monotonic()
        boundary = done == 0 or done >= total
        if not boundary and now - self._last_progress_emit < _PROGRESS_MIN_INTERVAL_S:
            return
        self._last_progress_emit = now
        self.progress.emit(done, total)

    @staticmethod
    def _abort_requested() -> bool:
        thread = QThread.currentThread()
        return bool(thread and thread.isInterruptionRequested())


class UpdateController(QObject):
    """Owns the update check, the update dialog, the download and the install.

    Args:
        settings: the AppSettings instance.
        current_version: the running version (``APP_VERSION``).
        install_blocker: returns a user-facing reason why NC Flash must not
            close right now (an ECU window is open, a trip-log download runs),
            or None when it is safe.
        close_app: closes the main window; returns False when the user
            cancelled (e.g. at the unsaved-changes prompt).
        dialog_parent: parent widget for the dialog and message boxes.
    """

    def __init__(
        self,
        settings,
        current_version: str,
        install_blocker: Callable[[], Optional[str]],
        close_app: Callable[[], bool],
        dialog_parent=None,
        download_dir: Optional[Path] = None,
    ):
        super().__init__(dialog_parent)
        self._settings = settings
        self._current_version = current_version
        self._install_blocker = install_blocker
        self._close_app = close_app
        self._dialog_parent = dialog_parent
        self._download_dir = download_dir or default_download_dir()

        self._check_thread = None
        self._check_worker = None
        self._check_manual = False
        self._download_thread = None
        self._download_worker = None
        self._dialog = None
        #: (path, InstallerAsset) of the last download verified this session.
        self._verified = None

        # Replaceable in tests.
        self._launch = launch_installer
        self._quit_app = QApplication.quit

    # --- public API ---------------------------------------------------------

    @property
    def is_checking(self) -> bool:
        return self._check_thread is not None

    @property
    def is_downloading(self) -> bool:
        return self._download_thread is not None

    def schedule_startup_check(
        self, delay_ms: Optional[int] = None, now: Optional[float] = None
    ) -> bool:
        """Arm the quiet launch check. Returns whether it was armed.

        Skipped for source builds, when turned off in Settings, and when the
        last check was less than a day ago.
        """
        if not is_release_build(self._current_version):
            logger.debug("Update check skipped: development build")
            return False
        if not self._settings.get_check_updates_on_startup():
            logger.debug("Update check at startup is turned off")
            return False
        now = time.time() if now is None else now
        last = self._settings.get_last_update_check()
        if 0 <= now - last < _STARTUP_CHECK_INTERVAL_S:
            logger.debug("Update check skipped: last check was under a day ago")
            return False
        if delay_ms is None:
            delay_ms = _STARTUP_CHECK_DELAY_MS
        QTimer.singleShot(delay_ms, self, lambda: self.check_for_updates(manual=False))
        return True

    def check_for_updates(self, manual: bool = True) -> bool:
        """Start a background check. Returns False if one is already running.

        A manual check reports every outcome in a dialog. The startup check
        is quiet: only a new, not-skipped release shows the update dialog.
        """
        if self.is_checking:
            if manual:
                self._check_manual = True
            return False
        self._check_manual = manual

        worker = _CheckWorker(self._current_version)
        thread = QThread(self)
        worker.moveToThread(thread)
        self._check_thread = thread
        self._check_worker = worker

        worker.finished.connect(self._on_check_finished, Qt.QueuedConnection)
        worker.error.connect(self._on_check_error, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(self._on_check_thread_finished, Qt.QueuedConnection)

        logger.info("Checking for %s updates...", APP_NAME)
        thread.start()
        return True

    def start_download(self, release: ReleaseInfo) -> bool:
        """Download and verify the release's installer in the background.

        Never installs by itself: when the download is verified the dialog
        offers *Install Now*, and only that click calls :meth:`install`.
        """
        if self.is_downloading or release.installer is None:
            return False
        reason = self._install_blocker()
        if reason:
            self._refuse(reason)
            return False

        worker = _DownloadWorker(
            release.installer, self._download_dir, self._current_version
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        self._download_thread = thread
        self._download_worker = worker

        worker.finished.connect(self._on_download_finished, Qt.QueuedConnection)
        worker.error.connect(self._on_download_error, Qt.QueuedConnection)
        worker.cancelled.connect(self._on_download_cancelled, Qt.QueuedConnection)
        worker.progress.connect(self._on_download_progress, Qt.QueuedConnection)
        thread.started.connect(worker.run)
        for done_signal in (worker.finished, worker.error, worker.cancelled):
            done_signal.connect(thread.quit)
        thread.finished.connect(self._on_download_thread_finished, Qt.QueuedConnection)

        self._verified = None
        logger.info(
            "Downloading %s (%s)...",
            release.installer.name,
            format_size(release.installer.size),
        )
        thread.start()
        if self._dialog is not None:
            self._dialog.set_downloading(True)
        return True

    def cancel_download(self):
        """Ask a running download to stop (returns at once)."""
        if self._download_thread is not None:
            self._download_thread.requestInterruption()

    def has_verified(self, asset) -> bool:
        """Whether *asset* is the installer downloaded and verified this session."""
        return asset is not None and (
            self._verified is not None and self._verified[1] == asset
        )

    def install(self, asset) -> bool:
        """Close NC Flash and start the verified installer (the Install Now click).

        Refused while ``install_blocker`` reports a reason or another dialog
        is open, before the close and again after it was accepted. The
        installer is re-hashed before each step, so only the bytes GitHub
        published are ever started. Returns True once it was started.
        """
        if not self.has_verified(asset):
            return False
        path = self._verified[0]
        reason = self._install_blocker() or self._modal_reason()
        if reason:
            self._refuse(reason)
            return False
        if not verify_installer(path, asset):
            self._installer_changed(path)
            return False

        logger.info("Closing %s to install the update", APP_NAME)
        if not self._close_app():
            logger.info("Update postponed: %s was not closed", APP_NAME)
            return False

        # The close ran prompts (unsaved changes) with their own event loop;
        # re-check that nothing started meanwhile.
        reason = self._install_blocker()
        if reason:
            logger.warning("Update not started: %s", reason)
            return False
        if not verify_installer(path, asset):
            self._installer_changed(path)
            return False

        try:
            self._launch(path)
        except OSError as e:
            # The main window is already closed, so the app still exits;
            # the opened folder lets the user run the installer by hand.
            logger.error(
                "Could not start the installer %s: %s. Run it from the folder "
                "that opens.",
                path,
                e,
            )
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
            return False
        logger.info("Installer started: %s", path.name)
        self._quit_app()
        return True

    def shutdown(self, timeout_ms: int = _SHUTDOWN_WAIT_MS):
        """Stop a running check or download before app exit.

        A download polls the interruption flag between 64 KiB chunks (each
        read has a 15 s timeout); a check is one GET with a 10 s timeout.
        """
        for thread in (self._download_thread, self._check_thread):
            if thread is None:
                continue
            thread.requestInterruption()
            thread.quit()
            if thread.isRunning() and not thread.wait(timeout_ms):
                logger.warning("Update worker did not stop within %d ms", timeout_ms)

    # --- check results (GUI thread) -----------------------------------------

    def _on_check_finished(self, release: ReleaseInfo):
        manual = self._check_manual
        if manual or not self._install_blocker():
            # A startup check held back by a busy app does not count, so
            # the next launch shows the update.
            self._settings.set_last_update_check(time.time())
        if not is_release_build(self._current_version):
            logger.info("Latest %s release: v%s", APP_NAME, release.version)
            if manual:
                QMessageBox.information(
                    self._dialog_parent,
                    "Check for Updates",
                    f"This is a development build, so it is not updated "
                    f"automatically.\n\nThe latest release is v{release.version}.",
                )
            return
        if not is_newer(release.version, self._current_version):
            logger.info("%s is up to date (v%s)", APP_NAME, self._current_version)
            if manual:
                QMessageBox.information(
                    self._dialog_parent,
                    "Check for Updates",
                    f"You have the latest version of {APP_NAME} "
                    f"(v{self._current_version}).",
                )
            return
        logger.info("%s v%s is available", APP_NAME, release.version)
        if not manual:
            if self._settings.get_skipped_update_version() == release.version:
                logger.info("v%s was skipped; not showing the update", release.version)
                return
            if self._install_blocker():
                # Never pop a dialog over a running ECU operation; the next
                # launch (or Help > Check for Updates) shows it.
                logger.info("Not showing the update now: NC Flash is busy")
                return
        self._show_dialog(release, manual=manual)

    def _on_check_error(self, message: str):
        logger.info("Update check failed: %s", message)
        if self._check_manual:
            QMessageBox.warning(
                self._dialog_parent,
                "Check for Updates",
                f"Could not check for updates.\n\n{message}",
            )

    def _on_check_thread_finished(self):
        thread, worker = self._check_thread, self._check_worker
        self._check_thread = None
        self._check_worker = None
        self._dispose(thread, worker)

    # --- download results (GUI thread) --------------------------------------

    def _download_dialog(self):
        """The dialog for the release being downloaded, if it is still open.

        A dialog for another version (opened after a cancelled download
        that was still finishing) never receives this download's results.
        """
        worker = self._download_worker
        dialog = self._dialog
        if worker is None or dialog is None:
            return None
        return dialog if dialog.release.installer == worker.asset else None

    def _on_download_progress(self, done: int, total: int):
        dialog = self._download_dialog()
        if dialog is not None:
            dialog.show_progress(done, total)

    def _on_download_finished(self, path: str):
        worker = self._download_worker
        if worker is None:
            return
        self._verified = (Path(path), worker.asset)
        logger.info("Update downloaded and verified: %s", Path(path).name)
        # No auto-install: a finished download must never close the app on
        # its own (a prompt or dialog may be open). The user clicks Install Now.
        dialog = self._download_dialog()
        if dialog is not None:
            dialog.set_downloaded()

    def _on_download_error(self, message: str):
        logger.warning("Update download failed: %s", message)
        dialog = self._download_dialog()
        if dialog is not None:
            dialog.show_failure(message)

    def _on_download_cancelled(self):
        logger.info("Update download cancelled")
        dialog = self._download_dialog()
        if dialog is not None:
            dialog.show_cancelled()

    def _on_download_thread_finished(self):
        thread, worker = self._download_thread, self._download_worker
        self._download_thread = None
        self._download_worker = None
        self._dispose(thread, worker)

    # --- helpers ------------------------------------------------------------

    def _show_dialog(self, release: ReleaseInfo, manual: bool = False):
        if self._dialog is not None and self._dialog.release.version == (
            release.version
        ):
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        if self.is_downloading:
            # Never open another version's dialog while a download (or its
            # cancellation) is still finishing.
            logger.info("Not showing v%s: a download is finishing", release.version)
            if manual:
                QMessageBox.information(
                    self._dialog_parent,
                    "Check for Updates",
                    f"{APP_NAME} v{release.version} is available. A download is "
                    "still finishing; try again in a moment.",
                )
            return
        if self._dialog is not None:
            self._dialog.close()
        dialog = UpdateDialog(
            self, release, self._current_version, parent=self._dialog_parent
        )
        # Clear the reference only if it still points at THIS dialog: a
        # replaced dialog is destroyed later, after the new one is current.
        dialog.destroyed.connect(lambda *_: self._forget_dialog(dialog))
        self._dialog = dialog
        dialog.show()

    def _forget_dialog(self, dialog):
        if self._dialog is dialog:
            self._dialog = None

    @staticmethod
    def _modal_reason() -> Optional[str]:
        if QApplication.activeModalWidget() is not None:
            return "Another NC Flash dialog is open. Close it first."
        return None

    def _installer_changed(self, path: Path):
        self._verified = None
        logger.error("Update not started: %s no longer matches its checksum", path)
        if self._dialog is not None:
            self._dialog.show_failure(
                "The downloaded installer changed on disk and was not started. "
                "Download it again."
            )

    def _refuse(self, reason: str):
        logger.warning("Update install refused: %s", reason)
        QMessageBox.warning(
            self._dialog or self._dialog_parent,
            "Can't Install Yet",
            f"{reason}\n\n{APP_NAME} must stay open until then. Try again "
            "once it is done.",
        )

    def skip_version(self, version: str):
        self._settings.set_skipped_update_version(version)
        logger.info("Update v%s skipped", version)

    @staticmethod
    def _dispose(thread, worker):
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()


class UpdateDialog(QDialog):
    """Non-modal "update available" dialog: release notes + download/install."""

    def __init__(
        self,
        controller: UpdateController,
        release: ReleaseInfo,
        current_version: str,
        parent=None,
    ):
        super().__init__(parent)
        self.release = release
        self._controller = controller
        self._can_install = release.installer is not None and can_self_install()

        self.setWindowTitle("Update Available")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(False)
        self.resize(560, 460)

        layout = QVBoxLayout(self)

        headline = QLabel(
            f"<b>{APP_NAME} v{release.version} is available.</b> "
            f"You have v{current_version}."
        )
        headline.setTextFormat(Qt.RichText)
        layout.addWidget(headline)

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "No release notes.")
        layout.addWidget(notes, 1)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        if not self._can_install:
            if release.installer is None:
                self._show_status(
                    "This release has no verified installer to download here. "
                    "Get it from the release page."
                )
            else:
                self._show_status(
                    "Automatic install is only available in the Windows "
                    "installer build. Get the new version from the release page."
                )

        buttons = QHBoxLayout()
        skip_btn = QPushButton("Skip This Version")
        skip_btn.setToolTip("Don't show this version again at startup")
        skip_btn.clicked.connect(self._on_skip)
        buttons.addWidget(skip_btn)
        page_btn = QPushButton("View on GitHub")
        page_btn.clicked.connect(self._on_open_page)
        buttons.addWidget(page_btn)
        buttons.addStretch(1)
        self._later_btn = QPushButton("Later")
        self._later_btn.clicked.connect(self.close)
        buttons.addWidget(self._later_btn)
        self._install_btn = QPushButton("Download && Install")
        self._install_btn.setDefault(True)
        self._install_btn.setVisible(self._can_install)
        self._install_btn.clicked.connect(self._on_install)
        buttons.addWidget(self._install_btn)
        layout.addLayout(buttons)

    # --- controller callbacks -----------------------------------------------

    def set_downloading(self, downloading: bool):
        self._install_btn.setEnabled(not downloading)
        self._later_btn.setText("Cancel" if downloading else "Later")
        if downloading:
            self._progress.setRange(0, 0)
            self._progress.setVisible(True)
            self._show_status("Downloading...")

    def show_progress(self, done: int, total: int):
        if total > 0:
            self._progress.setRange(0, 1000)
            self._progress.setValue(int(done * 1000 / total))
        self._show_status(f"Downloading... {format_size(done)} of {format_size(total)}")

    def set_downloaded(self):
        self.set_downloading(False)
        self._progress.setVisible(False)
        self._install_btn.setText("Install Now")
        self._show_status(
            f"Downloaded and verified. Install Now closes {APP_NAME} (you'll be "
            "asked about unsaved changes) and starts the installer."
        )

    def show_failure(self, message: str):
        self._install_btn.setText("Download && Install")
        self.set_downloading(False)
        self._progress.setVisible(False)
        self._show_status(f"Download failed: {message}")

    def show_cancelled(self):
        self.set_downloading(False)
        self._progress.setVisible(False)
        self._show_status("Download cancelled.")

    # --- buttons ------------------------------------------------------------

    def _on_install(self):
        if self._controller.has_verified(self.release.installer):
            self._controller.install(self.release.installer)
        else:
            self._controller.start_download(self.release)

    def _on_skip(self):
        self._controller.skip_version(self.release.version)
        self.close()

    def _on_open_page(self):
        QDesktopServices.openUrl(QUrl(self.release.page_url or RELEASES_PAGE_URL))

    def done(self, result):
        """Single close chokepoint (Later / Cancel / X / Esc): stop a download."""
        if self._controller.is_downloading:
            self._controller.cancel_download()
        super().done(result)

    def _show_status(self, text: str):
        self._status.setText(text)
        self._status.setVisible(True)
