# Session Notes

Only open work and live decisions go here. Completed work goes in `CHANGELOG.md`; delete an item
here once it's done. Tracked work lives in GitHub issues (`gh issue list`) — don't duplicate it.
History before Sep 24, 2026: `git show f4709f1:.claude/notes.md` (search it, don't read it end to end).

## Open items (not tracked in an issue)

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
- **B2 spurious dirty flag** after undo back to the saved state: safe-side (extra save prompt, no
  data loss). A real fix needs per-document clean-state tracking across per-table undo stacks.
- **Unconfirmed, from Jul 6**: retest-on-binary for B2/B5/B15 may already be done; decision D4
  (retire Option-A `WiCANFlasher`?) was still open.
