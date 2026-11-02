"""Read and switch the PC's Wi-Fi network (Windows only for now).

Used to join the WiCAN's own access point when the adapter can't be reached on
the current network (#129). No Qt and no ECU knowledge here: the UI decides
when to switch and asks the user first. Findings and design:
``docs/internal/WICAN_AP_AUTOSWITCH.md``.

Windows goes through the native Wi-Fi API (``wlanapi.dll``) via ctypes: no admin
rights, no extra package, and no parsing of ``netsh`` output (localised, so it
breaks on a French Windows).
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

# The firmware's access-point address is fixed (wifi_mgr.c), and its default
# SSID is "WiCAN_" + the device_id the adapter publishes over mDNS (main.c).
WICAN_AP_HOST = "192.168.0.10"
WICAN_AP_PREFIX = "WiCAN_"

# Security of a visible network, as far as joining it is concerned.
AUTH_OPEN = "open"
AUTH_WPA2_PSK = "wpa2-psk"
AUTH_OTHER = "other"


class WifiError(Exception):
    """A Wi-Fi query or switch failed."""


class WifiLocationOff(WifiError):
    """Windows refused the network list: location access is off for apps."""


@dataclass(frozen=True)
class WifiNetwork:
    """A visible network. ``profile`` is the saved Windows profile, or ""."""

    ssid: str
    profile: str = ""
    auth: str = AUTH_OTHER
    signal: int = 0


@dataclass(frozen=True)
class WifiConnection:
    """The network the PC is joined to right now."""

    ssid: str
    profile: str


def ap_ssid_for_device_id(device_id: Optional[str]) -> str:
    """The WiCAN's default access-point name for its mDNS ``device_id``, or ""."""
    ident = (device_id or "").replace(":", "").strip().lower()
    return f"{WICAN_AP_PREFIX}{ident}" if ident else ""


def is_wican_ap(ssid: str, saved_ssid: str = "") -> bool:
    """True if ``ssid`` is the saved WiCAN AP name (or, with none saved, any WiCAN AP)."""
    if not ssid:
        return False
    if saved_ssid:
        return ssid == saved_ssid
    return ssid.startswith(WICAN_AP_PREFIX)


def find_wican_ap(
    networks: list[WifiNetwork], saved_ssid: str = ""
) -> Optional[WifiNetwork]:
    """The WiCAN AP among ``networks``.

    With a saved name, only that exact network counts. With none (the adapter
    was never picked with Scan), the strongest ``WiCAN_*`` network is the guess;
    the user is shown its name before anything is joined.
    """
    matches = [n for n in networks if is_wican_ap(n.ssid, saved_ssid)]
    if not matches:
        return None
    return max(matches, key=lambda n: n.signal)


def tcp_reachable(host: str, port: int, timeout_s: float) -> bool:
    """True if a TCP connection to ``host:port`` opens within ``timeout_s``."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def wait_until(
    condition: Callable[[], bool],
    timeout_s: float,
    tick: Callable[[], bool],
    interval_s: float = 0.25,
) -> bool:
    """Poll ``condition`` until it holds or ``timeout_s`` passes.

    ``tick`` runs between polls (the UI pumps its events there) and returns
    False to cancel. Returns whether the condition held.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline or not tick():
            return False
        time.sleep(interval_s)


