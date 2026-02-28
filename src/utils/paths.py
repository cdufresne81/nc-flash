"""
Application Path Resolution

Provides a single source of truth for locating the application root directory,
whether running from source or from a PyInstaller frozen executable.
"""

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
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent
