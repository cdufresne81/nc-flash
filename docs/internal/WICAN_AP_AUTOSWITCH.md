# WiCAN access-point auto-switch: feasibility findings (#129)

**TLDR:** Feasible on Windows with no admin rights and no new dependency (native Wi-Fi API via
ctypes, proven read-only on this machine). The AP name can be learned for free while on the home
network and saved in settings; the AP **password cannot** (the firmware redacts it). The
switch is only useful when the WiCAN is in `AP` or `APStation` mode; in `Station` mode there is no
AP to join. macOS/Linux are possible but unverified here.

Investigated 2026-09-30. Nothing was switched: this PC is Wi-Fi-only, so joining the WiCAN AP
would cut the dev session (and every peer session) off the internet.

## What the firmware does (nc-flash-wican-fw)

| Fact | Evidence |
|---|---|
| AP SSID = `WiCAN_` + **SoftAP** MAC, lowercase hex, no colons | `main/main.c:721` (`ESP_MAC_WIFI_SOFTAP`), `:730` |
| Same hex string is the device `uid`, published as mDNS TXT `device_id` | `main/main.c:727`, `main/wc_mdns.c:56` |
| mDNS TXT `mac` is the **Station** MAC (SoftAP MAC − 1 on ESP32) | `main/wc_mdns.c:40`; bench: saved `mac` `DC:B4:D9:15:11:B8`, Windows profile `WiCAN_dcb4d91511b9` |
| Custom SSID possible (`ap_ssid_en`=enable, `ap_ssid`, 3–32 chars) | `main/config_server.c:955-975`, `:2789-2817` |
| AP address is fixed **192.168.0.10/24** (not 192.168.80.1 as the issue first said) | `main/wifi_mgr.c:820-825` |
| Default AP password `@meatpi#` | `main/config_server.c:212` |
| `ap_pass` is redacted in `/check_status` and `/load_config` | `main/config_server.c:763-772` |
| `/check_status` exposes `wifi_mode`, `ap_ssid_en`, `ap_ssid`, `ap_auto_disable`, `device_id` | `main/config_server.c:1670-1674`, `:1786` |
| `wifi_mode` ∈ `AP`, `APStation`, `Station`, `BLEStation`, `SmartConnect` | `main/config_server.c:371-387` |
| In `APStation` with `ap_auto_disable`, the AP turns off once the STA link is up and back on ~1 s after it drops | `main/wifi_mgr.c:1566-1578`, `:1443-1455` |

Consequence: with `APStation` + auto-disable, the AP appears exactly when the WiCAN can't reach
home Wi-Fi, i.e. in the car. That is the case this feature targets. `SmartConnect` (home + drive
networks) may already cover the "phone hotspot in the car" setup without any switching.

## Storing the AP name (answer to "can we save it?")

Yes, at two existing points, both while NC Flash can reach the WiCAN on the home network:

1. **Settings → Scan pick** (`src/ui/settings_dialog.py:1676-1708`): the chosen `WiCANDevice`
   already carries the mDNS `device_id` (`src/ecu/wican_discovery.py:156`). Save
   `ecu/wican_ap_ssid = "WiCAN_" + device_id` next to `ecu/wican_device_id`. No extra request.
2. **Settings → Test connection** (`settings_dialog.py:1302`, currently SLCAN-only) or the
   first successful ECU Connect: one `GET /check_status` refreshes the AP SSID if a custom one is
   enabled, and records `wifi_mode` / `ap_auto_disable`, so NC Flash knows whether an AP exists at
   all. Read-only, so no bench lock or bus reservation needed.

Don't derive the SSID from the stored `mac` (+1 arithmetic): use the `device_id` TXT, which is
the firmware's own string.

**Password:** can't be read from the device. Options, best first: rely on an existing Windows
profile for that SSID (this PC already has `WiCAN_dcb4d91511b9`); else ask once and hand it to
the OS profile (Windows stores it); try `@meatpi#` only as a labelled fallback. Never put it in
QSettings (plain text in the registry).

## OS side

**Windows (proven read-only, `tools/wlan_probe.py`):** `wlanapi.dll` via ctypes, no admin, no
package. It read the current SSID + profile, all 20 saved profiles and the visible-network list.
Use the API, not `netsh`: `netsh` output is localised (French Windows breaks text parsing).
Calls needed: `WlanScan` (refresh), `WlanGetAvailableNetworkList`,
`WlanQueryInterface(current_connection)` (to remember the previous network),
`WlanSetProfile` (only if no profile exists), `WlanConnect`, then `WlanConnect` back.
Location access must be on for scans on Windows 11 24H2+ (it is on here); detect error 5 and
explain it.

**macOS (unverified):** `networksetup -setairportnetwork <if> <ssid> <pass>` can join, but since
macOS 14/15 reading SSIDs/scan results needs Location Services for the app (CoreWLAN + an
`NSLocationUsageDescription` in the bundle). Needs a Mac test.

**Linux (unverified):** `nmcli device wifi connect <ssid> [password <p>]`; normally works
unprivileged for the desktop user via polkit.

## Proposed shape

- `src/utils/wifi_switch.py`: OS abstraction, no Qt, no ECU knowledge (`current()`,
  `visible_ssids()`, `connect(ssid)`, `has_profile(ssid)`). Windows backend first.
- Hook: the ECU window **Connect** path only, in `_resolve_wican_host`
  (`src/ui/ecu_window.py:380`), after mDNS and the stored host both fail. If the saved AP SSID is
  visible → ask → join → use `192.168.0.10` for this session only.
- Don't cache `192.168.0.10` as the new `ecu/wican_host`: today a fresh mDNS result is written
  back (`ecu_window.py:405-411`), which would overwrite the home IP.
- Offer to rejoin the previous network on Disconnect / app close.
- Never from background work (`wican_log_sync` auto-on-launch) and never while connected.

## Safety

- Never switch Wi-Fi while a session is open. Before the point of no return, a Wi-Fi drop leaves
  the ECU in programming session at worst; after it, the firmware finishes from SD
  (`WICAN_FLASH_SEQUENCE.md` ⑦/⑧). A deliberate switch still has no business being there.
- **Unconfirmed:** with home Wi-Fi also in range (bench), Windows might roam off the
  "no internet" WiCAN AP back to an auto-connect network mid-session. Test on the bench before
  shipping; if it happens, create the WiCAN profile with `connectionMode=manual` and consider
  temporarily clearing auto-connect on the home profile.
- Switching drops the laptop's internet: always ask, never silent.

## Still to verify

1. Bench `wifi_mode` / `ap_auto_disable` (the WiCAN answered ping but not HTTP during this
   investigation, so `/check_status` couldn't be read).
2. A real switch → connect → read → switch back, on a second machine or with Ethernet plugged in.
3. macOS behaviour.
