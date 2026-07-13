# WiCAN Live Datalog Stream (fw issue #3) — design

Status: DESIGN — 2026-07-10. Firmware branch `feature/live-datalog-stream` on
`../nc-flash-wican-fw`; host branch `feature/live-datalog-stream` here.

Goal: NC Flash live-tails the WiCAN's active wide-CSV datalog over WiFi
(MegaLogViewerHD-style): the device streams exactly the rows it writes to SD,
NC Flash receives them and appends to a growing local `.csv` that MLV (or
anything else) can tail. **NC Flash initiates** — the device only ever serves.

## Decisions (with the constraint that forced each)

| Decision | Choice | Why |
|---|---|---|
| Transport | New dedicated raw-TCP listener on port **35002** (`WICAN_DATALOG_STREAM_PORT`), always-on, single client | WiCAN=server / host=client is fixed (WICAN_TRANSPORT.md:65). 35001 is load-bearing for the dead-man reaper and must not change semantics (WICAN_DEADMAN_AUTORESUME.md:134-142). HTTP chunked/WS would ride the httpd whose restart brackets flashing. 35002 is unclaimed. |
| Wire format | Line-oriented text: `#`-prefixed control lines + raw CSV lines byte-identical to the SD file | The SD file is already the validated artifact; `#` lines are CSV-comment-friendly and the receiver strips them. |
| Stream source | Hook (callback) registered into `csv_logger` by `main/`, fired from the writer task at header/row/close points | `csv_logger` is a component and cannot depend on `main/`; same one-way pattern as `csv_logger_set_column_provider` (csv_logger.h:87-95). |
| Backpressure | Writer→streamer FreeRTOS StreamBuffer (INTERNAL RAM, 16 KB), 0-timeout send, **drop-newest whole rows** + `#drop N` marker + counter. Session-boundary lines (`#close`, `#session`+header) are never dropped permanently: they are announced via a retried all-or-nothing sync block that always precedes further rows, with a seq-counter handshake so a boundary racing the connect preamble has exactly one announcer. | The CSV writer must never block (it runs fprintf/fsync flash-cache-disable windows; csv_logger.c:125-129). Bounded-send rule per the historic mid-stream-close CAN wedge (WICAN_PART_C_FINDINGS.md:14-21). A droppable header would make the receiver adopt a data row as column names. |
| Capability detection | Banner probe: connect 35002, expect `#hello NCDLv1 ...`; refused/timeout → unsupported. Plus `stream_*` fields in `GET /check_status`. | No NCFRv bump: the fast-read/write wire contract is untouchable (REFACTOR_PLAN.md §1) and its rev only moves when *that* wire behaviour changes. Banner is self-describing, soft-degrades (INV-9 style). |
| Config | None. Always-on listener, zero new config keys, no web-UI change | Matches 35001 (hardcoded, always-on). Keeps the firmware diff out of homepage/main.js entirely. |
| Host presence | A 35002 connection does **NOT** count as host-present and never touches park/claim/leases | Wiring it into the reaper would change validated deadman semantics (WICAN_DEADMAN_AUTORESUME.md:134-142). Stream client must not add an un-park path (INV-5). |

## Wire protocol `NCDLv1` (device → host; host sends nothing)

All lines `\n`-terminated. Control lines start with `#`; unknown `#` lines must
be ignored by the receiver. Everything else is a raw CSV line.

- `#hello NCDLv1 fw=<git_version>` — once, immediately on accept.
- `#idle` — on accept when no CSV session is active (rows will follow whenever one opens).
- `#session file=<basename> cols=<n>` — a session is (already) active: sent on
  accept for mid-session joiners, and when a session opens/rotates.
  Followed immediately by the CSV **header line**.
- `#nohdr` — the header line is unavailable (mid-session joiner with an
  incomplete copy, or a header line >4 KB). Rows still flow; the next
  `#session` brings a full header. The header always travels as ONE whole
  line (sent at header-complete time, never chunk-by-chunk) so it can never
  be torn by backpressure.
- `#drop <n>` — n rows were dropped (ring full) since the last successfully
  streamed row. Receiver should note a gap, not error.
- `#close` — the SD session closed (trip end / manual stop / pause / SD pull).
  Socket stays open; `#session` re-announces when the next one opens.

Semantics: live tail only — no backfill of rows written before the client
connected. Rotation looks like `#close` + `#session` + header (same columns).
`POST /datalog?op=pause` closes the session (manual_stop) → `#close`; silence
while parked is normal, not an error.

## Firmware architecture (`../nc-flash-wican-fw`)

