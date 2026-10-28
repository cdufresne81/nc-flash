# Session Notes

Only open work and live decisions go here. Completed work goes in `CHANGELOG.md`; delete an item
here once it's done. Tracked work lives in GitHub issues (`gh issue list`) — don't duplicate it.
History before Sep 24, 2026: `git show f4709f1:.claude/notes.md` (search it, don't read it end to end).

## Dev workflow (live decision, Oct 15 2026)

From v2.23.0 on, all work goes to `dev` first unless the user says otherwise, ships as a
`vX.Y.Z-beta.N`, and is folded into `master` once tested (rule in CLAUDE.md, flow in
`docs/internal/RELEASE_CHANNELS.md`). The user runs the Experimental update channel.

What is on `dev` awaiting `master`: run `python tools/dev_status.py` (asks GitHub; no list to keep here).

## Next up (in order; delete the top line when it's done)

1. Optional: tell speeps the definitions are being fixed and extended (speeps' email only OKs
   including them).
2. #124 tool to port a new table across all NC calibrations.
3. #122 give duplicate-named tables real names.
4. #123 check the TCM odd-address cells / overlaps against the TCM code.
5. #149 Windows CI reports exit 1 after every test has passed.

## Open items (not tracked in an issue)

- **Live table cursor: branch `feature/live-table-cursor` (off `feature/datalogger`), uncommitted
  in the datalogger worktree.** Open tables show where the Datalogger's live values fall: a compact
  cross + dot on the exact point, nearest cell outlined, values tagged on the axis headers
  (`src/ui/table_viewer_helpers/live_overlay.py`), plus a graph marker. Axis -> channel map in
  `examples/axis_channels.json` (exact axis names; TCM definitions use only their own `logparam`).
  Bench (Tactrix, engine off) + 3 Opus reviews (1st DO-NOT-SHIP, fixed; 2nd and 3rd SHIP).
  - Perf (5 tables with GPU graphs fed at 10 Hz): UI lag p95 28 ms. Needed: compact cross (each
    repainted cell costs ~0.5 ms in the styled delegate), background windows throttled to 0.4 s,
    GPU 3D skips the label fit when nothing moved. Don't tune the matplotlib fallback (user: unused).
  - Axis-name suffixes (`RPM_6DD8`) are NOT RAM addresses: lf9veb Load Scaling reads RPM from
    float 0xFFFF6D84 and loads 0xFFFF6E80/6F24/6F40 (3 lookups). LOAD mappings stay `approx`
    until the in-car comparison card answers whether PID 0x43 matches.
  - Next: commit + PR (into `dev` after #211 merges, else draft on `feature/datalogger`).
  - LOW leftovers: no message when a NaN/zig-zag axis refuses the cursor; duplicate-breakpoint
    cell (first vs last) unconfirmed vs the ECU; TCM detection keys on "TCM" in romid.model; axis
    tags cover half of the two breakpoint labels they sit between.
- **Datalogger: PR #211 (`feature/datalogger` -> `dev`), open.** Worktree
  `C:\Users\dufre\Projets\nc-rom-editor-datalogger` (has the built-in OpenPort driver in
  `dist/openport/`; run `..\nc-rom-editor\venv-windows\Scripts\python.exe main.py` there).
  Plan: `~/.claude/plans/i-m-ok-with-the-snug-clover.md`. Merges `feature/live-datalog-v2`,
  `feature/obdx-pro-adapter`, `feature/openport-winusb-driver`, `fix/openport-sd-card`, so it
  reaches master only after those four. Refs #105 (standalone recording still open).
  - Proven: Tactrix bench (engine off) 38.5 rows/s packed / 29.9 unpacked, 0 empty cells;
    the user's own bench test passed (Oct 28). Two Opus reviews: 1st DO-NOT-SHIP (fixed),
    2nd SHIP.
  - Next: merge #211 + cut a beta when the user says so; then the board cards (WiCAN push
    bench test once the WiCAN is online: snapshot `/load_auto_pid` + `/csv_status` first,
    restore after; in-car Tactrix drive with the engine running; OBDX untested).
  - No "Apply Now" for a list sent mid-trip: released WiCAN firmware has no `op=auto`, and
    `op=start` leaves its logger forced ON. Needs `op=auto` in the firmware to bring it back.
  - Known LOW gaps (2nd review): a late NRC for an earlier same-service request can drop the
    current channel; a late positive reply can turn packing off; the device file isn't
    compared between preview and push; a Start landing inside the ECU window's connect event
    pump can pass the blocker (one opener then fails).
  - No "Apply Now" for a list sent mid-trip: released firmware has no `op=auto`, and
    `op=start` would leave the logger forced ON for good. Adding `op=auto` to the firmware
    would allow it (then restore the exact prior `manual_mode`).

- **WiCAN live datalog (fw #3, PR from `feature/live-datalog-v2`) follow-ups** (design:
  `docs/internal/WICAN_LIVE_STREAM.md`):
  - User's in-car test pending; MegaLogViewerHD trail launch not exercised in a bench run.
  - Overnight soak with a client connected not done (the July build's crash-loop was never pinned).
  - Internal-RAM headroom: largest block 23,552 B while recording + streaming vs the 22,528 B CSV
    queue; only matters for a manual trip start with csv_log off at boot. No release baseline.
  - `#bye replaced` is sent MSG_DONTWAIT, so a full send buffer can truncate it → the two-client
    swap can recur. Any stray 35002 connection stops NC Flash's stream (by design).
  - mDNS re-resolve can't be cancelled (`resolve_host_with_fallback` doesn't pass `cancel_event`;
    the fix touches `src/ecu`); app close waits up to 10 s for it.

- **OBDX Pro adapter** (PR from `feature/obdx-pro-adapter` into `dev`): follow-ups tracked in
  #167 (hardware tests before Stable), #168 (wireless links), #169 (late read reply). The same
  PR changes the generic flow for every adapter: read/scan accept an all-zero seed without a
  key, and "still programming" refusals offer an ECU reset. Proven over OBDX only; check them
  on the Tactrix too (item below). The reset dialog itself was only unit-tested.

- **Bench-test #130/#131 on the Tactrix clone (J2534)** (owner: user). Also check the OBDX PR's
  generic changes there: Read RAM then Read ROM straight away works, and a flash refused with
  "still in programming mode" offers a reset that works. Only tested over
  WiCAN so far. Check: ignition OFF → Connect fails in ~3 s, not 60 s; right after a ROM
  read or RAM scan, Flash waits ~6.5 s then reads RPM and voltage (no false refusal);
  pulling the cable mid-session → "ECU not answering" and buttons fail fast. Delete this
  item once done (or open an issue if anything misbehaves).
- **ROM definitions (after PR #120, speeps import):**
  - Decided: nc-flash-re is the working copy where definitions are edited; NC Flash holds the
    released copy (copied in, never hand-edited). Check any change with
    `python tools/metadata_lint.py check`.
  - Optional: the KR table is proven 1 byte from ROM code at 0xBB8D9/0xBBCA9/0xBBBD1 only; the
    other 5 addresses (30 files) rest on the odd-address argument + identical neighbours.
  - RomDetector stops at `</romid>`: a file broken after it is listed, and opening a matching
    ROM fails with a parse error instead of "no definition". Not a write hazard.

Carried over from the old log on Sep 24, 2026. Verify an item is still open before acting on it.

- **MCP Streamable HTTP follow-ups (merged in PR #107):**
  - Next release: drop the `--transport sse` CLI option (kept for one release).
  - Deferred: MCP SDK v2 / spec 2026-07-28 migration. Needs an adversarial review, because
    `write_table` edits flashable ROM data and v2 runs sync tools on worker threads.
- **WiCAN `close()` trailing `C`**: it disables the shared bus after the reservation is released,
  so the logger loses ~10 s after every disconnect/read/flash. Fix as its own change with its own
  bench pass. KEEP the `C` at the start of `open()` and `_prime_channel` (bench-proven).
- **Graph engine follow-ups (PR #103)**: Settings UI + crash sentinel for the classic engine
  (`display/graph_engine` is env/registry only); draw-time GPU error → classic fallback
  (rendercanvas swallows draw errors); treat CPU adapters (WARP/llvmpipe) as no GPU. Untested:
  packaged build on a clean / no-GPU machine, Linux package.
- **Refactors deferred from #99** (agreed real, not urgent): shared `_AsyncJob` thread-lifecycle
  collaborator (4 copies of the QThread pattern); move the WiCAN probe-outcome classifier into
  `src/ecu` (`_grade_wican_test` and `_open_coexist_transport` word the same outcomes differently);
  `tools/_wican_link.py` entry helper so bench tools can't forget the bus reservation.
- **Trip-log names with a space, `+`, `%` or non-ASCII never download** (pre-existing, found in
  the #112 review): the download query-encodes the name (space → `+`) and the firmware's
  `/download_csv` never decodes it → 404, and the run stops at that file. Only hand-renamed trips
  hit it (firmware names are `[A-Za-z0-9._-]`). The #112 delete pass refuses such names.
- **#112 follow-ups (not fixed, low):** the delete pass's three start-up GETs can't be aborted
  (up to 30 s, longer than `shutdown()`'s 15 s wait); the backlog re-verify shows no bytes/ETA;
  rescued trips aren't offered in the MegaLogViewerHD prompt.
- **B2 spurious dirty flag** after undo back to the saved state: safe-side (extra save prompt, no
  data loss). A real fix needs per-document clean-state tracking across per-table undo stacks.
- **Unconfirmed, from Jul 6**: retest-on-binary for B2/B5/B15 may already be done; decision D4
  (retire Option-A `WiCANFlasher`?) was still open.
- **🔴 ECU window "Close anyway? → Yes" aborts a J2534 flash mid-write** (pre-existing, found in
  the #104 updater review, confirmed by reading the code, not on hardware): `ecu_window.closeEvent` calls
  `_current_manager.abort()` for ANY op (comment claims read-only only), and `flash_rom`'s ROM program
  `transfer_data` honours `abort_check` → partly programmed ECU. The window may also be destroyed with
  the flash QThread still running (`wait(3000)`). Needs its own change + WICAN_MANUAL_TEST-style bench pass.
- **In-app updater (#104 item 1, shipped in v2.18.0) follow-ups:**
  - Decision pending: `update_check._stream_to_file` duplicates `wican_http.download_to_file`'s read loop
    ("one pipeline copy"). Merging touches `src/ecu` → bench test. `wican_http` likely has the same
    blocking `read()` vs shutdown-wait problem the updater fixed with `read1` (untested).
  - Low, unfixed: small TOCTOU between the last hash check and `os.startfile` in user-writable %TEMP%;
    backport tags (e.g. v2.16.1 after v2.17.0) become GitHub "latest" (`make_latest` default);
    installer.iss has no `[InstallDelete]`, so stale `_internal` files accumulate across upgrades.
