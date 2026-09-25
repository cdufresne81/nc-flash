"""Open datalog CSVs in MegaLogViewerHD (GitHub issue 109).

THE single MLV-launch pipeline. (The parked live-datalog branch carries a
``mlv_trail`` launcher for Trail Live File mode; when it lands it must fold
into this module rather than add a second launcher.)

Behavior of the installed MLV HD 4.6.06, from its decompiled launcher
(``MegaLogViewer.main``) and verified with live launches on 2026-09-24:

- Plain CSV paths passed as arguments are ALL loaded, in order, into ONE
  window as a single concatenated log; MLV marks each file boundary
  ("beginning of <name>, Time will be inconsistent"). This is the same path
  as a multi-select File > Open.
- The concatenation maps each later file's values by column POSITION against
  the first file's header — logs with different column sets would come out
  misaligned. So files are grouped by identical header line and each group
  gets its own launch (its own window).
- Every launch is a separate JVM (no single-instance forwarding in 4.6.06),
  so launching once per file would cost one window and up to 10 GB heap each.
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QCheckBox, QMessageBox

logger = logging.getLogger(__name__)

#: Standard Windows install locations (64-bit first). Checked in order.
_CANDIDATES = (
    Path(r"C:\Program Files\EFIAnalytics\MegaLogViewerHD\MegaLogViewerHD.exe"),
    Path(r"C:\Program Files (x86)\EFIAnalytics\MegaLogViewerHD\MegaLogViewerHD.exe"),
)


def find_mlv() -> Optional[Path]:
    """The installed MegaLogViewerHD executable, or None when not installed."""
    for candidate in _CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _header_line(path: Path) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readline().rstrip("\r\n")
    except OSError:
        return None


def group_by_header(paths) -> list:
    """Split *paths* into launch groups of files sharing the same CSV header.

    Order is preserved inside and across groups (first appearance of each
    header). An unreadable file gets a group of its own rather than being
    concatenated blindly.
    """
    groups = {}
    for path in map(Path, paths):
        header = _header_line(path)
        key = header if header is not None else object()
        groups.setdefault(key, []).append(path)
    return list(groups.values())


def _ask(parent, count: int) -> tuple:
    """Modal Yes/No with a "Don't ask again" box → ``(open, dont_ask_again)``."""
    plural, pronoun = ("s", "them") if count != 1 else ("", "it")
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle("Open in MegaLogViewerHD")
    box.setText(
        f"{count} trip log{plural} downloaded.\n\n"
        f"Open {pronoun} in MegaLogViewerHD?"
    )
    if count > 1:
        box.setInformativeText(
            "The logs open together in one window, oldest first, with a "
            "marker where each trip starts."
        )
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.Yes)
    dont_ask = QCheckBox("Don't ask again")
    box.setCheckBox(dont_ask)
    return box.exec() == QMessageBox.Yes, dont_ask.isChecked()


def offer_to_open(parent, paths, settings, exe: Optional[Path] = None) -> bool:
    """After a download: ask to open the new logs (oldest first) in MLV.

    Silent no-op — no dialog — when nothing was downloaded, the user turned
    the offer off, or MegaLogViewerHD is not installed. "Don't ask again"
    turns the Settings toggle off (re-enable it under ECU > WiCAN). Returns
    True when MLV was launched.
    """
    paths = list(paths)
    if not paths or not settings.get_offer_open_logs_in_mlv():
        return False
    exe = exe or find_mlv()
    if exe is None:
        logger.debug("MegaLogViewerHD not installed: no open-logs offer")
        return False
    wants_open, dont_ask_again = _ask(parent, len(paths))
    if dont_ask_again:
        settings.set_offer_open_logs_in_mlv(False)
        logger.info(
            "MegaLogViewerHD offer turned off (re-enable in Settings > ECU > WiCAN)"
        )
    if not wants_open:
        return False
    return open_logs(paths, exe)


def open_logs(paths, exe: Optional[Path] = None) -> bool:
    """Open *paths* (oldest first) in MegaLogViewerHD.

    One launch per header group — normally a single window holding every log
    concatenated in the given order. Returns True when every launch started;
    False (with a log line, never a dialog) when MLV is not installed, there
    is nothing to open, or a launch failed.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        return False
    exe = exe or find_mlv()
    if exe is None:
        logger.info(
            "MegaLogViewerHD not found (looked in %s)",
            " and ".join(str(c.parent) for c in _CANDIDATES),
        )
        return False

    ok = True
    for group in group_by_header(paths):
        # PySide6 maps the C++ pid out-parameter to a (success, pid) tuple —
        # a bare truthiness check would read a failed (False, 0) as success.
        started, _pid = QProcess.startDetached(
            str(exe), [str(p.resolve()) for p in group], str(exe.parent)
        )
        names = ", ".join(p.name for p in group)
        if started:
            logger.info("Opened in MegaLogViewerHD: %s", names)
        else:
            logger.warning("MegaLogViewerHD failed to launch (%s) for %s", exe, names)
            ok = False
    return ok
