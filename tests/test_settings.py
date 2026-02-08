"""
Tests for Application Settings

Tests settings loading, saving, and default values.
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from PySide6.QtCore import QByteArray

from src.utils.settings import AppSettings, get_settings
import src.utils.settings as settings_module


@pytest.fixture(autouse=True)
def _restore_settings_globals():
    """Save and restore the module-level ``_settings`` singleton between tests.

    Tests in ``TestGlobalSettingsInstance`` set ``settings_module._settings = None``
    to exercise lazy initialization.  Without cleanup, the mutation leaks into
    later tests and can cause order-dependent failures.
    """
    original_settings = settings_module._settings
    yield
    settings_module._settings = original_settings


@pytest.fixture
def mock_qsettings():
    """Mock QSettings to avoid writing to actual system settings"""
    with patch('src.utils.settings.QSettings') as mock:
        # Create a dict to store settings values
        settings_store = {}

        def mock_value(key, default=None):
            return settings_store.get(key, default)

        def mock_set_value(key, value):
            settings_store[key] = value

        mock_instance = MagicMock()
        mock_instance.value = mock_value
        mock_instance.setValue = mock_set_value
        mock_instance.sync = MagicMock()
        mock.return_value = mock_instance

        yield mock_instance, settings_store


@pytest.fixture
def app_settings(mock_qsettings):
    """Create AppSettings instance with mocked QSettings"""
    return AppSettings()


class TestDefinitionsDirectory:
    """Tests for definitions directory settings"""

    def test_get_definitions_directory_default(self, app_settings):
        """Test getting default definitions directory"""
        result = app_settings.get_definitions_directory()
        assert "definitions" in result
        assert Path(result).name == "definitions"

    def test_set_and_get_definitions_directory(self, mock_qsettings):
        """Test setting and getting definitions directory"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_definitions_directory("/custom/path/definitions")

        assert settings_store.get("paths/definitions_directory") == "/custom/path/definitions"
        assert app_settings.get_definitions_directory() == "/custom/path/definitions"


class TestRecentFiles:
    """Tests for recent files functionality"""

    def test_get_recent_files_empty(self, app_settings):
        """Test getting recent files when none exist"""
        result = app_settings.get_recent_files()
        assert result == []

    def test_get_recent_files_none_value(self, mock_qsettings):
        """Test handling of None value from QSettings"""
        mock_instance, settings_store = mock_qsettings
        settings_store["recent_files"] = None
        app_settings = AppSettings()

        result = app_settings.get_recent_files()
        assert result == []

    def test_get_recent_files_single_string(self, mock_qsettings):
        """Test handling of single string value (Qt quirk)"""
        mock_instance, settings_store = mock_qsettings
        settings_store["recent_files"] = "/path/to/file.bin"
        app_settings = AppSettings()

        result = app_settings.get_recent_files()
        assert result == ["/path/to/file.bin"]

    def test_get_recent_files_empty_string(self, mock_qsettings):
        """Test handling of empty string value"""
        mock_instance, settings_store = mock_qsettings
        settings_store["recent_files"] = ""
        app_settings = AppSettings()

        result = app_settings.get_recent_files()
        assert result == []

    def test_add_recent_file(self, mock_qsettings):
        """Test adding a file to recent files"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.add_recent_file("/path/to/file1.bin")

        recent = app_settings.get_recent_files()
        assert len(recent) == 1
        assert recent[0] == "/path/to/file1.bin"

    def test_add_recent_file_moves_to_front(self, mock_qsettings):
        """Test that adding existing file moves it to front"""
        mock_instance, settings_store = mock_qsettings
        settings_store["recent_files"] = ["/path/file1.bin", "/path/file2.bin"]
        app_settings = AppSettings()

        app_settings.add_recent_file("/path/file2.bin")

        recent = app_settings.get_recent_files()
        assert recent[0] == "/path/file2.bin"
        assert recent[1] == "/path/file1.bin"

    def test_add_recent_file_respects_max_limit(self, mock_qsettings):
        """Test that recent files list respects max limit"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        # Add more files than the limit
        for i in range(15):
            app_settings.add_recent_file(f"/path/file{i}.bin")

        recent = app_settings.get_recent_files()
        # Default max is MAX_RECENT_FILES (10)
        assert len(recent) <= 10
        # Most recent should be first
        assert recent[0] == "/path/file14.bin"

    def test_clear_recent_files(self, mock_qsettings):
        """Test clearing recent files"""
        mock_instance, settings_store = mock_qsettings
        settings_store["recent_files"] = ["/path/file1.bin", "/path/file2.bin"]
        app_settings = AppSettings()

        app_settings.clear_recent_files()

        recent = app_settings.get_recent_files()
        assert recent == []


