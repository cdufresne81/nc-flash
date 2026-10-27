"""
Tactrix OpenPort 2.0 Windows driver state and the WinUSB switch.

NC Flash's built-in OpenPort driver (see j2534.find_builtin_openport_dll) needs
the cable bound to Microsoft's WinUSB driver. This module reads which Windows
driver the plugged-in cable uses (no admin rights needed) and builds the
command that runs packaging/openport/openport_winusb.ps1 elevated (one UAC
prompt) to switch the cable to WinUSB or back to Tactrix's driver. The script's
text is passed encoded on the command line, so the elevated process runs what
NC Flash read, not a file in a folder the user can write to.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import ntpath
import os
import sys
import uuid
from ctypes import byref, c_ulong, create_unicode_buffer
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .j2534 import BUILTIN_OPENPORT_DIR, find_builtin_openport_dll

logger = logging.getLogger(__name__)

OPENPORT_HARDWARE_ID = r"USB\VID_0403&PID_CC4D"
SETUP_SCRIPT = "openport_winusb.ps1"

# Exit codes of the elevated run (the script's own, plus 2 from the launcher).
EXIT_OK, EXIT_FAILED, EXIT_DECLINED, EXIT_RESTART = 0, 1, 2, 3
EXIT_LAUNCH_FAILED, EXIT_BUSY = 4, 5

# cfgmgr32 constants
_CR_SUCCESS = 0
_CR_BUFFER_SMALL = 0x1A
_CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
_CM_GETIDLIST_FILTER_PRESENT = 0x00000100
_CM_DRP_SERVICE = 0x00000005
_DN_HAS_PROBLEM = 0x00000400
_CM_PROB_FAILED_INSTALL = 28
_CM_PROB_DRIVER_FAILED_LOAD = 39
_CM_PROB_DRIVER_BLOCKED = 48
_CM_PROB_UNSIGNED_DRIVER = 52

# Command lines are limited to 32767 characters.
_MAX_COMMAND_CHARS = 30000


class OpenPortDriver(Enum):
    """Which Windows driver the plugged-in OpenPort uses."""

    NOT_FOUND = "not_found"  # not plugged in (or not Windows)
    WINUSB = "winusb"  # ready for NC Flash's built-in driver
    TACTRIX = "tactrix"  # Tactrix's own driver, working
    BLOCKED = "blocked"  # its driver blocked by Windows (Code 39/48/52)
    NO_DRIVER = "no_driver"  # no driver installed (Code 28)
    OTHER = "other"  # any other driver or problem


@dataclass(frozen=True)
class OpenPortState:
    driver: OpenPortDriver
    service: Optional[str] = None
    problem: int = 0

    @property
    def needs_winusb(self) -> bool:
        """True when switching to WinUSB is what makes the cable usable."""
        return self.driver in (OpenPortDriver.BLOCKED, OpenPortDriver.NO_DRIVER)


def classify(service: Optional[str], problem: int) -> OpenPortDriver:
    """Map a device's service name and problem code to an OpenPortDriver."""
    svc = (service or "").lower()
    if problem in (
        _CM_PROB_DRIVER_FAILED_LOAD,
        _CM_PROB_DRIVER_BLOCKED,
        _CM_PROB_UNSIGNED_DRIVER,
    ):
        return OpenPortDriver.BLOCKED
    if problem == _CM_PROB_FAILED_INSTALL or (not svc and problem == 0):
        return OpenPortDriver.NO_DRIVER
    if problem:
        return OpenPortDriver.OTHER
    if svc == "winusb":
        return OpenPortDriver.WINUSB
    if svc == "openport":
        return OpenPortDriver.TACTRIX
    return OpenPortDriver.OTHER


def detect_openport() -> OpenPortState:
    """Read the plugged-in OpenPort's Windows driver state (no admin rights)."""
    if sys.platform != "win32":
        return OpenPortState(OpenPortDriver.NOT_FOUND)
    try:
        states = []
        for instance in _find_present_instances():
            service, problem = _read_devnode(instance)
            states.append(OpenPortState(classify(service, problem), service, problem))
    except OSError as e:
        logger.warning("Could not read the OpenPort's driver state: %s", e)
        return OpenPortState(OpenPortDriver.NOT_FOUND)
    if not states:
        return OpenPortState(OpenPortDriver.NOT_FOUND)
    # With several cables, report one that needs the switch first.
    state = next((s for s in states if s.needs_winusb), states[0])
    logger.info("OpenPort driver state: %s", state)
    return state


def _find_present_instances() -> list[str]:
    cfg = ctypes.WinDLL("cfgmgr32")
    flags = _CM_GETIDLIST_FILTER_ENUMERATOR | _CM_GETIDLIST_FILTER_PRESENT
    for _attempt in range(2):  # a device plugged in between the two calls: retry
        size = c_ulong()
        if cfg.CM_Get_Device_ID_List_SizeW(byref(size), "USB", flags) != _CR_SUCCESS:
            raise OSError("CM_Get_Device_ID_List_SizeW failed")
        buf = create_unicode_buffer(size.value)
        rc = cfg.CM_Get_Device_ID_ListW("USB", buf, size, flags)
        if rc == _CR_SUCCESS:
            break
        if rc != _CR_BUFFER_SMALL:
            raise OSError("CM_Get_Device_ID_ListW failed")
    else:
        raise OSError("CM_Get_Device_ID_ListW: the device list kept changing")
    prefix = OPENPORT_HARDWARE_ID.lower() + "\\"
    return [i for i in buf[: size.value].split("\0") if i.lower().startswith(prefix)]


