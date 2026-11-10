# WiCAN Logger window: edit the WiCAN's own list from NC Flash

**Goal document.** Paste the condition line below into `/goal` once the user has
approved the layout (mockups: see "Layout").

```
/goal Build the WiCAN Logger window described in docs/internal/WICAN_LOGGER_WINDOW.md until every acceptance criterion A1–A14 is shown true in this session's output, without breaking any constraint in that document.
```

## Why

Today one Datalogger window serves every adapter. For the Tactrix and OBDX,
NC Flash polls the ECU itself. For the WiCAN, NC Flash is a remote control for a
logger that also runs alone. The shared window hides that difference with
switched text and hidden buttons, and it shows NC Flash's channel model
(`x*0.25`), not the WiCAN's (`((B3*256)+B4)/4`, broadcast frames, computed
channels). "Save as WiCAN's Own List" then has to translate between the two.

The new window edits **the list stored on the WiCAN, in the WiCAN's own format**.
What you see is what the WiCAN logs.

## What the user sees

- **Opening the window** (WiCAN adapter selected) reads `/load_auto_pid` and shows
  three tabs: **Polled**, **Broadcast**, **Computed**. Each row has on/off and the
  WiCAN's own fields. Rows can be added, edited, duplicated, removed, and moved by drag and drop or Move Up/Down (the Tactrix/OBDX window's `_ChannelTable` does the same).
