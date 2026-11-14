"""Tests for ``WiCANApSwitch``: joining the WiCAN's Wi-Fi on Connect (#129).

A fake Wi-Fi backend stands in for Windows, so nothing really switches.
"""

from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QMessageBox

from src.ui import wican_ap_switch as mod
from src.ui.wican_ap_switch import WiCANApSwitch
from src.utils.wifi_switch import (
    AUTH_OTHER,
    AUTH_WPA2_PSK,
    WICAN_AP_HOST,
    WifiConnection,
    WifiLocationOff,
    WifiNetwork,
)

HOME_HOST = "192.168.1.169"
AP = "WiCAN_dcb4d91511b9"


@pytest.fixture(autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_busy_dialog():
    class _FakeBusy:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def set_text(self, text):
            pass

        def tick(self):
            return True

    with (
        patch.object(mod, "_Busy", _FakeBusy),
        patch.multiple(
            mod, SCAN_WAIT_S=0.05, JOIN_TIMEOUT_S=0.05, AP_ANSWER_TIMEOUT_S=0.05
        ),
        patch.object(mod.wifi_switch.time, "sleep", lambda s: None),
    ):
        yield


class FakeSettings:
    def __init__(self, mode="ask", ssid=AP, prev=""):
        self.mode, self.ssid, self.prev = mode, ssid, prev

    def get_wican_ap_switch(self):
        return self.mode

    def get_wican_ap_ssid(self):
        return self.ssid

    def set_wican_ap_ssid(self, v):
        self.ssid = v

    def get_wican_ap_prev_profile(self):
        return self.prev

    def set_wican_ap_prev_profile(self, v):
        self.prev = v


class FakeWifi:
    """Joins instantly unless ``join_works`` is False."""

    def __init__(self, current=("Home", "Home"), visible=None, join_works=True):
        self._current = WifiConnection(*current) if current else None
        self._visible = (
            visible if visible is not None else [WifiNetwork(AP, AP, AUTH_WPA2_PSK, 80)]
        )
        self.join_works = join_works
        self.connects, self.added, self.deleted = [], [], []
        self.scanned = False

    def current(self):
        return self._current

    def visible(self):
        return list(self._visible)

    def scan(self):
        self.scanned = True

    def add_profile(self, ssid, auth, password=""):
        self.added.append((ssid, auth, password))
        return ssid

    def delete_profile(self, profile):
        self.deleted.append(profile)

    def connect(self, profile):
        self.connects.append(profile)
        if self.join_works or profile == "Home":
            self._current = WifiConnection(profile, profile)


def _switch(settings, wifi, reachable_hosts=(WICAN_AP_HOST,)):
    return WiCANApSwitch(
        None,
        settings,
        backend_factory=lambda: wifi,
        reachable=lambda host, port, t: host in reachable_hosts
        and (host != WICAN_AP_HOST or (wifi.current() and wifi.current().ssid == AP)),
    )


def _answer(reply):
    return patch.object(mod.QMessageBox, "question", return_value=reply)


# --------------------------------------------------------------------------- #
# reach(): Connect
# --------------------------------------------------------------------------- #


def test_reachable_host_is_used_without_touching_wifi():
    wifi = FakeWifi()
    sw = _switch(FakeSettings(), wifi, reachable_hosts=(HOME_HOST,))
    with _answer(QMessageBox.Yes) as q:
        assert sw.reach(HOME_HOST) == HOME_HOST
    q.assert_not_called()
    assert wifi.connects == []


def test_already_on_the_wican_wifi_uses_the_ap_address_without_asking():
    wifi = FakeWifi(current=(AP, AP))
    sw = _switch(FakeSettings(mode="off"), wifi)
    with _answer(QMessageBox.Yes) as q:
        assert sw.reach(HOME_HOST) == WICAN_AP_HOST
    q.assert_not_called()
    assert wifi.connects == []


def test_asks_then_switches_and_remembers_the_previous_network():
    wifi = FakeWifi()
    settings = FakeSettings()
    sw = _switch(settings, wifi)
    with _answer(QMessageBox.Yes) as q:
        assert sw.reach(HOME_HOST) == WICAN_AP_HOST
    q.assert_called_once()
    assert wifi.connects == [AP]
    assert settings.prev == "Home"


def test_declining_changes_nothing():
    wifi = FakeWifi()
    settings = FakeSettings()
    sw = _switch(settings, wifi)
    with _answer(QMessageBox.No):
        assert sw.reach(HOME_HOST) == HOME_HOST
    assert wifi.connects == []
    assert settings.prev == ""


def test_setting_off_never_asks():
    wifi = FakeWifi()
    sw = _switch(FakeSettings(mode="off"), wifi)
    with _answer(QMessageBox.Yes) as q:
        assert sw.reach(HOME_HOST) == HOME_HOST
    q.assert_not_called()
    assert wifi.connects == []


def test_no_switch_while_something_else_uses_the_network():
    wifi = FakeWifi()
    sw = _switch(FakeSettings(), wifi)
    with _answer(QMessageBox.Yes) as q:
        assert sw.reach(HOME_HOST, may_switch=False) == HOME_HOST
    q.assert_not_called()


def test_no_backend_falls_back_to_the_stored_host():
    sw = WiCANApSwitch(
        None, FakeSettings(), backend_factory=lambda: None, reachable=lambda *a: False
    )
    assert sw.reach(HOME_HOST) == HOME_HOST


def test_ap_out_of_range_scans_then_gives_up_quietly():
    wifi = FakeWifi(visible=[WifiNetwork("Home", "Home", AUTH_WPA2_PSK, 99)])
    sw = _switch(FakeSettings(), wifi)
    with _answer(QMessageBox.Yes) as q:
        assert sw.reach(HOME_HOST) == HOME_HOST
    assert wifi.scanned
    q.assert_not_called()


def test_location_off_explains_and_does_not_switch():
    wifi = FakeWifi()
    wifi.visible = lambda: (_ for _ in ()).throw(WifiLocationOff("denied"))
    sw = _switch(FakeSettings(), wifi)
    with (
        _answer(QMessageBox.Yes) as q,
        patch.object(mod.QMessageBox, "information") as info,
    ):
        assert sw.reach(HOME_HOST) == HOME_HOST
    info.assert_called_once()
    q.assert_not_called()


def test_first_join_without_a_profile_asks_the_password_and_learns_the_name():
    wifi = FakeWifi(visible=[WifiNetwork(AP, "", AUTH_WPA2_PSK, 80)])
    settings = FakeSettings(ssid="")
    sw = _switch(settings, wifi)
    with (
        _answer(QMessageBox.Yes),
        patch.object(mod.QInputDialog, "getText", return_value=("@meatpi#", True)),
    ):
        assert sw.reach(HOME_HOST) == WICAN_AP_HOST
    assert wifi.added == [(AP, AUTH_WPA2_PSK, "@meatpi#")]
    assert settings.ssid == AP


def test_cancelled_password_does_not_switch():
    wifi = FakeWifi(visible=[WifiNetwork(AP, "", AUTH_WPA2_PSK, 80)])
    settings = FakeSettings()
    sw = _switch(settings, wifi)
    with (
        _answer(QMessageBox.Yes),
        patch.object(mod.QInputDialog, "getText", return_value=("", False)),
    ):
        assert sw.reach(HOME_HOST) == HOME_HOST
    assert wifi.connects == [] and wifi.added == []
    assert settings.prev == ""


def test_unsupported_security_asks_to_join_by_hand():
    wifi = FakeWifi(visible=[WifiNetwork(AP, "", AUTH_OTHER, 80)])
    sw = _switch(FakeSettings(), wifi)
    with _answer(QMessageBox.Yes), patch.object(mod.QMessageBox, "information") as info:
        assert sw.reach(HOME_HOST) == HOME_HOST
    info.assert_called_once()
    assert wifi.connects == []


def test_failed_join_drops_the_new_profile_and_goes_back():
    wifi = FakeWifi(visible=[WifiNetwork(AP, "", AUTH_WPA2_PSK, 80)], join_works=False)
    settings = FakeSettings()
    sw = _switch(settings, wifi)
    with (
        _answer(QMessageBox.Yes),
        patch.object(mod.QInputDialog, "getText", return_value=("wrong", True)),
        patch.object(mod.QMessageBox, "warning") as warn,
    ):
        assert sw.reach(HOME_HOST) == HOME_HOST
    assert wifi.deleted == [AP]
    assert wifi.connects == [AP, "Home"]
    assert settings.prev == ""
    warn.assert_called_once()


# --------------------------------------------------------------------------- #
# offer_rejoin(): Disconnect / window close
# --------------------------------------------------------------------------- #


def test_rejoin_offered_and_accepted():
    wifi = FakeWifi(current=(AP, AP))
    settings = FakeSettings(prev="Home")
    with _answer(QMessageBox.Yes) as q:
        _switch(settings, wifi).offer_rejoin()
    q.assert_called_once()
    assert wifi.connects == ["Home"]
    assert settings.prev == ""


def test_rejoin_declined_is_not_asked_again():
    wifi = FakeWifi(current=(AP, AP))
    settings = FakeSettings(prev="Home")
    with _answer(QMessageBox.No):
        _switch(settings, wifi).offer_rejoin()
    assert wifi.connects == []
    assert settings.prev == ""


def test_no_rejoin_offer_when_nothing_was_switched():
    wifi = FakeWifi(current=(AP, AP))
    with _answer(QMessageBox.Yes) as q:
        _switch(FakeSettings(prev=""), wifi).offer_rejoin()
    q.assert_not_called()


def test_no_rejoin_offer_when_the_user_already_left_the_wican():
    wifi = FakeWifi(current=("Home", "Home"))
    settings = FakeSettings(prev="Home")
    with _answer(QMessageBox.Yes) as q:
        _switch(settings, wifi).offer_rejoin()
    q.assert_not_called()
    assert settings.prev == ""
