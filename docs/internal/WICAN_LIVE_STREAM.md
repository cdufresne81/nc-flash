# WiCAN Live Datalog (firmware issue #3)

NC Flash follows the trip the WiCAN is recording, as it records, and writes it to a local CSV that
MegaLogViewerHD trails. Firmware side: `main/datalog_stream.c` and `docs/internals/datalog_stream.md`
in the firmware repo. This doc covers the contract and the host side.

History: a first version (July 2026, branch `feature/live-datalog-stream` in both repos) worked end to
end but was parked. Its 16 KB internal-RAM ring left the trip logger unable to allocate its 22.5 KB
queue ("CSV start failed (out of memory)"). It also started and stopped trips from NC Flash through
leases and a heartbeat. The current version keeps every stream buffer in PSRAM, and it is **tail only**.

## Contract

- **Port 35002**, raw TCP, always on. The device only serves; the host sends nothing.
- **One client, newest wins.** A new connection closes the old one (after `#bye replaced`), so a PC
  that drops off Wi-Fi gets straight back in. A client told `#bye replaced` must NOT reconnect, or two
  clients take turns forever. Never open a second connection to "probe" while a stream runs. The
  stream's own `connect()` is the capability check (refused, or a first line other than
  `#hello NCDLv1`, means firmware without the stream, or a device that is off).
- **Silence means dead.** The host never sends, and a device that sleeps (Wi-Fi torn down) or reboots
  never gets a reset to the PC. So the device sends `#ping` after 3 s with nothing else to say, and
  the host treats 15 s of silence as a dead link (plus TCP keepalive as a backstop).
- **Tail only.** The stream never starts, stops or rotates a trip, and touches no park/claim/lease
  state. A stream client is not "host present" for the dead-man reaper.
- **Live only.** No backfill. Rows the client misses (not connected, Wi-Fi too slow, reconnect gap)
  are still in the SD trip file, which stays the complete record.

Wire protocol `NCDLv1` (every line ends in `\n`; ignore unknown `#` lines):

| Line | Meaning |
|---|---|
| `#hello NCDLv1 fw=<git_version>` | First line of every connection |
| `#idle` | No trip open right now |
| `#session file=<name> cols=<n>` | A trip file is open (on connect, or when one opens/rotates). The next line is its CSV header |
| `#nohdr` | Sent instead of the header when it is unavailable (header longer than 8 KB) |
| `#drop <n>` | n rows skipped here because the client fell behind |
| `#close` | The trip file closed; the socket stays open |
| `#ping` | Nothing else sent for 3 s (liveness only) |
| `#bye replaced` | A newer client took the slot; the connection closes. Do not reconnect |
| anything else | A CSV row, byte-identical to the SD file |

## Host side

- `src/ecu/wican_stream.py` — THE NCDLv1 line reader (stdlib only). `WiCANLiveStreamClient.connect()`
  validates the banner; `run(on_event)` delivers typed events; `stop()` is thread-safe.
- `src/ui/wican_live_datalog.py` — `WiCANLiveDatalog`, owned by the main window. Worker on a real
  QThread. Captures go to `{logs_directory}/live/live_<YYYYmmdd_HHMMSS>.csv`, header first, every row
  flushed.
  - **One file per column set, not per trip.** A new device trip (or a size rotation) with the same
    header keeps appending to the open capture, so MegaLogViewerHD keeps following it. A changed header
    starts a new file. `#nohdr` with the same channel count also continues.
  - **A device restart starts a new file.** `timestamp_ms` counts from device boot, so a row whose
    timestamp is lower than the last one written means the device rebooted between trips: the
    capture is closed and a new one opens with the same header (time never runs backwards in a file).
  - **Reconnect.** After a working stream drops (an error, or 15 s of silence), the worker retries
    every 2 s, keeping the capture open, and looks the host up again (mDNS, up to 3 s, not
    interruptible) only after a connect attempt fails; the device's resync (`#session` +
    header) continues the same file. A first connect that fails is reported as "not reachable, or no
    live datalog", with no retry. `#bye replaced` stops the run with no reconnect.
- `src/ui/mlv_launch.py` — the single MegaLogViewerHD launcher. `launch_trail()` writes
  `<capture>.mlv.properties` (`fileName=` forward-slashed, `trailFile=true`, `startPlayback=true`)
  and passes it as MLV's only argument. MLV engages its trail only at 50 rows and gives up after 60 s,
  so the auto-open (setting `ecu/live_datalog_auto_trail`, on by default) fires when
  a capture reaches 50 rows, once per capture file, so a new file (device restart, new
  channels) opens a new MegaLogViewerHD on it; continuing rows never relaunch it.
- Trip Logs window — Live Datalog box: Start/Stop, status, "Open in MegaLogViewerHD", auto-open
  checkbox.

## Interlocks

| While... | ...these lock | Why |
|---|---|---|
| Live datalog runs | ECU window Connect + every ECU op (WiCAN adapter only); trip-log download | An ECU session reserves the bus, which pauses the very trip being followed |
| ECU window holds a WiCAN session (`wican_link_changed`) | Live Datalog start | Same reason, other direction |
| ECU op busy | Live Datalog start, download | Unchanged PR #89 rule |
| Trip-log download runs | Live Datalog start | SD/CPU/Wi-Fi contention on the device |

`_start_flash` also stops a running live datalog as a backstop for non-button paths. The ECU window's
auto-connect on open and its post-operation WiCAN reconnect both refuse (with a log line) while a live
datalog runs. `TripLogsWindow.start_download()` (the single download path, also used by the startup
prompt) refuses while a live datalog runs.

`WiCANLiveDatalog.start()` refuses until the previous run's queued cleanup has run (`_thread` is
None), not merely until `is_running` is false: a start in that gap would let the old cleanup
`deleteLater()` the new, running thread, which aborts the app.

## Bench results (2026-10-15, firmware `v1.25.1-1-g0e32cc9`)

- Banner and `#idle` on connect; manual Start gives `#session` + header + rows at ~22 Hz (mean 45 ms,
  max 104 ms), 0 dropped.
- All 251 streamed rows byte-identical to the closed SD file; mid-session joiner gets the header;
  newest-wins EOFs the old client; `#close` keeps the socket open.
- Largest free internal block 26,624 B with the stream allocated (the CSV queue needs 22,528 B).
- NC Flash owner end to end: two device trips with the same columns in one capture (one header),
  reconnect after a forced disconnect continued the same file.
