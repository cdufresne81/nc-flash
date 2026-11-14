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
| `POST /datalog_session?op=begin&ttl_s=30`, body `{"format":"nc-flash-1","pids":[rows],"can_filters":[],"calculated":[]}` | 200 `{"ok":true,"token":N,...}`; 400 `bad_request` (the list must have 1 to 128 rows, each of service 01, 22 or 23); 409 `busy` (an ECU session owns the bus, or the WiCAN is going to sleep); 409 `inactive` (the WiCAN's own saved list has no polled channel, so its datalogger is not running); 500 `internal` (out of memory, nothing changed) |
| `POST /datalog_session?op=keepalive&token=N` | 200 `{"remaining_ms":..}`; 409 `stale` (no session, or another token). The host reads any 409 that is not a readable `busy` as stale |
| `POST /datalog_session?op=end&token=N` | 200; 409 `stale` = already ended |
| `GET /poll_status` | gains `"session_token": N` = the token of the session whose list the poller has loaded (0 on its saved list) and `"session_list": true|false`. `reload_pending` is true from the begin until the WiCAN has parsed that list; `reload_ok` and `pids_unpollable` are stale while it is |

Rows have the same schema as the saved file's `pids` (`to_firmware_row`). A begin closes any open
trip, forces recording on for the session (like NC Flash polling over a cable: it logs whenever the
ECU answers) and the next trip opens with the new columns. A begin during a session replaces the
list and issues a new token. Ending restores the WiCAN's previous recording mode. Old firmware has
no `session_list` in `/host_caps` and answers 404 on `/datalog_session`; nothing else means "old
firmware". The lease runs out at ttl + up to 1 s. The WiCAN refuses an empty list (400), and NC
Flash refuses to start with no channel ticked before asking. If the WiCAN can't close its open trip it stops
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
   ticked channel is invalid. Build the rows from the ticked channels only (like the host engine;
   an unticked channel is neither sent nor checked) and start the session thread. `is_running` is true from here.
2. **Sending** (session thread): look the WiCAN up (mDNS, up to 3 s) -> `/host_caps` -> begin
   (ttl 30 s) -> poll `/poll_status` until `session_token` equals the token begin returned and
   `reload_pending` is false. Only then are `reload_ok` and `pids_unpollable` read.
   - A pending reload is waited for up to 25 s (the WiCAN may need most of that to close its open
     trip). The lease is renewed every 10 s during the wait, and a `/poll_status` call that fails is
     simply tried again.
   - Nothing pending and `session_token` is not ours: final, no waiting. With `reload_ok:false` the
     WiCAN could not load the list; otherwise it dropped it (it went to sleep, or another NC Flash
     took over).
   - `session_list:true` alone is not proof (it can be an earlier session's table); it is used only
     when the status has no `session_token`, and then only the 25 s timeout can report a failure.
3. **Following**: the worker emits `follow`; the Datalogger starts the stream follower on the GUI
   thread. The worker renews the lease at once (the wait used part of it), then every 10 s.
   - Keepalive answers `stale` (Wi-Fi gap longer than the lease, or the WiCAN rebooted): begin
     again at once and wait for applied again (10 s per try, lease renewed meanwhile).
   - While the WiCAN is not on our list (it refuses the begin, the begin does not get through, or
     the switch is not confirmed), the run goes on and the status says, in plain words, that the
     WiCAN is logging its own list for now and NC Flash is sending the channels again, with the
     WiCAN's reason when it gave one. Stream statuses carry a short reminder until it took the list.
     Retries are 2 s apart; after 3 refusals in a row, 10 s apart.
   - A renewal that just does not get through is retried every 2 s, silently (the stream has its
     own reconnect and status).
4. **Stop**: status "Stopping...", the stream is stopped first, then the worker ends the session
   (3 s timeout; if it does not get through the lease runs out within 30 s). `is_running` turns
   false when the session thread has been cleaned up **and** the stream follower has reported its
   own end. A new start is refused until then: the follower reports from the event loop, after
   `stop()` returned, and that late report would otherwise stop the new run.
5. **The stream ends on its own** (error, another app took it over, no live stream in the
   firmware): the Datalogger stops the worker, which ends the session. No renewals without a stream.

### What can go wrong at start

| Case | Result |
|---|---|
| Old firmware (no capability, or 404) | The run follows the WiCAN's own list, as before. One status line says so; every stream status then ends with "(WiCAN's own list; update its firmware to log the channels shown here)", because the first line is replaced within milliseconds |
| `busy` | No run. Status: the WiCAN's link to the ECU is in use, or it is going to sleep |
| List refused (`bad_request`, with the WiCAN's message) / channels it can't poll / `internal` | The session is ended, no run, the reason is shown |
| `inactive` | No run. The WiCAN's message is shown (add one channel on its Logger page) |
| List not loaded, or dropped right after the begin | The session is ended, no run, said at once |
| Reload still pending after 25 s | The session is ended, no run: the WiCAN could not switch channels right now |
| WiCAN unreachable | No run: "the WiCAN is not reachable (check it is powered and on the same Wi-Fi)". URLs go to the log only |
| Begin sent, reply lost | The WiCAN may hold a list nobody has a token for. One try (5 s) to begin again and end that token; if that fails too, the lease clears it within 30 s. Skipped on app close |

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
