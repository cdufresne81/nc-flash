"""
Build the 32-bit J2534 bridge executable for local development.

This script is for **local dev only** — CI/release builds use build.bat.

Prerequisites:
    Install 32-bit Python (one-time):
        winget install Python.Python.3.12 --architecture x86

Usage:
    python packaging/build_bridge.py

The script will:
    1. Find 32-bit Python via the ``py -3-32`` launcher
    2. Ensure PyInstaller is available (installs it if needed)
    3. Build ``dist/j2534_bridge_32/j2534_bridge_32.exe``
"""

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC_FILE = _REPO_ROOT / "packaging" / "j2534_bridge_32.spec"
_OUTPUT_EXE = _REPO_ROOT / "dist" / "j2534_bridge_32" / "j2534_bridge_32.exe"


def _find_py32() -> list[str] | None:
    """Return the command list for 32-bit Python, or None."""
    cmd = ["py", "-3-32"]
    try:
        r = subprocess.run(
            [*cmd, "-c", "print('ok')"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and "ok" in r.stdout:
            return cmd
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _ensure_pyinstaller(py_cmd: list[str]) -> bool:
    """Make sure PyInstaller is importable in the target Python."""
    r = subprocess.run(
        [*py_cmd, "-m", "PyInstaller", "--version"],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode == 0:
        return True

    print("Installing PyInstaller in 32-bit Python...")
    r = subprocess.run(
        [*py_cmd, "-m", "pip", "install", "pyinstaller>=6.0,<7.0"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"Failed to install PyInstaller:\n{r.stderr}", file=sys.stderr)
        return False
    return True


def build_bridge() -> Path | None:
    """Build the 32-bit bridge exe.  Returns the exe path on success."""
    if not _SPEC_FILE.is_file():
        print(f"Spec file not found: {_SPEC_FILE}", file=sys.stderr)
        return None

    py_cmd = _find_py32()
    if py_cmd is None:
        print(
            "32-bit Python not found.  Install it with:\n"
            "  winget install Python.Python.3.12 --architecture x86",
            file=sys.stderr,
        )
        return None

    if not _ensure_pyinstaller(py_cmd):
        return None

    print("Building 32-bit J2534 bridge...")
    r = subprocess.run(
        [*py_cmd, "-m", "PyInstaller", str(_SPEC_FILE), "--noconfirm"],
        cwd=str(_REPO_ROOT),
        timeout=180,
    )
    if r.returncode != 0:
        print("PyInstaller build failed.", file=sys.stderr)
        return None

    if not _OUTPUT_EXE.is_file():
        print(f"Build succeeded but exe not found at {_OUTPUT_EXE}", file=sys.stderr)
        return None

    print(f"Bridge built: {_OUTPUT_EXE}")
    return _OUTPUT_EXE


if __name__ == "__main__":
    result = build_bridge()
    sys.exit(0 if result else 1)
