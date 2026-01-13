"""
Application Settings Management

Handles loading, saving, and accessing application settings using QSettings.
"""

from pathlib import Path
from PySide6.QtCore import QSettings

from .constants import MAX_RECENT_FILES


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

    def add_recent_file(self, file_path: str, max_recent: int = MAX_RECENT_FILES):
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

    def get_session_files(self) -> list:
        """
        Get list of files from last session

        Returns:
            list: List of file paths that were open when app last closed
        """
        files = self.settings.value("session/open_files", [])
        if files is None:
            return []
        if isinstance(files, str):
            return [files] if files else []
        return files

    def set_session_files(self, file_paths: list):
        """
        Save list of currently open files for session restore

        Args:
            file_paths: List of ROM file paths currently open
        """
        self.settings.setValue("session/open_files", file_paths)
        self.settings.sync()

    def get_gradient_mode(self) -> str:
        """
        Get gradient coloring mode for table cells

        Returns:
            str: 'minmax' (default) or 'neighbors'
        """
        return self.settings.value("display/gradient_mode", "minmax")

    def set_gradient_mode(self, mode: str):
        """
        Set gradient coloring mode for table cells

        Args:
            mode: 'minmax' or 'neighbors'
        """
        self.settings.setValue("display/gradient_mode", mode)
        self.settings.sync()

    def get_table_font_size(self) -> int:
        """
        Get font size for table cells

        Returns:
            int: Font size in pixels (default 9 for compact display)
        """
        return int(self.settings.value("display/table_font_size", 9))

    def set_table_font_size(self, size: int):
        """
        Set font size for table cells

        Args:
            size: Font size in pixels
        """
        self.settings.setValue("display/table_font_size", size)
        self.settings.sync()

    def get_colormap_path(self) -> str:
        """
        Get the configured color map file path

        Returns:
            str: Path to .map file, or empty string for built-in gradient
        """
        # Default to the built-in default.map in the colormap directory
        default_path = str(Path(__file__).parent.parent.parent / "colormap" / "default.map")
        return self.settings.value("display/colormap_path", default_path)

    def set_colormap_path(self, path: str):
        """
        Set the color map file path

        Args:
            path: Path to .map file, or empty string for built-in gradient
        """
        self.settings.setValue("display/colormap_path", path)
        self.settings.sync()

    def get_colormap_directory(self) -> str:
        """
        Get the configured color map directory path

        Returns:
            str: Path to directory containing .map files
        """
        default_path = str(Path(__file__).parent.parent.parent / "colormap")
        return self.settings.value("paths/colormap_directory", default_path)

    def set_colormap_directory(self, path: str):
        """
        Set the color map directory path

        Args:
            path: Path to directory containing .map files
        """
        self.settings.setValue("paths/colormap_directory", path)
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
