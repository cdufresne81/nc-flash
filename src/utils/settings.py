"""
Application Settings Management

Handles loading, saving, and accessing application settings using QSettings.
"""

import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings

from .constants import MAX_RECENT_FILES
from .paths import get_app_root, get_user_data_dir


class AppSettings:
    """Application settings manager using QSettings for persistence"""

    def __init__(self):
        """Initialize settings manager"""
        self.settings = QSettings("NCFlash", "NCFlash")

    # ------------------------------------------------------------------ #
    # Workspace directory (root for all user content)
    # ------------------------------------------------------------------ #

    def get_workspace_directory(self) -> str:
        """Get the workspace root directory (defaults to user data dir)."""
        default_path = str(get_user_data_dir())
        return os.path.normpath(
            self.settings.value("paths/workspace_directory", default_path)
        )

    def set_workspace_directory(self, path: str):
        """Set the workspace root directory."""
        self.settings.setValue("paths/workspace_directory", path)

    # ------------------------------------------------------------------ #
    # Workspace-derived path settings
    # ------------------------------------------------------------------ #

    def _get_workspace_path(self, key: str, subdir: str) -> str:
        """Get a path setting that defaults to a workspace subdirectory."""
        default = str(Path(self.get_workspace_directory()) / subdir)
        return os.path.normpath(self.settings.value(key, default))

    def _set_path(self, key: str, path: str):
        """Set a path setting."""
        self.settings.setValue(key, path)

    def get_metadata_directory(self) -> str:
        """Get the ROM metadata directory (defaults to {workspace}/metadata)."""
        return self._get_workspace_path("paths/metadata_directory", "metadata")

    def set_metadata_directory(self, path: str):
        self._set_path("paths/metadata_directory", path)

    def get_colormap_directory(self) -> str:
        """Get the color map directory (defaults to {workspace}/colormaps)."""
        return self._get_workspace_path("paths/colormap_directory", "colormaps")

    def set_colormap_directory(self, path: str):
        self._set_path("paths/colormap_directory", path)

    def get_export_directory(self) -> str:
        """Get the CSV export directory (defaults to {workspace}/exports)."""
        return self._get_workspace_path("paths/export_directory", "exports")

    def set_export_directory(self, path: str):
        self._set_path("paths/export_directory", path)

    def get_projects_directory(self) -> str:
        """Get the projects directory (defaults to {workspace}/projects)."""
        return self._get_workspace_path("paths/projects_directory", "projects")

    def set_projects_directory(self, path: str):
        self._set_path("paths/projects_directory", path)

    def get_roms_directory(self) -> str:
        """Get the default ROM files directory (defaults to {workspace}/roms)."""
        return self._get_workspace_path("paths/roms_directory", "roms")

    def set_roms_directory(self, path: str):
        self._set_path("paths/roms_directory", path)

    def get_screenshots_directory(self) -> str:
        """Get the screenshots directory (defaults to {workspace}/screenshots)."""
        return self._get_workspace_path("paths/screenshots_directory", "screenshots")

    def set_screenshots_directory(self, path: str):
        self._set_path("paths/screenshots_directory", path)

    def get_reads_directory(self) -> str:
        """Get the ECU reads directory (defaults to {workspace}/reads)."""
        return self._get_workspace_path("paths/reads_directory", "reads")

    def set_reads_directory(self, path: str):
        self._set_path("paths/reads_directory", path)

    def get_logs_directory(self) -> str:
        """Get the WiCAN trip-log directory (defaults to {workspace}/logs)."""
        return self._get_workspace_path("paths/logs_directory", "logs")

    def set_logs_directory(self, path: str):
        self._set_path("paths/logs_directory", path)

    # ------------------------------------------------------------------ #
    # Window state
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Recent files & session
    # ------------------------------------------------------------------ #

    def get_recent_files(self) -> list:
        """Get list of recently opened ROM files."""
        files = self.settings.value("recent_files", [])
        if files is None:
            return []
        if isinstance(files, str):
            return [files] if files else []
        return files

    def add_recent_file(self, file_path: str, max_recent: int = MAX_RECENT_FILES):
        """Add a file to the recent files list."""
        recent = self.get_recent_files()
        if file_path in recent:
            recent.remove(file_path)
        recent.insert(0, file_path)
        recent = recent[:max_recent]
        self.settings.setValue("recent_files", recent)

    def clear_recent_files(self):
        """Clear the recent files list"""
        self.settings.setValue("recent_files", [])

    def get_session_files(self) -> list:
        """Get list of files from last session."""
        files = self.settings.value("session/open_files", [])
        if files is None:
            return []
        if isinstance(files, str):
            return [files] if files else []
        return files

    def set_session_files(self, file_paths: list):
        """Save list of currently open files for session restore."""
        self.settings.setValue("session/open_files", file_paths)

    # ------------------------------------------------------------------ #
    # Display settings
    # ------------------------------------------------------------------ #

    def get_gradient_mode(self) -> str:
        """Get gradient coloring mode ('minmax' or 'neighbors')."""
        return self.settings.value("display/gradient_mode", "minmax")

    def set_gradient_mode(self, mode: str):
        self.settings.setValue("display/gradient_mode", mode)

    def get_table_font_size(self) -> int:
        """Get font size for table cells in pixels (default 11)."""
        return int(self.settings.value("display/table_font_size", 11))

    def set_table_font_size(self, size: int):
        self.settings.setValue("display/table_font_size", size)

    def get_colormap_path(self) -> str:
        """Get the color map file path."""
        default_path = str(get_app_root() / "colormaps" / "default.map")
        return os.path.normpath(
            self.settings.value("display/colormap_path", default_path)
        )

    def set_colormap_path(self, path: str):
        self.settings.setValue("display/colormap_path", path)

    def get_show_type_column(self) -> bool:
        """Get whether the Type column is visible in the table browser."""
        return self.settings.value("display/show_type_column", True, type=bool)

    def set_show_type_column(self, enabled: bool):
        self.settings.setValue("display/show_type_column", enabled)

    def get_show_address_column(self) -> bool:
        """Get whether the Address column is visible in the table browser."""
        return self.settings.value("display/show_address_column", True, type=bool)

    def set_show_address_column(self, enabled: bool):
        self.settings.setValue("display/show_address_column", enabled)

    # ------------------------------------------------------------------ #
    # Editor / toggle settings
    # ------------------------------------------------------------------ #

    _DEFAULT_TOGGLE_CATEGORIES = ["DTC - Activation Flags"]

    def get_toggle_categories(self) -> list:
        """Get category names that display as toggle switches."""
        value = self.settings.value(
            "display/toggle_categories", self._DEFAULT_TOGGLE_CATEGORIES
        )
        if value is None:
            return list(self._DEFAULT_TOGGLE_CATEGORIES)
        if isinstance(value, str):
            return [value] if value else []
        return list(value)

    def set_toggle_categories(self, categories: list):
        self.settings.setValue("display/toggle_categories", categories)

    def get_auto_round(self) -> bool:
        """Get whether interpolation/smoothing results are auto-rounded."""
        return self.settings.value("editor/auto_round", False, type=bool)

    def set_auto_round(self, enabled: bool):
        self.settings.setValue("editor/auto_round", enabled)

    # ------------------------------------------------------------------ #
    # Tools settings
    # ------------------------------------------------------------------ #

    def get_mcp_auto_start(self) -> bool:
        """Get whether the MCP server should start automatically on app launch."""
        return self.settings.value("tools/mcp_auto_start", False, type=bool)

    def set_mcp_auto_start(self, enabled: bool):
        self.settings.setValue("tools/mcp_auto_start", enabled)

    # ------------------------------------------------------------------ #
    # ECU settings
    # ------------------------------------------------------------------ #

    def get_j2534_dll_path(self) -> str:
        """Get the J2534 DLL path for ECU communication."""
        path = self.settings.value("ecu/j2534_dll_path", "")
        return os.path.normpath(path) if path else ""

    def set_j2534_dll_path(self, path: str):
        self.settings.setValue("ecu/j2534_dll_path", path)

    # -- Adapter selection (J2534 wired default; WiCAN opt-in) --

    def get_ecu_adapter(self) -> str:
        """Get the selected ECU adapter kind: ``"j2534"`` (default) or ``"wican"``."""
        value = self.settings.value("ecu/adapter", "j2534")
        return value if value in ("j2534", "wican") else "j2534"

    def set_ecu_adapter(self, kind: str):
        self.settings.setValue("ecu/adapter", "wican" if kind == "wican" else "j2534")

    def is_wican_adapter(self) -> bool:
        """True when the WiCAN adapter is selected.

        The single predicate for WiCAN-only affordances (trip-log sync, its
        Download Logs button) — callers never compare the raw adapter string.
        """
        return self.get_ecu_adapter() == "wican"

    # -- WiCAN (SLCAN-over-TCP) settings --

    def get_wican_host(self) -> str:
        """Get the WiCAN adapter host/IP."""
        return self.settings.value("ecu/wican_host", "192.168.1.169")

    def set_wican_host(self, host: str):
        self.settings.setValue("ecu/wican_host", host)

    def get_wican_device_id(self) -> str:
        """Get the stable WiCAN device identity (mDNS device_id/mac), if any.

        Persisted when the user picks a device via mDNS "Scan". Lets the app
        re-resolve the adapter's *current* DHCP IP at connect time, so the link
        survives the adapter's IP changing. Empty string means "no identity
        stored — use the static host above".
        """
        return self.settings.value("ecu/wican_device_id", "")

    def set_wican_device_id(self, device_id: str):
        self.settings.setValue("ecu/wican_device_id", device_id or "")

    def get_wican_port(self) -> int:
        """Get the WiCAN SLCAN TCP port."""
        return self.settings.value("ecu/wican_port", 35000, type=int)

    def set_wican_port(self, port: int):
        self.settings.setValue("ecu/wican_port", int(port))

    def get_wican_auto_config(self) -> bool:
        """Whether to auto-switch the WiCAN to SLCAN on connect and restore after."""
        return self.settings.value("ecu/wican_auto_config", True, type=bool)

    def set_wican_auto_config(self, enabled: bool):
        self.settings.setValue("ecu/wican_auto_config", bool(enabled))

    def get_wican_auto_download_logs(self) -> bool:
        """Whether to auto-download new SD trip logs at launch (no-op when no
        WiCAN host/identity is configured)."""
        return self.settings.value("ecu/wican_auto_download_logs", True, type=bool)

    def set_wican_auto_download_logs(self, enabled: bool):
        self.settings.setValue("ecu/wican_auto_download_logs", bool(enabled))


# Global settings instance
_settings = None


def get_settings() -> AppSettings:
    """Get the global settings instance."""
    global _settings
    if _settings is None:
        _settings = AppSettings()
    return _settings