def profile_xml(ssid: str, auth: str, password: str = "") -> str:
    """A Windows WLAN profile for joining ``ssid`` by hand only.

    ``connectionMode`` is manual so Windows never joins the WiCAN on its own
    (it has no internet, and auto-joining it at home would cut the PC off).
    """
    if auth == AUTH_OPEN:
        security = (
            "<authEncryption><authentication>open</authentication>"
            "<encryption>none</encryption><useOneX>false</useOneX></authEncryption>"
        )
    elif auth == AUTH_WPA2_PSK:
        security = (
            "<authEncryption><authentication>WPA2PSK</authentication>"
            "<encryption>AES</encryption><useOneX>false</useOneX></authEncryption>"
            "<sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
            f"<keyMaterial>{escape(password)}</keyMaterial></sharedKey>"
        )
    else:
        raise WifiError(f"Can't create a profile for {ssid!r}: unsupported security")
    name = escape(ssid)
    return (
        '<?xml version="1.0"?>'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
        f"<name>{name}</name>"
        f"<SSIDConfig><SSID><name>{name}</name></SSID></SSIDConfig>"
        "<connectionType>ESS</connectionType>"
        "<connectionMode>manual</connectionMode>"
        f"<MSM><security>{security}</security></MSM>"
        "</WLANProfile>"
    )


