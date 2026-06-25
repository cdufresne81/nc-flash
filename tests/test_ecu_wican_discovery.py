"""Tests for WiCAN mDNS auto-discovery (``src/ecu/wican_discovery.py``).

The only zeroconf-touching seam is :func:`_browse`; everything else is pure and
duck-typed, so these tests drive the parser/dedup/resolve logic with a tiny fake
``ServiceInfo`` and a patched ``_browse`` — no network, no zeroconf required.
"""

import threading
import time
from unittest.mock import patch

import pytest

from src.ecu import wican_discovery as wd
from src.ecu.wican_discovery import (
    DiscoveryUnavailable,
    WiCANDevice,
    _id_matches,
    _parse_service_info,
    discover,
    resolve_host_for_device_id,
)


class _FakeInfo:
    """Duck-typed stand-in for zeroconf ``ServiceInfo``."""

    def __init__(self, addresses, server, port, properties):
        self._addresses = list(addresses)
        self.server = server
        self.port = port
        self.properties = properties

    def parsed_addresses(self):
        return list(self._addresses)


def _info(
    addresses=("192.168.1.169",),
    server="wican_dcb4d91511b9.local.",
    port=80,
    device_id="dcb4d91511b9",
    mac="DC:B4:D9:15:11:B8",
    firmware=None,
    hardware=None,
):
    props = {b"path": b"/"}
    if device_id is not None:
        props[b"device_id"] = device_id.encode()
    if mac is not None:
        props[b"mac"] = mac.encode()
    # Deployed firmware leaves these blank -> zeroconf yields None for the value.
    props[b"firmware"] = firmware.encode() if firmware else None
    props[b"hardware"] = hardware.encode() if hardware else None
    return _FakeInfo(addresses, server, port, props)


# --------------------------------------------------------------------------- #
# _parse_service_info
# --------------------------------------------------------------------------- #


class TestParseServiceInfo:
    def test_happy_path(self):
        dev = _parse_service_info("WiCAN-WebServer._wican._tcp.local.", _info())
        assert isinstance(dev, WiCANDevice)
        assert dev.name == "WiCAN-WebServer"
        assert dev.host == "192.168.1.169"
        assert dev.port == 80
        assert dev.hostname == "wican_dcb4d91511b9.local"  # trailing dot stripped
        assert dev.device_id == "dcb4d91511b9"
        assert dev.mac == "DC:B4:D9:15:11:B8"
        assert dev.stable_id == "DC:B4:D9:15:11:B8"  # mac preferred

    def test_blank_firmware_hardware_become_none(self):
        # The live deployed adapter returned empty firmware/hardware TXT values.
        dev = _parse_service_info(
            "x._wican._tcp.local.", _info(firmware="", hardware="")
        )
        assert dev.firmware is None
        assert dev.hardware is None

    def test_prefers_ipv4_over_ipv6(self):
        info = _info(addresses=("fe80::1", "192.168.1.50"))
        dev = _parse_service_info("x._wican._tcp.local.", info)
        assert dev.host == "192.168.1.50"

    def test_ipv6_only_still_usable(self):
        info = _info(addresses=("fe80::1234",))
        dev = _parse_service_info("x._wican._tcp.local.", info)
        assert dev.host == "fe80::1234"

    def test_no_address_skipped(self):
        info = _info(addresses=())
        assert _parse_service_info("x._wican._tcp.local.", info) is None

    def test_none_info_skipped(self):
        assert _parse_service_info("x._wican._tcp.local.", None) is None

    def test_stable_id_falls_back_to_device_id(self):
        dev = _parse_service_info("x._wican._tcp.local.", _info(mac=None))
        assert dev.mac is None
        assert dev.stable_id == "dcb4d91511b9"

    def test_label_is_descriptive(self):
        dev = _parse_service_info("WiCAN-WebServer._wican._tcp.local.", _info())
        assert "192.168.1.169" in dev.label
        assert "dcb4d91511b9" in dev.label


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #


