"""Read-only feasibility probe of the Windows Native Wifi API (wlanapi.dll) via ctypes.

Lists interfaces, the current connection (SSID + profile), saved profiles, and visible
networks. Does NOT scan-trigger, connect, or change anything.
"""

import ctypes
from ctypes import wintypes as wt

wlan = ctypes.WinDLL("wlanapi.dll")


class GUID(ctypes.Structure):
    _fields_ = [
        ("d1", wt.DWORD),
        ("d2", wt.WORD),
        ("d3", wt.WORD),
        ("d4", ctypes.c_ubyte * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("guid", GUID), ("desc", ctypes.c_wchar * 256), ("state", wt.DWORD)]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("n", wt.DWORD), ("idx", wt.DWORD), ("items", WLAN_INTERFACE_INFO * 1)]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [("len", wt.ULONG), ("ssid", ctypes.c_char * 32)]


class WLAN_AVAILABLE_NETWORK(ctypes.Structure):
    _fields_ = [
        ("profile", ctypes.c_wchar * 256),
        ("ssid", DOT11_SSID),
        ("bss_type", wt.DWORD),
        ("n_bssids", wt.ULONG),
        ("connectable", wt.BOOL),
        ("not_conn_reason", wt.DWORD),
        ("n_phy", wt.ULONG),
        ("phy", wt.DWORD * 8),
        ("more_phy", wt.BOOL),
        ("signal", wt.ULONG),
        ("sec_enabled", wt.BOOL),
        ("auth", wt.DWORD),
        ("cipher", wt.DWORD),
        ("flags", wt.DWORD),
        ("reserved", wt.DWORD),
    ]


class WLAN_AVAILABLE_NETWORK_LIST(ctypes.Structure):
    _fields_ = [
        ("n", wt.DWORD),
        ("idx", wt.DWORD),
        ("items", WLAN_AVAILABLE_NETWORK * 1),
    ]


class WLAN_PROFILE_INFO(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar * 256), ("flags", wt.DWORD)]


class WLAN_PROFILE_INFO_LIST(ctypes.Structure):
    _fields_ = [("n", wt.DWORD), ("idx", wt.DWORD), ("items", WLAN_PROFILE_INFO * 1)]


class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("ssid", DOT11_SSID),
        ("bss_type", wt.DWORD),
        ("bssid", ctypes.c_ubyte * 6),
        ("phy_type", wt.DWORD),
        ("phy_idx", wt.ULONG),
        ("quality", wt.ULONG),
        ("rx", wt.ULONG),
        ("tx", wt.ULONG),
    ]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("state", wt.DWORD),
        ("mode", wt.DWORD),
        ("profile", ctypes.c_wchar * 256),
        ("assoc", WLAN_ASSOCIATION_ATTRIBUTES),
        # security attributes follow; not needed
        ("sec", ctypes.c_ubyte * 16),
    ]


def items(lst_ptr, item_type):
    n = lst_ptr.contents.n
    arr = ctypes.cast(
        ctypes.addressof(lst_ptr.contents.items), ctypes.POINTER(item_type * n)
    )
    return list(arr.contents)


def ssid_str(s):
    return s.ssid[: s.len].decode("utf-8", "replace")


h = wt.HANDLE()
ver = wt.DWORD()
assert wlan.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h)) == 0

ifl = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
assert wlan.WlanEnumInterfaces(h, None, ctypes.byref(ifl)) == 0
for iface in items(ifl, WLAN_INTERFACE_INFO):
    print(f"Interface: {iface.desc} state={iface.state}")
    g = ctypes.byref(iface.guid)

    size = wt.DWORD()
    data = ctypes.c_void_p()
    # wlan_intf_opcode_current_connection = 7
    rc = wlan.WlanQueryInterface(
        h, g, 7, None, ctypes.byref(size), ctypes.byref(data), None
    )
    if rc == 0:
        ca = ctypes.cast(data, ctypes.POINTER(WLAN_CONNECTION_ATTRIBUTES)).contents
        print(
            f"  Connected: ssid={ssid_str(ca.assoc.ssid)!r} profile={ca.profile!r} quality={ca.assoc.quality}"
        )
        wlan.WlanFreeMemory(data)
    else:
        print(f"  Not connected (rc={rc})")

    pl = ctypes.POINTER(WLAN_PROFILE_INFO_LIST)()
    assert wlan.WlanGetProfileList(h, g, None, ctypes.byref(pl)) == 0
    names = [p.name for p in items(pl, WLAN_PROFILE_INFO)]
    print(
        f"  Saved profiles: {len(names)}; WiCAN ones: {[n for n in names if n.startswith('WiCAN_')]}"
    )
    wlan.WlanFreeMemory(pl)

    nl = ctypes.POINTER(WLAN_AVAILABLE_NETWORK_LIST)()
    rc = wlan.WlanGetAvailableNetworkList(h, g, 0, None, ctypes.byref(nl))
    if rc == 0:
        nets = items(nl, WLAN_AVAILABLE_NETWORK)
        print(f"  Visible networks ({len(nets)}):")
        for n in nets:
            print(
                f"    {ssid_str(n.ssid)!r:32} signal={n.signal}% profile={n.profile!r}"
            )
        wlan.WlanFreeMemory(nl)
    else:
        print(
            f"  WlanGetAvailableNetworkList rc={rc} (5 = access denied, e.g. location off)"
        )

wlan.WlanFreeMemory(ifl)
wlan.WlanCloseHandle(h, None)
