"""
Application Settings Management

Handles loading, saving, and accessing application settings using QSettings.
"""

from pathlib import Path
from PySide6.QtCore import QSettings


class AppSettings:
    """Application settings manager using QSettings for persistence"""

    def __init__(self):
        """Initialize settings manager"""
        self.settings = QSettings("NCRomEditor", "NCRomEditor")

    def get_metadata_directory(self) -> str:
        """
        Get the configured metadata directory path

        Returns:
            str: Path to metadata directory (defaults to ./metadata relative to app)
        """
        # Default to 'metadata' directory in the application root
        default_path = str(Path.cwd() / "metadata")
        return self.settings.value("paths/metadata_directory", default_path)

    def set_metadata_directory(self, path: str):
        """
        Set the metadata directory path

        Args:
            path: Path to metadata directory
        """
        self.settings.setValue("paths/metadata_directory", path)
        self.settings.sync()

    def get_window_geometry(self):
        """Get saved window geometry"""
        return self.settings.value("window/geometry")

    def set_window_geometry(self, geometry):
        """Save window geometry"""
        self.settings.setValue("window/geometry", geometry)
        self.settings.sync()

    def get_splitter_state(self):
        """Get saved splitter state"""
        return self.settings.value("window/splitter_state")

    def set_splitter_state(self, state):
        """Save splitter state"""
        self.settings.setValue("window/splitter_state", state)
        self.settings.sync()

    def get_recent_files(self) -> list:
        """
        Get list of recently opened ROM files

        Returns:
            list: List of file paths
        """
        files = self.settings.value("recent_files", [])
        if files is None:
            return []
        # Ensure it's always a list
        if isinstance(files, str):
            return [files] if files else []
        return files

    def add_recent_file(self, file_path: str, max_recent: int = 10):
        """
        Add a file to the recent files list

        Args:
            file_path: Path to ROM file
            max_recent: Maximum number of recent files to keep
        """
        recent = self.get_recent_files()

        # Remove if already in list
        if file_path in recent:
            recent.remove(file_path)

        # Add to front of list
        recent.insert(0, file_path)

        # Limit to max_recent
        recent = recent[:max_recent]

        self.settings.setValue("recent_files", recent)
        self.settings.sync()

    def clear_recent_files(self):
        """Clear the recent files list"""
        self.settings.setValue("recent_files", [])
        self.settings.sync()


# Global settings instance
_settings = None


def get_settings() -> AppSettings:
    """
    Get the global settings instance

    Returns:
        AppSettings: Global settings instance
    """
    global _settings
    if _settings is None:
        _settings = AppSettings()
    return _settings