class TestDiscover:
    def test_returns_parsed_devices(self):
        raw = [("WiCAN-WebServer._wican._tcp.local.", _info())]
        with patch.object(wd, "_browse", return_value=raw):
            devices = discover()
        assert len(devices) == 1
        assert devices[0].host == "192.168.1.169"

    def test_skips_unresolved_and_addressless(self):
        raw = [
            ("good._wican._tcp.local.", _info()),
            ("dead._wican._tcp.local.", None),  # never resolved
            ("noaddr._wican._tcp.local.", _info(addresses=(), device_id="zzz")),
        ]
        with patch.object(wd, "_browse", return_value=raw):
            devices = discover()
        assert [d.host for d in devices] == ["192.168.1.169"]

    def test_dedupes_by_stable_id(self):
        # Same device seen on two interfaces -> one entry.
        raw = [
            ("a._wican._tcp.local.", _info(addresses=("192.168.1.169",))),
            ("b._wican._tcp.local.", _info(addresses=("10.0.0.5",))),
        ]
        with patch.object(wd, "_browse", return_value=raw):
            devices = discover()
        assert len(devices) == 1

    def test_sorted_by_host(self):
        raw = [
            (
                "a._wican._tcp.local.",
                _info(
                    addresses=("192.168.1.50",),
                    mac="AA:AA:AA:AA:AA:AA",
                    device_id="aaa",
                ),
            ),
            (
                "b._wican._tcp.local.",
                _info(
                    addresses=("192.168.1.10",),
                    mac="BB:BB:BB:BB:BB:BB",
                    device_id="bbb",
                ),
            ),
        ]
        with patch.object(wd, "_browse", return_value=raw):
            devices = discover()
        assert [d.host for d in devices] == ["192.168.1.10", "192.168.1.50"]

    def test_missing_zeroconf_raises_discovery_unavailable(self):
        with patch.object(wd, "_browse", side_effect=ImportError("no zeroconf")):
            with pytest.raises(DiscoveryUnavailable):
                discover()

    def test_dedupes_by_host_when_no_stable_id(self):
        # A record with neither mac nor device_id has stable_id None -> dedup
        # falls back to host, so two such records on the same host collapse.
        raw = [
            ("a._wican._tcp.local.", _info(device_id=None, mac=None)),
            ("b._wican._tcp.local.", _info(device_id=None, mac=None)),
        ]
        with patch.object(wd, "_browse", return_value=raw):
            devices = discover()
        assert len(devices) == 1
        assert devices[0].stable_id is None


# --------------------------------------------------------------------------- #
# _id_matches / resolve_host_for_device_id
# --------------------------------------------------------------------------- #


class TestIdMatches:
    def _dev(self):
        return WiCANDevice(
            name="x",
            host="192.168.1.169",
            port=80,
            hostname="wican.local",
            device_id="dcb4d91511b9",
            mac="DC:B4:D9:15:11:B8",
        )

    def test_match_device_id(self):
        assert _id_matches(self._dev(), "dcb4d91511b9", "dcb4d91511b9")

    def test_match_mac_with_colons(self):
        t = "dc:b4:d9:15:11:b8"
        assert _id_matches(self._dev(), t, t.replace(":", ""))

    def test_match_mac_without_colons(self):
        assert _id_matches(
            self._dev(), "dcb4d9151118".replace("18", "b8"), "dcb4d91511b8"
        )

    def test_no_match(self):
        assert not _id_matches(self._dev(), "ffffffffffff", "ffffffffffff")


