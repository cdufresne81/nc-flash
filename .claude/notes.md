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

- **Bench-test the NC1/NC2 generation check on the Tactrix clone (J2534)** (owner: user),
  before the next release. Proven over WiCAN only. Check: a normal flash logs "Generation
  check OK"; with the ECU stuck in its bootloader, the "Which car is this ECU from?" picker
  held open ~30 s still lets the flash go on (a side thread sends Tester Present through the
  cable DLL meanwhile; some clone DLLs may not like a second thread). Also unproven: the NC1
  boot signature has only been seen in ROM files, never read from a real NC1 ECU.
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
  PR adds, for every adapter: "still programming" refusals (NRC 0x22 on 0xB1/0x34) offer an
  ECU reset. Proven over OBDX only; check them
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
