"""
Application Settings Management

Handles loading, saving, and accessing application settings using QSettings.
"""

from PySide6.QtCore import QByteArray, QSettings

from .constants import MAX_RECENT_FILES
from .paths import get_app_root, get_user_data_dir


class AppSettings:
    """Application settings manager using QSettings for persistence"""

    def __init__(self):
        """Initialize settings manager"""
        self.settings = QSettings("NCRomEditor", "NCRomEditor")

    def get_definitions_directory(self) -> str:
        """
        Get the configured ROM definitions directory path

        Returns:
            str: Path to definitions directory (defaults to ./definitions relative to app)
        """
        # Default to 'definitions' directory in the application root
        default_path = str(get_app_root() / "definitions")
        return self.settings.value("paths/definitions_directory", default_path)

    def set_definitions_directory(self, path: str):
        """
        Set the ROM definitions directory path

        Args:
            path: Path to definitions directory
        """
        self.settings.setValue("paths/definitions_directory", path)

    def get_window_geometry(self):
        """Get saved window geometry"""
        value = self.settings.value("window/geometry")
        if value is not None and not isinstance(value, QByteArray):
            return None
        return value

    def set_window_geometry(self, geometry):
        """Save window geometry"""
        self.settings.setValue("window/geometry", geometry)

    def get_splitter_state(self):
        """Get saved splitter state"""
        value = self.settings.value("window/splitter_state")
        if value is not None and not isinstance(value, QByteArray):
            return None
        return value

    def set_splitter_state(self, state):
        """Save splitter state"""
        self.settings.setValue("window/splitter_state", state)

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

    def clear_recent_files(self):
        """Clear the recent files list"""
        self.settings.setValue("recent_files", [])

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

    def get_colormap_path(self) -> str:
        """
        Get the configured color map file path

        Returns:
            str: Path to .map file, or empty string for built-in gradient
        """
        # Default to the built-in default.map in the colormaps directory
        default_path = str(get_app_root() / "colormaps" / "default.map")
        return self.settings.value("display/colormap_path", default_path)

    def set_colormap_path(self, path: str):
        """
        Set the color map file path

        Args:
            path: Path to .map file, or empty string for built-in gradient
        """
        self.settings.setValue("display/colormap_path", path)

    def get_colormap_directory(self) -> str:
        """
        Get the configured color map directory path

        Returns:
            str: Path to directory containing .map files
        """
        default_path = str(get_app_root() / "colormaps")
        return self.settings.value("paths/colormap_directory", default_path)

    def set_colormap_directory(self, path: str):
        """
        Set the color map directory path

        Args:
            path: Path to directory containing .map files
        """
        self.settings.setValue("paths/colormap_directory", path)

    def get_projects_directory(self) -> str:
        """
        Get the configured projects directory path

        Returns:
            str: Path to directory where projects are stored
        """
        default_path = str(get_user_data_dir() / "projects")
        return self.settings.value("paths/projects_directory", default_path)

    def set_projects_directory(self, path: str):
        """
        Set the projects directory path

        Args:
            path: Path to directory where projects are stored
        """
        self.settings.setValue("paths/projects_directory", path)

    _DEFAULT_TOGGLE_CATEGORIES = ["DTC - Activation Flags"]

    def get_toggle_categories(self) -> list:
        """
        Get list of category names that display as toggle switches.

        Returns:
            list: Category names where 1D tables use toggle ON/OFF display
        """
        value = self.settings.value("display/toggle_categories",
                                    self._DEFAULT_TOGGLE_CATEGORIES)
        if value is None:
            return list(self._DEFAULT_TOGGLE_CATEGORIES)
        if isinstance(value, str):
            return [value] if value else []
        return list(value)

    def set_toggle_categories(self, categories: list):
        """
        Set list of category names that display as toggle switches.

        Args:
            categories: List of category name strings
        """
        self.settings.setValue("display/toggle_categories", categories)

    def get_romdrop_executable_path(self) -> str:
        """
        Get the configured RomDrop executable path

        Returns:
            str: Path to romdrop.exe, or empty string if not configured
        """
        return self.settings.value("tools/romdrop_executable_path", "")

    def set_romdrop_executable_path(self, path: str):
        """
        Set the RomDrop executable path

        Args:
            path: Path to romdrop.exe
        """
        self.settings.setValue("tools/romdrop_executable_path", path)

    def get_mcp_auto_start(self) -> bool:
        """Get whether the MCP server should start automatically on app launch."""
        return self.settings.value("tools/mcp_auto_start", False, type=bool)

    def set_mcp_auto_start(self, enabled: bool):
        """Set whether the MCP server should start automatically on app launch."""
        self.settings.setValue("tools/mcp_auto_start", enabled)


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
