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


class TestWifiSwitchWiring:
    """Connect asks the Wi-Fi switch for the host; Disconnect offers the rejoin."""

    def _window(self, sync_running=False, connected=False):
        from types import SimpleNamespace

        s = _settings()
        s.get_ecu_adapter.return_value = "wican"
        log_sync = MagicMock()
        log_sync.is_running = sync_running  # a property on the real object
        session = MagicMock()
        session.is_connected = connected
        win = SimpleNamespace(
            _main_window=SimpleNamespace(settings=s, wican_log_sync=log_sync),
            _ap_switch=MagicMock(),
            _poll_timer=MagicMock(),
            _session=session,
        )
        win._ap_switch.reach.side_effect = lambda host, may_switch: "192.168.0.10"
        win._resolve_wican_host = lambda s: "192.168.1.169"
        return win

    def test_connect_uses_the_host_the_switch_returns(self):
        win = self._window()
        cfg = ECUProgrammingWindow._build_adapter_config(win)
        assert cfg == {"kind": "wican", "host": "192.168.0.10"}
        win._ap_switch.reach.assert_called_once_with("192.168.1.169", may_switch=True)

    def test_no_switch_during_a_trip_log_download(self):
        win = self._window(sync_running=True)
        ECUProgrammingWindow._build_adapter_config(win)
        win._ap_switch.reach.assert_called_once_with("192.168.1.169", may_switch=False)

    def test_disconnect_offers_the_rejoin_once_closed(self):
        win = self._window(connected=False)
        ECUProgrammingWindow._on_disconnect(win)
        win._ap_switch.offer_rejoin.assert_called_once()

    def test_no_rejoin_offer_while_the_session_stays_open(self):
        # disconnect_ecu refuses while an operation runs (BUSY stays connected).
        win = self._window(connected=True)
        ECUProgrammingWindow._on_disconnect(win)
        win._ap_switch.offer_rejoin.assert_not_called()
