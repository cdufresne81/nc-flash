# Session Notes

Only open work and live decisions go here. Completed work goes in `CHANGELOG.md`; delete an item
here once it's done. Tracked work lives in GitHub issues (`gh issue list`) — don't duplicate it.
History before Sep 24, 2026: `git show f4709f1:.claude/notes.md` (search it, don't read it end to end).

## Open items (not tracked in an issue)

- **ROM definitions (after PR #120, speeps import):**
  - Decided: nc-flash-re is the working copy where definitions are edited; NC Flash holds the
    released copy (copied in, never hand-edited). User to seed nc-flash-re with the reformatted
    set before its `sync_ncflash_definitions.py` next runs, or the old layout comes back and
    fails `tools/metadata_lint.py check`.
  - Tracked in issues: #121 (deliver definition updates to existing installs), #122 (duplicate
    table names), #123 (TCM odd-address cells/overlaps, unconfirmed), #124 (porting tool).
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