New: `main/datalog_stream.{c,h}` — clone of the `slcan_port.c` 3-task shape
(srv accept loop with 1 s bind-retry-forever, rx task solely for disconnect
detection, tx drain task; PSRAM stacks 4096 @ prio 5; SO_KEEPALIVE 5/5/3;
single client, backlog 1, event-group OPEN/CLOSED handshake). The accepted
socket sets `TCP_NODELAY` (deployed + bench-verified 2026-07-11): rows are
~100 B at ~10 Hz, and Nagle would let a delayed ACK clump them; measured
inter-arrival stayed a clean ~102 ms mean both before and after on the bench
(baseline p95 134 ms → 111 ms), so this is insurance for higher grid rates /
other host stacks, not a measured 10 Hz win.

Data path: `csv_logger` writer task → hook → `datalog_stream_hook(ev, data, len)`:
- `EV_SESSION_OPEN` (data = file path) → reset header accumulator, enqueue `#session` line
- `EV_HDR_CHUNK` → append to 4 KB internal-RAM header copy (for joiners) AND enqueue
- `EV_HDR_END` → mark header copy complete, enqueue `\n`
- `EV_ROW` (data = the emitted row incl. `\n`) → enqueue
- `EV_CLOSE` → invalidate header copy, enqueue `#close`

Enqueue = `xStreamBufferSend(..., 0)` after a whole-line space check — a line
either enters whole or is dropped+counted (never a partial line). When no
client is connected (OPEN bit clear) the hook returns immediately (drop-free
fast path; ring is reset on each accept). All hook calls happen on the writer
task (single StreamBuffer writer); the tx task is the single reader. Header
copy is guarded by a mutex held only for memcpy (µs-bounded, writer-safe).

`csv_logger` changes are minimal and producer-hot-path-free:
- `csv_logger_set_stream_hook(fn)` setter + typedef in `csv_logger.h` (mirror
  of the column-provider registration).
- Hook call sites: top of `csv_open_new_file()` (SESSION_OPEN after the path is
  built), next to each header `fprintf` (HDR_CHUNK/HDR_END), after the row
  `fprintf` in `csv_emit_wide_row()` (EV_ROW with `buf,len`), in
  `csv_close_file()` (EV_CLOSE). NULL hook → zero-cost no-ops.

`main/main.c`: `#define WICAN_DATALOG_STREAM_PORT 35002` next to 35001, same
collision guard against the configured stock port; init after
`slcan_port_init()`; register the hook before `csv_logger_init_deferred()`.

`main/config_server.c` `/check_status` additions (precedent `led_indicator`):
`stream_port` (35002), `stream_connected` (bool), `stream_rows_sent`,
`stream_rows_dropped`.

Hard constraints honored: no BIT1 access, no park/claim/lease access, no
`portMAX_DELAY` on any path that can hold the writer, no PSRAM deref from the
writer task, no new config key, no change to 35000/35001 behaviour, no new
un-park path, wide-row producer hot path untouched.

## Host architecture (nc-rom-editor)

Split enforced by `tests/test_architecture.py` (ui never imported from ecu):

- `src/ecu/wican_stream.py` (headless, stdlib-only, precedent `wican_logs.py`):
  `WiCANLiveStreamClient(host, port=35002)` — blocking socket + line reader;
  `connect()` validates the `#hello NCDLv1` banner (short timeout; refused /
  no banner → `WiCANStreamUnsupported`); `run(on_event)` loop delivering typed
  events (hello/idle/session/header/row/drop/close); `stop()` is thread-safe
  (socket shutdown + Event). Constants in `src/ecu/constants.py`
  (`WICAN_DATALOG_STREAM_PORT = 35002`, banner prefix). Must NOT touch
  `WiCANTransport`/35001/`/datalog` ops — the trip lease choreography lives in
  `WiCANDatalogClient` (below), never in this pipeline module.
- `src/ui/wican_live_datalog.py` (Qt owner, precedent `WiCANLogSync`):
  `WiCANLiveDatalog(QObject)` MainWindow collaborator; worker QObject on a real
  QThread, bound-method `Qt.QueuedConnection` signals; writes one local file
  per `#session` into `{logs_directory}/live/live_<ts>.csv` (control lines
  stripped, rows appended + flushed so MLV can tail); signals for state/rows;
  `shutdown()` from MainWindow close. The FIRST capture of each run offers
  "Trail in MegaLogViewerHD?"; accepting launches MLV via
  `src/ui/mlv_trail.py` (THE single MLV-launch pipeline) — MLV's documented
  properties-file hook (`fileName=` forward-slashed, `trailFile=true`,
  `startPlayback=true`, keys verified in the installed build's `ax/bL`
  class), `QProcess.startDetached`. Rotated sessions never re-prompt. No MLV
  install → no dialog, ever.