class TestResolveHostForDeviceId:
    def test_resolves_by_device_id(self):
        raw = [("a._wican._tcp.local.", _info())]
        with patch.object(wd, "_browse", return_value=raw):
            assert resolve_host_for_device_id("dcb4d91511b9") == "192.168.1.169"

    def test_resolves_by_mac_case_insensitive(self):
        raw = [("a._wican._tcp.local.", _info())]
        with patch.object(wd, "_browse", return_value=raw):
            assert resolve_host_for_device_id("dc:b4:d9:15:11:b8") == "192.168.1.169"

    def test_empty_device_id_short_circuits(self):
        # Must not even browse when there is no identity.
        with patch.object(wd, "_browse") as browse:
            assert resolve_host_for_device_id("") is None
        browse.assert_not_called()

    def test_no_match_returns_none(self):
        raw = [("a._wican._tcp.local.", _info())]
        with patch.object(wd, "_browse", return_value=raw):
            assert resolve_host_for_device_id("0badidentity0") is None

    def test_missing_zeroconf_returns_none(self):
        with patch.object(wd, "_browse", side_effect=ImportError("no zeroconf")):
            assert resolve_host_for_device_id("dcb4d91511b9") is None

    def test_ambiguous_identity_refuses_to_guess(self):
        # Same identity at two DISTINCT IPs (only possible with cloned MACs) ->
        # we must NOT guess which adapter to talk to; return None so the caller
        # falls back to the user's stored static host (brick-safety).
        raw = [
            ("a._wican._tcp.local.", _info(addresses=("192.168.1.50",))),
            ("b._wican._tcp.local.", _info(addresses=("192.168.1.99",))),
        ]
        with patch.object(wd, "_browse", return_value=raw):
            assert resolve_host_for_device_id("dcb4d91511b9") is None

    def test_same_identity_same_host_is_not_ambiguous(self):
        # The same device echoed twice at one IP is a single host -> resolves.
        raw = [
            ("a._wican._tcp.local.", _info()),
            ("a-dup._wican._tcp.local.", _info()),
        ]
        with patch.object(wd, "_browse", return_value=raw):
            assert resolve_host_for_device_id("dcb4d91511b9") == "192.168.1.169"

    def test_early_exit_predicate_fires_on_match(self):
        """The stop_when handed to _browse ends the scan once the target is seen."""
        captured = {}

        def fake_browse(timeout_s, stop_when=None):
            captured["stop_when"] = stop_when
            return [("a._wican._tcp.local.", _info())]

        with patch.object(wd, "_browse", side_effect=fake_browse):
            resolve_host_for_device_id("dcb4d91511b9")

        stop_when = captured["stop_when"]
        assert stop_when is not None
        # Matching device present -> stop immediately.
        assert stop_when({"a": _info()}) is True
        # Different device present -> keep listening.
        assert (
            stop_when({"b": _info(device_id="other", mac="00:00:00:00:00:00")}) is False
        )

    def test_resolve_predicate_survives_bad_info(self):
        """A None/garbage info in the collected dict must not break early-exit."""
        raw = [("a._wican._tcp.local.", _info())]

        def fake_browse(timeout_s, stop_when=None):
            # Predicate gets a None info (unresolved peer) — must not raise.
            assert stop_when({"dead": None}) is False
            return raw

        with patch.object(wd, "_browse", side_effect=fake_browse):
            assert resolve_host_for_device_id("dcb4d91511b9") == "192.168.1.169"


# --------------------------------------------------------------------------- #
# zeroconf_available
# --------------------------------------------------------------------------- #


class TestCancelSupport:
    """The cancel_event plumbing that lets a user-facing scan bail out early."""

    def test_discover_passes_cancel_event_to_browse(self):
        ev = threading.Event()
        captured = {}

        def fake_browse(timeout_s, stop_when=None, cancel_event=None):
            captured["cancel_event"] = cancel_event
            return []

        with patch.object(wd, "_browse", side_effect=fake_browse):
            discover(cancel_event=ev)
        assert captured["cancel_event"] is ev

    def test_wait_for_browse_returns_promptly_on_cancel(self):
        done = threading.Event()
        cancel = threading.Event()
        cancel.set()
        start = time.monotonic()
        wd._wait_for_browse(done, 5.0, cancel)
        assert time.monotonic() - start < 1.0  # did NOT wait the full 5s

    def test_wait_for_browse_returns_on_done(self):
        done = threading.Event()
        done.set()
        cancel = threading.Event()
        start = time.monotonic()
        wd._wait_for_browse(done, 5.0, cancel)
        assert time.monotonic() - start < 1.0  # early-exit predicate fired

    def test_wait_for_browse_without_cancel_uses_done_wait(self):
        # No cancel_event -> exact done.wait(timeout) semantics (returns at once
        # here because done is already set).
        done = threading.Event()
        done.set()
        start = time.monotonic()
        wd._wait_for_browse(done, 5.0, None)
        assert time.monotonic() - start < 1.0

    def test_wait_for_browse_honours_timeout(self):
        # Neither event set -> bounded by timeout_s, never blocks forever.
        done = threading.Event()
        cancel = threading.Event()
        start = time.monotonic()
        wd._wait_for_browse(done, 0.3, cancel)
        elapsed = time.monotonic() - start
        assert 0.2 < elapsed < 2.0


class TestZeroconfAvailable:
    def test_returns_bool(self):
        assert isinstance(wd.zeroconf_available(), bool)

    def test_false_when_import_fails(self):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "zeroconf":
                raise ImportError("simulated missing")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=fake_import):
            assert wd.zeroconf_available() is False