def _read_devnode(instance: str) -> tuple[Optional[str], int]:
    cfg = ctypes.WinDLL("cfgmgr32")
    devinst = c_ulong()
    if cfg.CM_Locate_DevNodeW(byref(devinst), instance, 0) != _CR_SUCCESS:
        raise OSError(f"CM_Locate_DevNodeW failed for {instance}")
    status, problem = c_ulong(), c_ulong()
    if cfg.CM_Get_DevNode_Status(byref(status), byref(problem), devinst, 0):
        raise OSError("CM_Get_DevNode_Status failed")
    problem_code = problem.value if status.value & _DN_HAS_PROBLEM else 0

    reg_type, length = c_ulong(), c_ulong(512)
    value = create_unicode_buffer(256)
    rc = cfg.CM_Get_DevNode_Registry_PropertyW(
        devinst, _CM_DRP_SERVICE, byref(reg_type), value, byref(length), 0
    )
    service = value.value if rc == _CR_SUCCESS else None
    return service, problem_code


def builtin_driver_available() -> bool:
    """The switch only makes sense when NC Flash's own driver is bundled."""
    return find_builtin_openport_dll() is not None


def find_setup_script() -> Optional[str]:
    """Path of openport_winusb.ps1: bundled next to the driver, or in the repo."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if meipass.is_dir():
            candidates.append(meipass / BUILTIN_OPENPORT_DIR / SETUP_SCRIPT)
        candidates.append(
            Path(sys.executable).parent / BUILTIN_OPENPORT_DIR / SETUP_SCRIPT
        )
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(repo_root / "packaging" / "openport" / SETUP_SCRIPT)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def powershell_path() -> str:
    """Windows PowerShell by full path, never a copy found on PATH or nearby."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def setup_log_dir() -> str:
    """Where the elevated script logs: Administrators write, Users read."""
    data = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(data, "NC Flash", "OpenPort")


def new_log_name() -> str:
    return f"setup_{uuid.uuid4().hex}.log"


def _compact(script_text: str) -> list[str]:
    """Drop comments, blank lines and indentation (they only make the command
    longer), but keep here-string content exactly as written."""
    out, in_here = [], False
    for line in script_text.splitlines():
        stripped = line.strip()
        if in_here:
            out.append(line)
            in_here = not line.startswith(("'@", '"@'))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
        in_here = stripped.endswith(("@'", '@"'))
    return out


def elevated_setup_command(action: str, script_text: str, log_name: str) -> list[str]:
    """
    Command line that runs the setup script elevated and returns its exit code.

    The outer, unelevated PowerShell asks Windows for the UAC prompt and waits.
    The elevated one gets the script text itself (-EncodedCommand), so what it
    runs can't be swapped on disk in between. Its exit code is the script's
    (EXIT_OK/FAILED/RESTART/BUSY), EXIT_DECLINED when the UAC prompt was
    declined, or EXIT_LAUNCH_FAILED when Windows couldn't start it.
    """
    if action not in ("Install", "Restore"):
        raise ValueError(f"unknown action {action!r}")
    stem = log_name[:-4] if log_name.endswith(".log") else ""
    if not stem or not stem.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"bad log name {log_name!r}")
    body = "\n".join(_compact(script_text))
    inner = f"& {{\n{body}\n}} -Action '{action}' -LogName '{log_name}'"
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    ps = powershell_path().replace("'", "''")
    outer = (
        f"try {{ $p = Start-Process -FilePath '{ps}' -Verb RunAs -Wait -PassThru "
        "-WindowStyle Hidden -ArgumentList '-NoProfile','-NonInteractive',"
        f"'-ExecutionPolicy','Bypass','-EncodedCommand','{encoded}'; "
        "exit $p.ExitCode } catch { $e = $_.Exception; "
        "while ($e -and -not ($e -is [ComponentModel.Win32Exception])) "
        "{ $e = $e.InnerException }; "
        # 1223 = ERROR_CANCELLED: the user said No to the UAC prompt.
        "if ($e -and $e.NativeErrorCode -eq 1223) { exit 2 } else { exit 4 } }"
    )
    if len(outer) > _MAX_COMMAND_CHARS:
        raise ValueError("the setup script is too long to pass on the command line")
    return [powershell_path(), "-NoProfile", "-NonInteractive", "-Command", outer]


def read_setup_result(log_path: str) -> tuple[Optional[str], str]:
    """("ok" | "restart" | "failed" | None when there is no result line, log text)."""
    try:
        text = Path(log_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None, ""
    for line in reversed(text.splitlines()):
        for prefix, result in (
            ("RESULT: OK", "ok"),
            ("RESULT: RESTART", "restart"),
            ("RESULT: FAILED", "failed"),
        ):
            if line.startswith(prefix):
                return result, text
    return None, text