- MLV trail facts (decompiled MegaLogViewerHD 2026-07-11, all hardcoded): the
  Data Loader Thread ingests new rows on a 50 ms poll; trail engages only
  once the log has ≥50 samples (100 ms poll, 60 s timeout dialog) and then
  starts 1.0× playback 10 samples before the end — so the visible lag at a
  10 Hz grid is ~1 s of deliberate playback offset, not file latency. Wire →
  local CSV measured at ~102 ms mean inter-arrival (= the grid period).
- ECU window: a "Live Datalog" start/stop affordance, `is_wican_adapter()`
  gated (hidden for J2534); logger prefix added to the activity-log allowlist.
  Utilities and ECU operations never mix (`_update_action_states`): while the
  live stream or a trip-log sync runs, every ECU action locks (explanatory
  tooltip) and the two utilities lock each other — only the live toggle stays
  enabled while streaming so it can be stopped. `_start_flash` still stops
  both first as a backstop for non-button paths. The Activity Log narrates
  captures end-to-end: start names the destination folder (`{logs}/live`),
  each `#session` logs the full local path, each closed capture logs
  `capture saved: <path> (N rows)`, and a run with no session ends with an
  explicit "nothing was captured".
- Tests: real-TCP `MockStreamServer` (precedent `MockSlcanServer`) covering
  banner gate, mid-session join with header, `#drop`/`#close` handling, stop()
  mid-read; UI worker test on a real QThread (precedent
  `test_wican_log_sync.py`). CHANGELOG under `[Unreleased]`.

## Live-trip lifecycle (2026-07-11; user-defined semantics)

Pressing **Live Datalog** starts a NEW trip on the device; **Stop** stops the
device's logging too; **disconnect / app close** restores autonomous
(follow-ignition) trip logging; a **host crash** anywhere self-heals via
firmware reapers. Built on the shared per-host `WiCANDatalogClient`
(`get_datalog_client()` — the firmware issues a fresh lease token per arm, so
independent client instances clobber each other; the ECU session, the flash
fence and the live trip all share ONE client/refcount):

- **Firmware** (`csv_logger.c`): `/csv_logger?op=start` grew `rotate=1` (close
  the active session so the next record opens a fresh file — why=`rotate` in
  the close event) and `lease_ms=N` (arms the live-trip dead-man; an UNLEASED
  start — the web UI button — disarms it). New `op=auto` restores
  follow-ignition EVERYWHERE: current mode, the `/datalog` pre-pause restore
  snapshot, and the lease. New `op=renew&lease_ms=N` (2026-07-11 eve) is the
  heartbeat: renews the lease ONLY while manual-ON, answers **409** otherwise —
  it can never (re)start logging, so a web-UI Stop between two heartbeats WINS
  (the old leased-re-start heartbeat silently restarted every operator Stop
  within one 4 s tick — field incident). The writer loop reaps an expired lease
  back to AUTO (`EVL_REAPER_RESUME` "live-trip lease expired"). `lease_armed`
  exposed in `/csv_status` + `/datalog` JSON; `producers_parked`
  (`can_should_park()`) in `/csv_status`, `parked` in `/poll_status` — the
  honest "armed but fenced" signal the web UI renders (see the Start Trip
  section below).
- **Client** (`wican_config.py`): `begin_live_trip(on_external_stop=cb)` =
  suspend any physical park/claim (refcount untouched; `_suspended`) →
  `op=start&rotate=1&lease_ms`; the keepalive daemon heartbeats
  `op=renew&lease_ms` each tick while `_live_trip` (on 409: one-shot
  `on_external_stop` fires on the keepalive thread + `_trip_external_stop`
  latches; on 400: pre-renew firmware → degrade to the old leased re-start,
  remembered in `_csv_renew_legacy`). `end_live_trip()` = re-arm the park FIRST
  for any ref holders (no un-parked AUTO instant → no stub trip file) →
  `op=auto` (fixes mode + snapshot) — **skipped entirely after an external
  stop** (the operator's OFF stands; flipping it to AUTO would be the same
  who-owns-the-mode fight from the other side). `hold_silent()`/
  `release_trip_hold()` = the one-shot "Stop leaves the device quiet" ref. Last
  `release_bus()` while suspended releases nothing physical (the trip keeps its
  own dead-man) — that is the disconnect-mid-stream path. Known corner (accepted):
  a web Stop+re-Start inside one 4 s tick is invisible to the host (mode is ON
  again at the next renew), so the stream keeps tailing the operator's new trip
  with a lease armed on it.