class TestSessionFiles:
    """Tests for session file persistence"""

    def test_get_session_files_empty(self, app_settings):
        """Test getting session files when none exist"""
        result = app_settings.get_session_files()
        assert result == []

    def test_get_session_files_none_value(self, mock_qsettings):
        """Test handling of None value"""
        mock_instance, settings_store = mock_qsettings
        settings_store["session/open_files"] = None
        app_settings = AppSettings()

        result = app_settings.get_session_files()
        assert result == []

    def test_get_session_files_single_string(self, mock_qsettings):
        """Test handling of single string value"""
        mock_instance, settings_store = mock_qsettings
        settings_store["session/open_files"] = "/path/to/file.bin"
        app_settings = AppSettings()

        result = app_settings.get_session_files()
        assert result == ["/path/to/file.bin"]

    def test_set_and_get_session_files(self, mock_qsettings):
        """Test setting and getting session files"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        files = ["/path/file1.bin", "/path/file2.bin"]
        app_settings.set_session_files(files)

        result = app_settings.get_session_files()
        assert result == files


class TestDisplaySettings:
    """Tests for display-related settings"""

    def test_get_gradient_mode_default(self, app_settings):
        """Test default gradient mode"""
        result = app_settings.get_gradient_mode()
        assert result == "minmax"

    def test_set_and_get_gradient_mode(self, mock_qsettings):
        """Test setting and getting gradient mode"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_gradient_mode("neighbors")

        result = app_settings.get_gradient_mode()
        assert result == "neighbors"

    def test_get_table_font_size_default(self, app_settings):
        """Test default table font size"""
        result = app_settings.get_table_font_size()
        assert result == 9

    def test_set_and_get_table_font_size(self, mock_qsettings):
        """Test setting and getting table font size"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_table_font_size(12)

        result = app_settings.get_table_font_size()
        assert result == 12


class TestColormapSettings:
    """Tests for colormap settings"""

    def test_get_colormap_path_default(self, app_settings):
        """Test default colormap path"""
        result = app_settings.get_colormap_path()
        assert "default.map" in result
        assert "colormaps" in result

    def test_set_and_get_colormap_path(self, mock_qsettings):
        """Test setting and getting colormap path"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_colormap_path("/custom/colormap/custom.map")

        result = app_settings.get_colormap_path()
        assert result == "/custom/colormap/custom.map"

    def test_get_colormap_directory_default(self, app_settings):
        """Test default colormap directory"""
        result = app_settings.get_colormap_directory()
        assert "colormaps" in result

    def test_set_and_get_colormap_directory(self, mock_qsettings):
        """Test setting and getting colormap directory"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_colormap_directory("/custom/colormap")

        result = app_settings.get_colormap_directory()
        assert result == "/custom/colormap"


class TestProjectsDirectory:
    """Tests for projects directory settings"""

    def test_get_projects_directory_default(self, app_settings):
        """Test default projects directory"""
        result = app_settings.get_projects_directory()
        assert "projects" in result

    def test_set_and_get_projects_directory(self, mock_qsettings):
        """Test setting and getting projects directory"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_projects_directory("/custom/projects")

        result = app_settings.get_projects_directory()
        assert result == "/custom/projects"


class TestWindowSettings:
    """Tests for window geometry and splitter state"""

    def test_get_window_geometry_none(self, app_settings):
        """Test getting window geometry when not set"""
        result = app_settings.get_window_geometry()
        assert result is None

    def test_set_and_get_window_geometry(self, mock_qsettings):
        """Test setting and getting window geometry"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        geometry_data = QByteArray(b"fake_geometry_data")
        app_settings.set_window_geometry(geometry_data)

        result = app_settings.get_window_geometry()
        assert result == geometry_data

    def test_get_splitter_state_none(self, app_settings):
        """Test getting splitter state when not set"""
        result = app_settings.get_splitter_state()
        assert result is None

    def test_set_and_get_splitter_state(self, mock_qsettings):
        """Test setting and getting splitter state"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        state_data = QByteArray(b"fake_splitter_state")
        app_settings.set_splitter_state(state_data)

        result = app_settings.get_splitter_state()
        assert result == state_data


class TestToggleCategories:
    """Tests for toggle categories settings (DTC toggle switch feature)"""

    def test_get_toggle_categories_default(self, app_settings):
        """Test default toggle categories includes DTC Activation Flags"""
        result = app_settings.get_toggle_categories()
        assert result == ["DTC - Activation Flags"]

    def test_set_and_get_toggle_categories(self, mock_qsettings):
        """Test setting and getting toggle categories"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_toggle_categories(["DTC - Activation Flags", "Custom Category"])

        result = app_settings.get_toggle_categories()
        assert result == ["DTC - Activation Flags", "Custom Category"]

    def test_set_empty_toggle_categories(self, mock_qsettings):
        """Test disabling all toggle categories"""
        mock_instance, settings_store = mock_qsettings
        app_settings = AppSettings()

        app_settings.set_toggle_categories([])

        result = app_settings.get_toggle_categories()
        assert result == []

    def test_get_toggle_categories_single_string(self, mock_qsettings):
        """Test handling of single string value (QSettings quirk with 1-element lists)"""
        mock_instance, settings_store = mock_qsettings
        settings_store["display/toggle_categories"] = "DTC - Activation Flags"
        app_settings = AppSettings()

        result = app_settings.get_toggle_categories()
        assert result == ["DTC - Activation Flags"]

    def test_get_toggle_categories_empty_string(self, mock_qsettings):
        """Test handling of empty string value"""
        mock_instance, settings_store = mock_qsettings
        settings_store["display/toggle_categories"] = ""
        app_settings = AppSettings()

        result = app_settings.get_toggle_categories()
        assert result == []

    def test_get_toggle_categories_none_value(self, mock_qsettings):
        """Test handling of None value falls back to default"""
        mock_instance, settings_store = mock_qsettings
        settings_store["display/toggle_categories"] = None
        app_settings = AppSettings()

        result = app_settings.get_toggle_categories()
        assert result == ["DTC - Activation Flags"]


class TestGlobalSettingsInstance:
    """Tests for global settings singleton"""

    def test_get_settings_returns_instance(self):
        """Test that get_settings returns an AppSettings instance"""
        with patch('src.utils.settings.QSettings'):
            # Reset the global instance
            import src.utils.settings as settings_module
            settings_module._settings = None

            result = get_settings()
            assert isinstance(result, AppSettings)

    def test_get_settings_returns_same_instance(self):
        """Test that get_settings returns the same instance"""
        with patch('src.utils.settings.QSettings'):
            import src.utils.settings as settings_module
            settings_module._settings = None

            result1 = get_settings()
            result2 = get_settings()
            assert result1 is result2
