"""Tests for ``ECUProgrammingWindow._resolve_wican_host`` (connect-path re-resolve).

This is the DHCP-resilience seam: when the user picked the adapter via Scan, a
stable device_id is stored and every WiCAN connect re-resolves it to the
adapter's current IP. It is brick-adjacent — a wrong host could talk to the
wrong device — so every branch is fail-safe (fall back to the stored static
host). The method only uses its ``s`` (settings) argument, so it is exercised
against a duck-typed ``self`` with no QApplication / real window.
"""

from unittest.mock import MagicMock, patch

from src.ui.ecu_window import ECUProgrammingWindow

RESOLVE = "src.ecu.wican_discovery.resolve_host_for_device_id"


def _settings(host="192.168.1.169", device_id="dcb4d91511b9"):
    s = MagicMock()
    s.get_wican_host.return_value = host
    s.get_wican_device_id.return_value = device_id
    return s


def _resolve(s):
    # self is unused by the method, so any object works.
    return ECUProgrammingWindow._resolve_wican_host(object(), s)


class TestResolveWicanHost:
    def test_no_device_id_returns_static_host(self):
        s = _settings(device_id="")
        with patch(RESOLVE) as r:
            assert _resolve(s) == "192.168.1.169"
        r.assert_not_called()  # never even attempts discovery
        s.set_wican_host.assert_not_called()

    def test_fresh_ip_updates_cache_and_returns(self):
        s = _settings(host="192.168.1.10", device_id="dcb4d91511b9")
        with patch(RESOLVE, return_value="192.168.1.55"):
            assert _resolve(s) == "192.168.1.55"
        s.set_wican_host.assert_called_once_with("192.168.1.55")

    def test_device_offline_falls_back_to_static(self):
        s = _settings(host="192.168.1.10")
        with patch(RESOLVE, return_value=None):
            assert _resolve(s) == "192.168.1.10"
        s.set_wican_host.assert_not_called()

    def test_discovery_exception_falls_back_to_static(self):
        s = _settings(host="192.168.1.10")
        with patch(RESOLVE, side_effect=RuntimeError("boom")):
            assert _resolve(s) == "192.168.1.10"
        s.set_wican_host.assert_not_called()

    def test_unchanged_ip_is_not_re_persisted(self):
        s = _settings(host="192.168.1.169", device_id="dcb4d91511b9")
        with patch(RESOLVE, return_value="192.168.1.169"):
            assert _resolve(s) == "192.168.1.169"
        s.set_wican_host.assert_not_called()

    def test_device_id_read_failure_uses_static_host(self):
        s = _settings()
        s.get_wican_device_id.side_effect = RuntimeError("corrupt settings")
        with patch(RESOLVE) as r:
            assert _resolve(s) == "192.168.1.169"
        r.assert_not_called()

    def test_cache_write_failure_is_non_fatal(self):
        # A settings-write hiccup must never break the connect: we still return
        # the freshly resolved IP for this session.
        s = _settings(host="192.168.1.10")
        s.set_wican_host.side_effect = OSError("disk full")
        with patch(RESOLVE, return_value="192.168.1.55"):
            assert _resolve(s) == "192.168.1.55"