- **Owner** (`wican_live_datalog.py`): worker `run()` begins the trip AFTER the
  NCDLv1 banner (unsupported fw never starts one); teardown takes the silent
  hold ONLY on a user stop (a stream error restores AUTO instead; an EXTERNAL
  stop — web-UI Stop Trip — takes no hold AND leaves the mode alone: the worker
  latches `_external_stop` before `request_stop()`, so `user_stop` is false in
  `_teardown_trip`). `dispose()` (app close, `main.py` closeEvent) also
  releases the hold. ECU window: Connect locks while a utility runs (connecting
  would re-park the bus and starve the stream). The MLV trail offer is
  **non-modal** and parented to the ACTIVE window (an app-modal box parented to
  the main window froze every window when the user worked in the ECU window —
  field incident 2026-07-11: the dialog opened UNDER the active window, all
  input blocked, nothing visible to dismiss); accepting trails the NEWEST open
  capture (the offered first capture can rotate away at 0 rows within
  milliseconds), and a run end dismisses an unanswered offer without logging a
  bogus "declined".
- **Reaper coverage**: streaming crash → csv lease (12 s TTL) → AUTO; parked
  crash → park/claim leases (12 s / 75 s + 3 s grace) → AUTO. The park path
  only became reachable on a LIVE bus with the 2026-07-11 TX-only idle fix
  (see WICAN_DEADMAN_AUTORESUME.md `BUS_IDLE_QUIESCE`).
- **Hardware E2E (2026-07-11, all passed)**: window-park silences the device →
  trip opens a NEW file + 15 rows streamed → stop re-parks with mode AUTO →
  keepalive holds past the TTL → release restores autonomous logging → crash
  mid-trip reaped at ~12 s → crash while parked reaped at ~80 s. Evening pass
  (post-renew build): renew answers 200 while ON / 409 after a web Stop with
  the mode left OFF; production-client E2E (`begin_live_trip` → real keepalive
  renewals → curl `op=stop` → callback fired within a tick → `end_live_trip`
  left the mode OFF).

## Start Trip web-UI trap — fixed (2026-07-11 eve)

Root cause of "Start Trip logs nothing": the web button only flips
`csv_manual_mode`; a trip FILE opens when the first record arrives, and BOTH
producers (poll_log / AutoPID) park on `can_should_park()` while any host fence
(NC Flash session reservation / flash) is up — so an armed trip records nothing
and `/poll_status` shows a healthy-looking STALE pre-park snapshot. Reboot
doesn't help (the app re-fences on reconnect). Fixes (fw fork, deployed):

- `/csv_status` + every `/csv_logger` op reply carry `producers_parked`
  (`can_should_park()`); `/poll_status` carries `parked`.
- Web UI (`main/web/src/main.js`) renders the truth: status line "armed —
  paused: bus reserved by NC Flash (recording resumes when it disconnects)",
  Field Console "Armed (paused)" + "paused — bus reserved by NC Flash", and the
  Start toasts say "NC Flash holds the bus — no data until it disconnects".
  Start Trip stays ENABLED while fenced (arming is valid — recording begins the
  moment the fence lifts); the message replaces the old false
  "waiting for data…" chase.
- Second (rarer) trigger — poller quiesced in engine-off LISTEN_ONLY on a
  silent bus (only a bus frame resumed it; a sleeping ECU never sends one):
  `op=start`/`op=auto` now arm a **wake-kick** (`can_datalog_kick()` in
  `main/can.c` — lives there because csv_logger may not depend on fast_log).
  The poll task consumes it in the quiesce loop as an extra resume trigger
  (kept pending through the FLIP_MIN_MS anti-flap window; cleared while polling
  normally so a stale kick can't fire a later spurious resume). Wrong kick
  cost: one ~2 s NORMAL probe, then re-quiesce. NOT hardware-exercised (the
  bench ECU was awake, so the poller never quiesces); compile- and
  logic-verified only.

## Out of scope (v1)

Live value table/plots in NC Flash (file tail via MLV covers viewing), web-UI
stream status, multi-client fan-out, backfill/replay, USB-CDC transport,
counting 35002 as host-presence.

## Bench test plan (device 192.168.1.169 — dry-run rules apply)

1. OTA the branch build; `/check_status` shows `stream_port:35002`.
2. `nc`-level probe: connect 35002 → `#hello NCDLv1 fw=...` then `#idle` or `#session`+header.
3. With the device's auto CSV session: rows arrive live; row text byte-equal to `/download_csv` content for the same span.
4. Mid-session join: reconnect → `#session` + header replay.
5. `POST /csv_logger?op=stop` → `#close`; `op=start` → `#session` + header.
6. NC Flash `WiCANLiveDatalog` end-to-end: local live_*.csv grows; stop/start; flash-arbitration (stream stops when an ECU op starts).