- **Save to WiCAN…** writes the whole document back. The previous file is backed
  up on this PC first. The first time, a popup explains what Save changes (the
  WiCAN's own logging, on every drive) with a "Don't explain this again"
  checkbox. The list of changes is always shown.
- **Start Logging** begins a temporary session (`/datalog_session`, firmware
  v1.27.0) with **exactly the document on screen**: polled, broadcast and
  computed channels. Unsaved edits therefore apply to that run only. Stop, a
  closed NC Flash or a dead PC all hand the WiCAN back its saved list (proven on
  the bench 2026-11-10).
- A **status bar** shows the WiCAN's address, firmware, and what it is doing:
  idle on its own list, or logging NC Flash's run (trip file, rows/s, stream).
- During a run, a **Live** column shows each channel's latest value from the
  stream.

The Tactrix/OBDX Datalogger window keeps the NC Flash channel model and loses
every WiCAN-specific part.

## Layout

Mockups (static, filled with the bench WiCAN's real list): idle, and while
logging. Rendered by the scratch script `mockup.py` in the session that wrote
this document; regenerate with the same structure if needed.

```
+--------------------------------------------------------------------------------+
| WiCAN <wican-ip> · fw v1.27.0 | ● Idle  the WiCAN logs this list ... [Reload]|
| This is the list stored on the WiCAN. Edit it here, then Save to WiCAN. ...    |
| [Polled (46)] [Broadcast (6)] [Computed (5)]                                   |
| On | Name | Request | Formula | Unit | Rate | Live                             |
| ...                                                                            |
| [Add from NC Miata list...] [Add custom...] [Edit...] [Duplicate] [Remove]     |
| [Move Up] [Move Down]                           41 polled on · about 31 rows/s |
|--------------------------------------------------------------------------------|
| [Start Logging] [Open in MegaLogViewerHD] [Open Live Logs Folder]              |
|                        ● 2 unsaved changes [Discard Changes] [Save to WiCAN...]|
| Last save: today 13:41, previous list backed up · Put back the previous list   |
+--------------------------------------------------------------------------------+
```

## Design, grounded in the code

### Routing
- `MainWindow.open_datalogger_window` (called from `src/ui/trip_logs_window.py`
  and the menu) opens `WiCANLoggerWindow` when `settings.get_ecu_adapter() == "wican"`,
  else the existing `DataloggerWindow` (`src/ui/datalogger_window.py`).
  Switching adapter while a window is open closes it; a run keeps going (the
  `Datalogger` owner is on the main window, as today).

### Model: `src/core/wican_logger_doc.py` (new, pure, no Qt)
- `WiCANDoc.from_json(dict)` / `.to_json()` keep **every key they don't edit**
  (`initialisation`, `ecu_protocol`, voltages, `standard_pids`, `std_pids`,
  unknown future keys, unknown per-row keys such as `comment`) and the order of
  the rows. Round trip of the bench file is byte-identical after
  `json.dumps(..., sort_keys=False)` normalisation.
- Rows: `PolledRow` (`Name, Mode, PID, Expression, Unit, Class, MinValue,
  MaxValue, Period, SampleEvery, enabled, description`), `BroadcastRow`
  (`frame_id` + parameter fields; the file groups parameters by frame, the UI
  shows one row per parameter and regroups on save), `ComputedRow`
  (`name, expression, unit, enabled`).
- `session_body()` = `to_json()` without `std_pids` (firmware refuses that key,
  `datalog_session.md`).
- Checks, run on edit and before Save/Start:
  - Polled `PID` must start with service 01, 22 or 23. Same test as the
    firmware's `dls_pid_service_ok()`. Save enforces it too, although
    `/store_auto_data` does not.
  - Polled/broadcast byte formulas: port the firmware's expression grammar
    (B-bytes, `[Bn:Bm]` ranges, `& | >> <<`, + - * /, parentheses). Find the
    parser in `components/` before porting; do not guess the grammar.
  - Computed formulas: port `parseCalcExpression()` from the firmware's
    `main/web/src/main.js` (~line 590), and warn on names no channel provides.
  - Unique names across all three tabs. At most 128 polled rows, body under
    64 KB (the session limits).

### Device I/O: reuse `src/ecu/wican_autopid.py`
- `WiCANAutoPid.load()`, `.store()`, `.restore()` and `save_backup()` as they
  are. (Line numbers are left out on purpose: this file changes often. Find
  them by name.)
- **Whose file it is.** `is_own_firmware_file()` decides, with the firmware's
  own rule (`main/auto_pid_seed_logic.h`): `format == "nc-flash-1"`, or no
  `format` and a `calculated` list (a file the Logger page saved before the
  marker existed, like the bench WiCAN's). `replace_pids()` adds the marker on
  write. An official MeatPi file (neither key) or another `format` is refused
  with a plain message; the window shows it read-only with that message.
- **Same name twice is allowed.** The firmware keeps same-name pairs on
  purpose (`poll_log.c`) and writes `ECT [PID]` / `ECT [CANFLT]` when both are
  on (`csv_logger.c`). Its own list turns one of each pair off. Today's push
  (`clashing_rows()`) turns the polled copy off when the broadcast copy is on,
  never RPM. The new window should show pairs side by side and warn, not refuse.
- **RPM.** The firmware's engine detection needs a turned-on polled or
  broadcast channel named RPM (any case; calculated ones don't count,
  `poll_log.c`). Without it, it goes by battery voltage and a trip can open and
  never end (seen on the bench, 2026-11-10). `has_engine_rpm()`; Save refuses a
  list without one.
- **Someone else changed it.** Save re-reads the file first. If it differs from
  what the window loaded (for example, edited in the web page meanwhile), it
  shows both and asks: keep mine, or reload theirs.
- Sessions: `WiCANSessionList` with `session_body()` instead of
  `session_document(rows)`. The firmware accepts broadcast and computed
  channels in a session (`main/datalog_session.c:220`).

### Live values
- The stream on port 35002 is already parsed for the live table cursor
  (`src/ui/wican_live_datalog.py`, `src/ui/live_values.py` `LiveValues`). The Live column
  reads `LiveValues.current()` by channel name, refreshed at 4 Hz.

### "Add from NC Miata list…"
- Reuses `CatalogDialog` (`src/ui/datalogger_window.py`, the Tactrix/OBDX
  window's picker) over `catalog_channels()`: 77 NC MX-5 channels in
  `examples/datalog_catalog_ncmx5.json`, generated by `tools/import_opconfig.py`.
  Each pick converts with `to_firmware_row()`; `tests/test_datalog_catalog.py`
  proves every entry converts. Names already on any tab are greyed out.

## Acceptance criteria (each shown in this session's output)

- **A1** Opening the window with the WiCAN adapter reads the device list: a test
  with a fake client shows the three tabs filled with 46 / 6 / 5 rows from the
  bench file (`autopid_before.json` committed as a test fixture, network keys
  absent from it — it has none).
- **A2** Round trip: `WiCANDoc.from_json(bench).to_json() == bench` (pytest).
- **A3** Unknown top-level keys, unknown row keys and row order survive an edit
  of one row (pytest).
- **A4** Drag and drop, Move Up/Down, Duplicate, Remove, Add work on each tab and mark the
  window dirty; Discard Changes restores the loaded document (pytest-qt).
- **A5** Each check refuses its bad input with a plain message (pytest,
  parametrised): PID `1003`, `2E1746FF`, `3101FF00`; bad byte formula; computed
  formula naming a missing channel (warning, not refusal); a duplicate name
  within one tab; a list with no turned-on RPM (polled or broadcast); 129
  polled rows. A same name on two tabs is a warning, not a refusal.
- **A6** Save writes a backup file, then `/store_auto_data` with the edited
  document; Put back the previous list restores it (pytest with fake client).
- **A7** Save re-reads first and stops on a changed file (pytest).
- **A8** A file with `calculated` but no `format` loads and saves (the Save adds
  `"format":"nc-flash-1"`); an official MeatPi file or a foreign `format` opens
  read-only with the plain refusal message (pytest).
- **A9** Start Logging sends `session_body()` of the on-screen document (unsaved
  edits included, `std_pids` removed) to `op=begin` (pytest with fake client).
- **A10** The first Save shows the explanation and checkbox; once ticked, only
  the change list shows (pytest-qt).
- **A11** The Tactrix/OBDX window has no WiCAN text, box or push code path left:
  `grep -n "wican\|WiCAN" src/ui/datalogger_window.py` shows none.
- **A12** Routing: adapter `wican` opens `WiCANLoggerWindow`, `j2534`/`obdx` open
  `DataloggerWindow` (pytest-qt).
- **A13** Full suite: `venv-windows\Scripts\python.exe -m pytest -q tests` exits 0.
- **A14** Bench, on the shared WiCAN with the lock taken (global CLAUDE.md): open
  the window against the real device and print the three row counts. Start
  Logging with one unsaved edit, then show `/poll_status` `session_token` equal to
  the token from begin, and the trip header holding that edit. Stop, then show
  `/load_auto_pid` sha256 unchanged from before the run. No Save to the device
  during `/goal`; that is a manual check.

## Constraints

- Never write the WiCAN's list without a backup on this PC first.
- Never send `std_pids` in a session. Never send a polled row outside services
  01/22/23, in a session or a Save.
- Don't change the firmware. Everything needed exists in v1.27.0.
- Don't change the Tactrix/OBDX polling path, its CSV format or its channel
  model, apart from removing the WiCAN parts and keeping the new Move Up/Down.
- Keep the tested session code (`WiCANSessionList`, the dead-man keepalive). Only
  the document it sends changes.
- The web page's Logger and NC Flash both edit the same file: never overwrite
  without the re-read check (A7).

## Manual checklist (outside `/goal`)

- [ ] Look at the real window on the bench and OK the layout and wording.
- [ ] Save to WiCAN once for real, then open the WiCAN web Logger page: the
      same rows, in the same order, broadcast and computed channels intact.
- [ ] Put back the previous list, then check the web page again.
- [ ] A drive with the engine running: Start Logging while the WiCAN has an AUTO
      trip open (the trip rolls, `channel_list` in the event log).

## Side findings from the bench file (not part of this work)

- `LOAD` is labelled `g/s` on the WiCAN; NC Flash's catalog says `g/rev`.
- `EQ_RATIO_DSD` has no `Period` (it falls back to every sweep).
- 24 of the 46 polled rows are mode 23 memory reads. They need the patched ROM;
  the stock bench PCM refuses them.
