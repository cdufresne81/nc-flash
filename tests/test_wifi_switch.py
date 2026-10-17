"""Tests for ``src.utils.wifi_switch`` (the OS-independent parts, #129)."""

import socket
import xml.etree.ElementTree as ET

import pytest

from src.utils import wifi_switch as ws
from src.utils.wifi_switch import WifiNetwork

NS = "{http://www.microsoft.com/networking/WLAN/profile/v1}"


def test_ap_ssid_for_device_id():
    assert ws.ap_ssid_for_device_id("DCB4D91511B9") == "WiCAN_dcb4d91511b9"
    assert ws.ap_ssid_for_device_id("dc:b4:d9:15:11:b9") == "WiCAN_dcb4d91511b9"
    assert ws.ap_ssid_for_device_id("") == ""
    assert ws.ap_ssid_for_device_id(None) == ""


def test_is_wican_ap_with_and_without_a_saved_name():
    assert ws.is_wican_ap("WiCAN_abc", "")
    assert not ws.is_wican_ap("HomeWiFi", "")
    assert ws.is_wican_ap("MyCar", "MyCar")
    # With a saved name, another WiCAN's AP is not ours.
    assert not ws.is_wican_ap("WiCAN_other", "WiCAN_abc")
    assert not ws.is_wican_ap("", "")


def test_find_wican_ap_prefers_the_saved_name():
    nets = [
        WifiNetwork("WiCAN_other", signal=90),
        WifiNetwork("WiCAN_mine", signal=40),
        WifiNetwork("Home", signal=99),
    ]
    assert ws.find_wican_ap(nets, "WiCAN_mine").ssid == "WiCAN_mine"
    assert ws.find_wican_ap(nets, "WiCAN_gone") is None


def test_find_wican_ap_without_saved_name_takes_the_strongest():
    nets = [WifiNetwork("WiCAN_a", signal=30), WifiNetwork("WiCAN_b", signal=70)]
    assert ws.find_wican_ap(nets).ssid == "WiCAN_b"
    assert ws.find_wican_ap([WifiNetwork("Home", signal=99)]) is None


def test_profile_xml_wpa2_is_manual_and_escaped():
    xml = ws.profile_xml("WiCAN_<x>", ws.AUTH_WPA2_PSK, "p&ss<word>")
    root = ET.fromstring(xml)
    assert root.find(f"{NS}name").text == "WiCAN_<x>"
    # Never auto-joined: the WiCAN has no internet.
    assert root.find(f"{NS}connectionMode").text == "manual"
    auth = root.find(f".//{NS}authentication").text
    key = root.find(f".//{NS}keyMaterial").text
    assert (auth, key) == ("WPA2PSK", "p&ss<word>")


def test_profile_xml_open_has_no_key():
    root = ET.fromstring(ws.profile_xml("WiCAN_x", ws.AUTH_OPEN))
    assert root.find(f".//{NS}authentication").text == "open"
    assert root.find(f".//{NS}keyMaterial") is None


def test_profile_xml_refuses_unsupported_security():
    with pytest.raises(ws.WifiError):
        ws.profile_xml("WiCAN_x", ws.AUTH_OTHER, "pw")


def test_wait_until_returns_when_condition_holds():
    calls = iter([False, False, True])
    assert ws.wait_until(lambda: next(calls), 5.0, lambda: True, interval_s=0)


def test_wait_until_stops_on_cancel_and_timeout():
    assert not ws.wait_until(lambda: False, 5.0, lambda: False, interval_s=0)
    assert not ws.wait_until(lambda: False, 0.0, lambda: True, interval_s=0)


def test_tcp_reachable():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert ws.tcp_reachable("127.0.0.1", port, 1.0)
    finally:
        server.close()
    assert not ws.tcp_reachable("127.0.0.1", port, 0.5)
