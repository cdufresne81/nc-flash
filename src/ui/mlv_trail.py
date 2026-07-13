"""Launch MegaLogViewerHD trailing a live capture (fw issue #3 follow-up).

MLV's documented automation hook (TunerStudio support manual 123, "Launching
MegaLogViewer with a properties file") is a *properties file* passed as the
executable's single argument::

    fileName=C:/…/logs/live/live_20260711_125123.csv
    trailFile=true
    startPlayback=true

``trailFile=true`` is the "Trail Live File" mode (keep loading while the file
grows); ``startPlayback=true`` starts playback from the most recent sample.
Verified against the installed build's own bytecode (class ``ax/bL`` holds
exactly these keys). THE single MLV-launch pipeline — the live-datalog owner
calls this; nothing else spawns MLV.

Java ``.properties`` parsing treats backslash as an escape character, so the
``fileName`` value is always written with forward slashes.
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess

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


def write_trail_properties(csv_path: Path) -> Path:
    """Write the MLV launch properties next to the capture and return its path.

    Sits beside the ``.csv`` (same stem) so a capture and the launcher that
    trails it travel together — easy to inspect, easy to relaunch by hand.
    """
    props_path = csv_path.with_suffix(".mlv.properties")
    file_name = str(csv_path).replace("\\", "/")
    props_path.write_text(
        f"fileName={file_name}\ntrailFile=true\nstartPlayback=true\n",
        encoding="utf-8",
    )
    return props_path


def launch_trail(csv_path: Path, exe: Optional[Path] = None) -> bool:
    """Open ``csv_path`` in MegaLogViewerHD in Trail Live File mode.

    Returns True when the detached process was started. False (with a log
    line, never a dialog) when MLV is not installed or the launch failed.
    """
    exe = exe or find_mlv()
    if exe is None:
        logger.info(
            "MegaLogViewerHD not found (looked in %s)",
            " and ".join(str(c.parent) for c in _CANDIDATES),
        )
        return False
    props_path = write_trail_properties(csv_path)
    # PySide6 maps the C++ pid out-parameter to a (success, pid) tuple here —
    # a bare truthiness check would read a failed (False, 0) as success.
    result = QProcess.startDetached(str(exe), [str(props_path)])
    started = bool(result[0]) if isinstance(result, tuple) else bool(result)
    if started:
        logger.info("Opened %s in MegaLogViewerHD (trail mode)", csv_path.name)
    else:
        logger.warning("MegaLogViewerHD failed to launch (%s)", exe)
    return started
