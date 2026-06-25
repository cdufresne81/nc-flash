"""
Application Path Resolution

Provides a single source of truth for locating the application root directory
and the user data directory for persistent storage.
"""

import os
import sys
from pathlib import Path


def get_app_root() -> Path:
    """
    Get the application root directory.

    When frozen (PyInstaller), returns the _MEIPASS temp directory where
    bundled data files are extracted. When running from source, returns
    the repository root (three levels up from this file).

    Returns:
        Path: Application root directory
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent


def get_user_data_dir() -> Path:
    """
    Get the user data directory for persistent, writable storage.

    Returns a platform-appropriate location:
    - Windows: %APPDATA%/NCFlash
    - Linux:   ~/.local/share/NCFlash

    Used for user-created content (projects) that must survive
    app updates and uninstalls. Does NOT create the directory.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "NCFlash"


def get_workspace_path() -> Path:
    """Get the path to workspace.json for MCP server discovery.

    Both the app (writer) and MCP server subprocess (reader) use this
    to agree on a shared, stable location that survives across processes.
    """
    return get_user_data_dir() / "workspace.json"
