# WiCAN per-run channels (temporary channel list)

With a Tactrix or an OBDX, Start Logging logs the channels ticked in the Datalogger window. With a
WiCAN the same must be true, without changing what the WiCAN logs on its own.

## The problem

The WiCAN polls the ECU itself from its saved channel file (`auto_pid.json`). The only way to make it
log other channels used to be the "Send Channels to WiCAN" button, which **overwrites that file**:
the change stays forever (also if NC Flash crashes), may wait for an ignition cycle when a trip is
open, and Start Logging itself only followed whatever the WiCAN recorded.

## The solution

The firmware accepts a **temporary list held in RAM only**. While it is in use the saved file is
shelved, never written. The temporary list ends on an explicit end, when its lease runs out (NC Flash
vanished), when the WiCAN sleeps, or when it reboots; the WiCAN then returns to its saved list by
itself.

Start Logging (WiCAN) = begin -> wait until applied -> follow the live stream (port 35002, unchanged,
see `WICAN_LIVE_STREAM.md`) -> renew the lease; Stop = stop the stream, then end.

## Firmware contract (summary)

| Call | Reply |
|---|---|
| `GET /host_caps` | has `"session_list": 1` when the firmware supports this |
| `POST /datalog_session?op=begin&ttl_s=30`, body `{"format":"nc-flash-1","pids":[rows],"can_filters":[],"calculated":[]}` | 200 `{"ok":true,"token":N,...}`; 400 `bad_request` (also 0 rows, more than 128 rows, or a row that is not service 01/22/23); 409 `busy` (an ECU session owns the bus, or the WiCAN is going to sleep); 409 `inactive` (the WiCAN's own saved list has no polled channel, so its datalogger is not running); 500 `internal` (out of memory, nothing changed) |
| `POST /datalog_session?op=keepalive&token=N` | 200 `{"remaining_ms":..}`; 409 `stale` (no session, or another token) |
| `POST /datalog_session?op=end&token=N` | 200; 409 `stale` = already ended |
| `GET /poll_status` | gains `"session_token": N` = the token of the session whose list the poller has loaded (0 on its saved list) and `"session_list": true|false`. `reload_ok` and `pids_unpollable` are stale until the list is applied |

Rows have the same schema as the saved file's `pids` (`to_firmware_row`). A begin closes any open
trip, forces recording on for the session (like NC Flash polling over a cable: it logs whenever the
ECU answers) and the next trip opens with the new columns. A begin during a session replaces the
list and issues a new token. Ending restores the WiCAN's previous recording mode. Old firmware has
no `session_list` in `/host_caps` and answers 404 on `/datalog_session`; nothing else means "old
firmware". The lease runs out at ttl + up to 1 s. The WiCAN accepts an empty `pids`, so NC Flash's
own "Select at least one channel" is the only guard. If the WiCAN can't close its open trip it stops
trying after 15-20 s and `reload_pending` stays true until that trip ends.

## Host side

- `src/ecu/wican_http.py`: `WiCANHttpError` carries `status` and `body` (messages unchanged).
- `src/ecu/wican_autopid.py`: `WiCANSessionList` (`supported`, `begin`, `keepalive`, `end`,
  `wait_applied`) and `session_document`. Errors by kind: `WiCANSessionUnsupported` (404),
  `WiCANSessionBusy`, `WiCANSessionStale`, another `WiCANAutoPidError` (refused, with the reason),
  `WiCANHttpError` (network).
- `src/ui/datalogger.py`: `Datalogger` owns the run. `_WicanSessionWorker` (its own QThread) holds
  the temporary list; `WiCANLiveDatalog` still follows the stream on its thread.

### Run lifecycle

1. **Start pressed** (GUI thread): refuse if blocked, no WiCAN set up, no channel enabled, or a
   channel is invalid. Build the rows (every channel with its enabled flag) and start the session
   thread. `is_running` is true from here.
2. **Sending** (session thread): look the WiCAN up (mDNS, up to 3 s) -> `/host_caps` -> begin
   (ttl 30 s) -> poll `/poll_status` until `session_token` equals the token begin returned and
   `reload_pending` is false (25 s at most; only then are `reload_ok` and `pids_unpollable` read).
   `session_list:true` alone is not proof (it can be an earlier session's table); it is used only
   when the status has no `session_token`.
3. **Following**: the worker emits `follow`; the Datalogger starts the stream follower on the GUI
   thread. The worker renews the lease every 10 s.
   - Keepalive answers `stale` (Wi-Fi gap longer than the lease, or the WiCAN rebooted): begin
     again at once and wait for applied again (10 s at most). A failed wait is only logged; the
     run carries on and renews 2 s later.
   - Any other failure: retry every 2 s, silently (the stream has its own reconnect and status).
4. **Stop**: the stream is stopped first, then the worker ends the session (3 s timeout; if it does
   not get through the lease runs out within 30 s). `is_running` turns false when the session thread
   has been cleaned up. A new start is refused until then.
5. **The stream ends on its own** (error, another app took it over, no live stream in the
   firmware): the Datalogger stops the worker, which ends the session. No renewals without a stream.

### What can go wrong at start

| Case | Result |
|---|---|
| Old firmware (no capability, or 404) | The run follows the WiCAN's own list, as before. One status line says so and that a firmware update lets Start Logging use the channels shown; stream statuses carry a short reminder |
| `busy` | No run. Status: the WiCAN's link to the ECU is in use, or it is going to sleep |
| List refused (`bad_request`, with the WiCAN's message) / channels it can't poll / `internal` | The session is ended, no run, the reason is shown |
| `inactive` | No run. The WiCAN's message is shown (add one channel on its Logger page) |
| Not applied in 25 s | The session is ended, no run: the WiCAN could not switch channels right now |
| WiCAN unreachable | No run, the reason is shown |

App close (`shutdown`) waits for the session thread (up to 20 s worst case: lookup + a call in
flight + the end call) so its QThread is never destroyed running.

### ECU window

An ECU session must never start while the WiCAN still polls a temporary list.

- Connect and every ECU operation are locked while `Datalogger.is_running`, which stays true after
  Stop until the session thread has made its end call and been cleaned up.
- The backstop in `_start_flash` calls `Datalogger.shutdown()`, which blocks until the stream is
  stopped and the end call has returned (3 s timeout), then the operation goes on.
- If the end call does not get through (Wi-Fi), the list stays on the WiCAN until its lease runs
  out (30 s). The firmware side covers that case.

### The saved list

"Save as WiCAN's Own List..." (the old Send Channels button) still overwrites `auto_pid.json` with
its preview, confirmation and restore. It is optional, changes what the WiCAN logs without NC Flash,
and is refused while a run is active.

## Not bench-tested yet

Both sides were written against the contract above; nothing here has run on a real WiCAN. To check
with the matching firmware build:

- Start with a trip open and with none: the trip rolls, the capture has the ticked channels.
- Stop: the WiCAN is back on its own list and its previous recording mode; `auto_pid.json` unchanged.
- Kill NC Flash mid-run: the list expires after ~30 s on its own.
- Wi-Fi gap longer than 30 s, and a WiCAN reboot mid-run: the list is sent again and the capture
  carries on (new file only if the columns changed).
- ECU window connected (or a flash running) from another PC: Start reports the link is in use.
- A channel the WiCAN can't poll: the run does not start and names the problem.
- A WiCAN with released firmware: the fallback line shows and the run follows its own list.
- `begin` sets `reload_pending` before it replies (otherwise a `reload_ok:false` left from an
  earlier reload would read as a refused list).
- Start an ECU operation right after Stop, and with the run still active (backstop): the WiCAN is
  back on its saved list before the ECU session begins.
- An open trip the WiCAN can't close: Start gives up after 25 s with the "right now" message.
- Keepalive and end are sent as a POST with an empty JSON body (`{}`); the firmware must not mind.
