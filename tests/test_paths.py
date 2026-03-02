"""Tests for src.utils.paths module"""

from pathlib import Path

from src.utils.paths import get_app_root, get_user_data_dir


class TestGetUserDataDir:
    def test_returns_path_object(self):
        result = get_user_data_dir()
        assert isinstance(result, Path)

    def test_ends_with_app_name(self):
        result = get_user_data_dir()
        assert result.name == "NCFlash"

    def test_not_inside_app_root(self):
        user_dir = get_user_data_dir()
        app_root = get_app_root()
        assert not str(user_dir).startswith(str(app_root))
