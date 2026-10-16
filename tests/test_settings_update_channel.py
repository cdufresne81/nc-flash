"""Tests for the update channel setting (stable / experimental)."""

from unittest.mock import MagicMock, patch

import pytest

from src.utils.settings import AppSettings


@pytest.fixture
def store():
    """AppSettings over a dict-backed QSettings; yields (settings, store)."""
    with patch("src.utils.settings.QSettings") as mock:
        backing = {}

        def value(key, default=None, type=None):
            val = backing.get(key, default)
            return type(val) if type is not None and val is not None else val

        instance = MagicMock()
        instance.value = value
        instance.setValue = lambda key, val: backing.__setitem__(key, val)
        mock.return_value = instance
        yield AppSettings(), backing


def test_default_is_stable(store):
    settings, _ = store
    assert settings.get_update_channel() == "stable"


def test_round_trip_experimental(store):
    settings, _ = store
    settings.set_update_channel("experimental")
    assert settings.get_update_channel() == "experimental"
    settings.set_update_channel("stable")
    assert settings.get_update_channel() == "stable"


def test_unknown_values_are_stable(store):
    settings, backing = store
    backing["updates/channel"] = "nightly"
    assert settings.get_update_channel() == "stable"
    settings.set_update_channel("nightly")
    assert backing["updates/channel"] == "stable"


def test_switching_channel_makes_the_next_launch_check(store):
    settings, _ = store
    settings.set_last_update_check(1_700_000_000.0)
    settings.set_update_channel("stable")  # unchanged: keeps the daily throttle
    assert settings.get_last_update_check() == 1_700_000_000.0
    settings.set_update_channel("experimental")
    assert settings.get_last_update_check() == 0.0
