"""Tests for ECU adapter + WiCAN settings round-trip and defaults."""

from unittest.mock import MagicMock, patch

import pytest

from src.utils.settings import AppSettings


@pytest.fixture
def mock_qsettings():
    """Mock QSettings with a backing dict that honours the ``type=`` kwarg."""
    with patch("src.utils.settings.QSettings") as mock:
        store = {}

        def mock_value(key, default=None, type=None):
            val = store.get(key, default)
            if type is not None and val is not None:
                try:
                    return type(val)
                except (TypeError, ValueError):
                    return val
            return val

        instance = MagicMock()
        instance.value = mock_value
        instance.setValue = lambda key, value: store.__setitem__(key, value)
        instance.sync = MagicMock()
        mock.return_value = instance
        yield instance, store


@pytest.fixture
def app_settings(mock_qsettings):
    return AppSettings()


class TestAdapterSelection:
    def test_default_is_j2534(self, app_settings):
        assert app_settings.get_ecu_adapter() == "j2534"

    def test_round_trip_wican(self, app_settings):
        app_settings.set_ecu_adapter("wican")
        assert app_settings.get_ecu_adapter() == "wican"

    def test_round_trip_j2534(self, app_settings):
        app_settings.set_ecu_adapter("wican")
        app_settings.set_ecu_adapter("j2534")
        assert app_settings.get_ecu_adapter() == "j2534"

    def test_unknown_value_coerced_to_j2534(self, app_settings, mock_qsettings):
        _instance, store = mock_qsettings
        store["ecu/adapter"] = "garbage"
        assert app_settings.get_ecu_adapter() == "j2534"

    def test_set_unknown_stored_as_j2534(self, app_settings):
        app_settings.set_ecu_adapter("nonsense")
        assert app_settings.get_ecu_adapter() == "j2534"


class TestWiCANSettings:
    def test_default_host(self, app_settings):
        assert app_settings.get_wican_host() == "192.168.1.169"

    def test_host_round_trip(self, app_settings):
        app_settings.set_wican_host("10.0.0.5")
        assert app_settings.get_wican_host() == "10.0.0.5"

    def test_no_configurable_port_or_auto_config(self, app_settings):
        """The firmware has one mode on one fixed port, so neither setting can
        mean anything any more. `auto_config` is the dangerous one: it used to
        gate the ONLY working connect path, so a user who read its help text and
        turned it off lost all ECU connectivity. Pinned negatively so a revert
        cannot quietly reintroduce a setting nothing honours."""
        for attr in (
            "get_wican_port",
            "set_wican_port",
            "get_wican_auto_config",
            "set_wican_auto_config",
        ):
            assert not hasattr(app_settings, attr), f"{attr} came back"

    def test_default_device_id_empty(self, app_settings):
        assert app_settings.get_wican_device_id() == ""

    def test_device_id_round_trip(self, app_settings):
        app_settings.set_wican_device_id("dcb4d91511b9")
        assert app_settings.get_wican_device_id() == "dcb4d91511b9"

    def test_device_id_none_stored_as_empty(self, app_settings):
        app_settings.set_wican_device_id("")
        assert app_settings.get_wican_device_id() == ""