def get_backend():
    """The Wi-Fi backend for this OS, or None where switching isn't supported."""
    if sys.platform != "win32":
        return None
    try:
        return _WindowsWifi()
    except (OSError, AttributeError) as exc:
        logger.debug("Windows Wi-Fi API unavailable: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Windows backend (wlanapi.dll)
# --------------------------------------------------------------------------- #

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as _wt

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("d1", _wt.DWORD),
            ("d2", _wt.WORD),
            ("d3", _wt.WORD),
            ("d4", ctypes.c_ubyte * 8),
        ]

    class _INTERFACE_INFO(ctypes.Structure):
        _fields_ = [
            ("guid", _GUID),
            ("desc", ctypes.c_wchar * 256),
            ("state", _wt.DWORD),
        ]

    class _INTERFACE_INFO_LIST(ctypes.Structure):
        _fields_ = [
            ("n", _wt.DWORD),
            ("idx", _wt.DWORD),
            ("items", _INTERFACE_INFO * 1),
        ]

    class _DOT11_SSID(ctypes.Structure):
        _fields_ = [("len", _wt.ULONG), ("ssid", ctypes.c_char * 32)]

    class _AVAILABLE_NETWORK(ctypes.Structure):
        _fields_ = [
            ("profile", ctypes.c_wchar * 256),
            ("ssid", _DOT11_SSID),
            ("bss_type", _wt.DWORD),
            ("n_bssids", _wt.ULONG),
            ("connectable", _wt.BOOL),
            ("not_conn_reason", _wt.DWORD),
            ("n_phy", _wt.ULONG),
            ("phy", _wt.DWORD * 8),
            ("more_phy", _wt.BOOL),
            ("signal", _wt.ULONG),
            ("sec_enabled", _wt.BOOL),
            ("auth", _wt.DWORD),
            ("cipher", _wt.DWORD),
            ("flags", _wt.DWORD),
            ("reserved", _wt.DWORD),
        ]

    class _AVAILABLE_NETWORK_LIST(ctypes.Structure):
        _fields_ = [
            ("n", _wt.DWORD),
            ("idx", _wt.DWORD),
            ("items", _AVAILABLE_NETWORK * 1),
        ]

    class _PROFILE_INFO(ctypes.Structure):
        _fields_ = [("name", ctypes.c_wchar * 256), ("flags", _wt.DWORD)]

    class _PROFILE_INFO_LIST(ctypes.Structure):
        _fields_ = [
            ("n", _wt.DWORD),
            ("idx", _wt.DWORD),
            ("items", _PROFILE_INFO * 1),
        ]

    class _ASSOCIATION_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("ssid", _DOT11_SSID),
            ("bss_type", _wt.DWORD),
            ("bssid", ctypes.c_ubyte * 6),
            ("phy_type", _wt.DWORD),
            ("phy_idx", _wt.ULONG),
            ("quality", _wt.ULONG),
            ("rx", _wt.ULONG),
            ("tx", _wt.ULONG),
        ]

    class _CONNECTION_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("state", _wt.DWORD),
            ("mode", _wt.DWORD),
            ("profile", ctypes.c_wchar * 256),
            ("assoc", _ASSOCIATION_ATTRIBUTES),
            ("sec", ctypes.c_ubyte * 16),  # security attributes, unused
        ]

    class _CONNECTION_PARAMETERS(ctypes.Structure):
        _fields_ = [
            ("mode", _wt.DWORD),
            ("profile", _wt.LPCWSTR),
            ("ssid", ctypes.c_void_p),
            ("bssids", ctypes.c_void_p),
            ("bss_type", _wt.DWORD),
            ("flags", _wt.DWORD),
        ]

    _OPCODE_CURRENT_CONNECTION = 7
    _STATE_CONNECTED = 1
    _CONNECTION_MODE_PROFILE = 0
    _BSS_INFRASTRUCTURE = 1
    _ERROR_ACCESS_DENIED = 5
    # DOT11_AUTH_ALGORITHM / DOT11_CIPHER_ALGORITHM values we can build a profile for.
    _AUTH_80211_OPEN = 1
    _AUTH_RSNA_PSK = 7
    _CIPHER_NONE = 0
    _CIPHER_CCMP = 4

    def _items(lst_ptr, item_type):
        n = lst_ptr.contents.n
        arr = ctypes.cast(
            ctypes.addressof(lst_ptr.contents.items), ctypes.POINTER(item_type * n)
        )
        return list(arr.contents)

    def _ssid_str(s) -> str:
        return s.ssid[: s.len].decode("utf-8", "replace")

    def _auth_kind(auth: int, cipher: int) -> str:
        if auth == _AUTH_80211_OPEN and cipher == _CIPHER_NONE:
            return AUTH_OPEN
        if auth == _AUTH_RSNA_PSK and cipher == _CIPHER_CCMP:
            return AUTH_WPA2_PSK
        return AUTH_OTHER

    class _WindowsWifi:
        """The first Wi-Fi interface, through wlanapi.dll. One handle per call."""

        def __init__(self):
            self._api = ctypes.WinDLL("wlanapi.dll")
            # Probe once so a PC without the WLAN service reports "unsupported"
            # instead of failing later.
            with self._session():
                pass

        # -- plumbing --

        def _session(self):
            api = self._api

            class _Session:
                def __enter__(s):
                    s.handle = _wt.HANDLE()
                    version = _wt.DWORD()
                    rc = api.WlanOpenHandle(
                        2, None, ctypes.byref(version), ctypes.byref(s.handle)
                    )
                    if rc != 0:
                        raise WifiError(f"WlanOpenHandle failed (error {rc})")
                    return s.handle

                def __exit__(s, *exc):
                    api.WlanCloseHandle(s.handle, None)

            return _Session()

        def _interface(self, handle) -> _GUID:
            lst = ctypes.POINTER(_INTERFACE_INFO_LIST)()
            rc = self._api.WlanEnumInterfaces(handle, None, ctypes.byref(lst))
            if rc != 0:
                raise WifiError(f"WlanEnumInterfaces failed (error {rc})")
            try:
                interfaces = _items(lst, _INTERFACE_INFO)
                if not interfaces:
                    raise WifiError("This PC has no Wi-Fi adapter")
                # Prefer the connected interface on a PC with several.
                chosen = next(
                    (i for i in interfaces if i.state == _STATE_CONNECTED),
                    interfaces[0],
                )
                return _GUID.from_buffer_copy(chosen.guid)
            finally:
                self._api.WlanFreeMemory(lst)

        # -- queries --

        def current(self) -> Optional[WifiConnection]:
            with self._session() as h:
                guid = self._interface(h)
                size = _wt.DWORD()
                data = ctypes.c_void_p()
                rc = self._api.WlanQueryInterface(
                    h,
                    ctypes.byref(guid),
                    _OPCODE_CURRENT_CONNECTION,
                    None,
                    ctypes.byref(size),
                    ctypes.byref(data),
                    None,
                )
                if rc != 0:
                    return None  # not connected
                try:
                    attrs = ctypes.cast(
                        data, ctypes.POINTER(_CONNECTION_ATTRIBUTES)
                    ).contents
                    if attrs.state != _STATE_CONNECTED:
                        return None
                    return WifiConnection(_ssid_str(attrs.assoc.ssid), attrs.profile)
                finally:
                    self._api.WlanFreeMemory(data)

        def scan(self) -> None:
            """Ask Windows for a fresh scan (results land a few seconds later)."""
            with self._session() as h:
                guid = self._interface(h)
                rc = self._api.WlanScan(h, ctypes.byref(guid), None, None, None)
                if rc == _ERROR_ACCESS_DENIED:
                    raise WifiLocationOff("Wi-Fi scan refused (location access off)")
                if rc != 0:
                    logger.debug("WlanScan failed (error %s); using cached list", rc)

        def visible(self) -> list[WifiNetwork]:
            with self._session() as h:
                guid = self._interface(h)
                lst = ctypes.POINTER(_AVAILABLE_NETWORK_LIST)()
                rc = self._api.WlanGetAvailableNetworkList(
                    h, ctypes.byref(guid), 0, None, ctypes.byref(lst)
                )
                if rc == _ERROR_ACCESS_DENIED:
                    raise WifiLocationOff("Wi-Fi list refused (location access off)")
                if rc != 0:
                    raise WifiError(f"WlanGetAvailableNetworkList failed (error {rc})")
                try:
                    # One entry per (SSID, profile) pair: keep the saved-profile
                    # entry for an SSID when there is one.
                    by_ssid: dict[str, WifiNetwork] = {}
                    for n in _items(lst, _AVAILABLE_NETWORK):
                        net = WifiNetwork(
                            ssid=_ssid_str(n.ssid),
                            profile=n.profile,
                            auth=_auth_kind(n.auth, n.cipher),
                            signal=int(n.signal),
                        )
                        if not net.ssid:
                            continue
                        old = by_ssid.get(net.ssid)
                        if old is None or (net.profile and not old.profile):
                            by_ssid[net.ssid] = net
                    return list(by_ssid.values())
                finally:
                    self._api.WlanFreeMemory(lst)

        # -- changes --

        def add_profile(self, ssid: str, auth: str, password: str = "") -> str:
            """Save a join-by-hand-only profile for ``ssid``; returns its name."""
            xml = profile_xml(ssid, auth, password)
            with self._session() as h:
                guid = self._interface(h)
                reason = _wt.DWORD()
                rc = self._api.WlanSetProfile(
                    h,
                    ctypes.byref(guid),
                    0,
                    _wt.LPCWSTR(xml),
                    None,
                    True,
                    None,
                    ctypes.byref(reason),
                )
                if rc != 0:
                    raise WifiError(
                        f"Saving the Wi-Fi profile failed (error {rc}, "
                        f"reason {reason.value})"
                    )
            return ssid

        def delete_profile(self, profile: str) -> None:
            with self._session() as h:
                guid = self._interface(h)
                rc = self._api.WlanDeleteProfile(
                    h, ctypes.byref(guid), _wt.LPCWSTR(profile), None
                )
                if rc != 0:
                    logger.debug("WlanDeleteProfile(%r) failed (error %s)", profile, rc)

        def connect(self, profile: str) -> None:
            """Start joining the network of saved ``profile`` (returns at once)."""
            params = _CONNECTION_PARAMETERS(
                _CONNECTION_MODE_PROFILE,
                profile,
                None,
                None,
                _BSS_INFRASTRUCTURE,
                0,
            )
            with self._session() as h:
                guid = self._interface(h)
                rc = self._api.WlanConnect(
                    h, ctypes.byref(guid), ctypes.byref(params), None
                )
                if rc != 0:
                    raise WifiError(f"WlanConnect({profile!r}) failed (error {rc})")
