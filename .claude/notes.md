# Session Notes

## ⏳ PENDING VALIDATION & FOLLOW-UPS (from user manual test, Jul 6, 2026)

- **[RETEST-ON-BINARY] B2 + B5 not yet verified** — user has the installed binary running, can't
  test the working-tree branch for these. **Re-test once a new binary is built + installed on this PC:**
  B2 = edit → Save → Undo → close must prompt to save; B5 = make ROM read-only, edit, close+save → error
  dialog + close cancelled (no crash).
- **[B15 — FIXED Jul 6, 2026] Project tab now shows `*` when modified** (was: only standalone ROMs did).
  Fix: (1) `open_project_path` connects `rom_document.modified_changed` → `_update_tab_title`; (2) RomDocument
  carries a `tab_base_title` (`file_name` for standalone, `"[P] {name}"` for projects, kept in sync across
  Save As); (3) `_update_tab_title` prefixes `*` onto `tab_base_title` → a modified project reads `*[P] name`.
  Cosmetic only (close prompt was already gated on `is_modified()`). Tests: `test_tab_title_modified_marker.py`
  (5). **[RETEST-ON-BINARY]** confirm visually once a new binary is installed.
- **B12 confirmed working; compare window is SLOW** (user: not a regression, feels identical to before).
  This is already tracked as **E3** (Phase 5) — compare window computes all diffs synchronously in its
  constructor. No new action; validates the E3 finding.
- **B7, G7 accepted blindly** (hard to test without bench). User priority: **do not break current flashing**.
  Note: Phase 0-2 made NO changes to the flash/transport core (`src/ecu/` driver/session/transport);
  B7/G7 only touch `src/ui/ecu_window.py` (error dialog on flash-*start* failure + status-label text) —
  the flash sequence itself is byte-for-byte unchanged.

## 🔍 FABLE 5 DEEP REVIEW of arch-hardening branch — ALL fixes applied (Jul 7, 2026)

**Driver:** user asked to review Opus's uncommitted branch work and fix issues. Multi-agent
adversarial-review workflow (`wf_c66a169c-c33`, salvaged via resume after a session-limit
interruption — 34 agents, 22 confirmed / 5 refuted findings across 7 dimensions) + my own
verification. Baseline 1558/12 before fixes; **final suite 1583 passed / 12 skipped** (+25 tests,
0 regressions), black clean. NOT committed (user-gated).

**Fixes beyond the first batch below (all verified findings addressed):**
- **B8 COMPLETION (medium):** bulk handlers + compare-copy/MCP external-edit pipeline now
  write-then-notify with best-effort rollback of partially-landed bulk writes; MCP returns
  success:false on a failed write (was success:true). +11 tests in test_edit_write_commit.py.
- **B11 hardening (medium):** single-instance handoff now requires an event-loop ACK from the
  running instance; hung instance → new window opens (was: silent exit 0, Task Manager recovery).
- **B4 hardening (medium):** failed project open (missing definition/load error after bind)
  unbinds the tabless project so it can't invisibly block all later opens; session-restore
  project skip now surfaces in the status bar. +3 tests.
- **Guardrail-test gaps (medium):** architecture walker now sees `from src import ui`/`from .. import ui`
  back-edges; flash-prep equivalence gained a static composition ratchet (both flash modules call
  prepare_flash_image, neither touches correct_rom_checksums/get_sbl_data) — the byte tests alone
  were circular post-refactor.
- **Low fixes:** B10 axis-child failure now drops the crippled parent table (+2 tests); B9 temp-file
  cleanup on failed os.replace; #cc6600 → theme.WARNING_AMBER + main.py added to theme ratchet
  (budget 4, ecu_window 40→39); test_runner bulk emits fixed (1-arg vs Signal(object,list) TypeError,
  pre-existing); validate_autoblip_defs exits non-zero on failure + argc guard; wican_flash_diag
  --commit help/usage no longer advertises a real flash; WICAN_TRANSPORT.md §6 rewritten for the
  SD-staged path; ARCHITECTURE.md pointers/theme-builder/tense fixed.
- **DEFERRED (deliberate):** B2 spurious dirty flag after undo-back-to-saved-state (safe-side —
  extra save prompt, never data loss; a correct fix needs per-document clean-state tracking across
  per-table undo stacks). Auto-Blip XML left untouched (user's separate stream) but now documented
  in CHANGELOG [Unreleased] Added with a "reads garbage on stock ROM" note.

- **✅ FIXED (HIGH): project revert targeted the ACTIVE TAB, not the project ROM** —
  `_on_revert_version` used `get_current_document()`: with a foreign tab active it reloaded the
  wrong document (discarding its unsaved edits) while the project doc kept stale pre-revert bytes
  (a later save silently undid the revert). Also cleared pending changes GLOBALLY and left undo
  stacks recorded against pre-revert bytes (replaying one would corrupt the reverted ROM). Now
  mirrors B3 commit scoping: resolve by `working_rom_path`, close that ROM's table windows,
  drop its undo stacks, `clear_pending_for_rom`, `set_modified(False)`, re-baseline edit state.
  Tests: `tests/test_revert_scope.py` (5).
- **✅ FIXED: `src/mcp` was missing from the architecture ratchet** — `tests/test_architecture.py`
  FORBIDDEN now includes `mcp` in the no-`ui`-import set (ARCHITECTURE.md already claimed it was
  enforced; src/mcp is clean). CLAUDE.md rule line aligned.
- **✅ FIXED: residual-checksum `ChecksumError` lost its diagnostic** — `prepare_flash_image` now
  folds the applied-corrections count + residual regions into the error message (the corrections
  list only reached the caller's log on success). Message-only, pre-ECU-contact, brick-safe.
- **✅ FIXED: 3 stale `ecu_window.py` docstrings/comments** still described the retired host-driven
  `WiCANFlasher` abort-and-restart flash (now name `WiCANSdFlasher` SD-staged path).
- **CHANGELOG:** D1 entry now documents the failure-path deviations honestly (pre-connect SBL →
  typed `FlashError` instead of raw `ValueError`; `build_flash_package` identical-ROM check ordering);
  new Fixed entry for the revert scoping bug.
- **Verified sound (no action):** flash-prep unification byte-equivalent (independently recomputed +
  empirical byte-compare on lf9veb.bin full+dynamic); Phase 3 edit-state ownership (late-bound
  rom_path lambdas are Save-As safe; no stale signal consumers); clamp_ratio/format helpers exact
  behavioral matches; B1-B15 fixes as claimed.
- **⚠️ FLAG for user:** `examples/metadata/lf9veb.xml` Auto-Blip tables (+37 lines, 0xfcac0-0xfcafc)
  + `tools/validate_autoblip_defs.py` look like a SEPARATE work stream riding on this branch.

## 🚀 ARCH HARDENING — /goal: execute ALL remaining phases (Jul 6, 2026 — session 3, IN PROGRESS)

**Driver:** user `/goal` = execute every remaining phase of `.claude/plans/architecture-goal.md`,
then `/simplify`, then Fable review, then produce an end-to-end test checklist. Branch
`feature/architecture-hardening`. NOT committed (user-gated). Ultracode on — using workflows for
map/design + adversarial verify, implementing coupled edits inline.

- **✅ Phase 3 CORE DONE (C1/C2/C3/C5)** — single-owner edit state.
  - New `src/core/table_edit_state.py` `TableEditState` (pure: borders + capture-once originals).
    `RomDocument` owns `document.edit_state` + tint (`get_color()/set_color()`). Viewer takes
    `edit_owner` and delegates all border storage (no dict across boundaries).
  - `TableViewerWindow` re-emits `cell_edited/bulk_edited/axis_edited/axis_bulk_edited` with rom_path
    bound; `_get_sender_rom_context` + silent active-tab fallback DELETED → `_resolve_edit_target`
    fails loud. Handlers gained a rom_path first arg.
  - Color allocator/swatch/picker/compare/close/Save-As all go through the document; MainWindow keeps
    only the palette-cycle allocator.
  - C3 minimal: commit re-baselines borders + re-captures originals (`_reset_document_edit_baseline`).
  - **5-lens adversarial verify (workflow phase3-verify): NO behavior regression.** Full suite
    **1549 passed / 12 skipped**. Tests: `test_table_edit_state.py` (8), `test_phase3_edit_ownership.py` (12).
  - 3f (C4 McpMixin→collaborator) DEFERRED (optional). **[RETEST-ON-BINARY]** manual 2-ROM cross-edit smoke.
- **✅ Phase 4 DONE** — D1 (brick-critical): NEW `src/ecu/flash_prep.py::prepare_flash_image` shared by J2534
  `_flash_rom_inner` + SD `build_flash_package` + byte-equality gate `test_flash_prep_equivalence.py`. D2 deleted
  `flash_setup_dialog.py`. D4 (user-approved) retired Option-A WiCANFlasher flash methods → gate-only. D3-step1 +
  D7/D8/D9 dedups (clamp_ratio/value_to_color, shared format helpers, dedup_dtcs, hoisted ISO15765 imports).
  **DEFERRED:** D5 (_http_request — heterogeneous connectivity code, conservative), D3-step2 (shared cell-renderer),
  D8-part2 (conditions→read_dtcs seam), D6 (bench).
- **✅ Phase 5** — E2 lazy matplotlib (measured **~1.15 s** off cold start; ratchet `test_lazy_matplotlib.py`).
  E1 DEFERRED (hardware-gated ECU threading), E3/E4/E5 DEFERRED (QThread/optional).
- **✅ Phase 6a** — NEW `src/ui/theme.py` + `get_toolbar_stylesheet` (F2: 3 toolbars migrated); shrink-only
  color-literal ratchet `test_theme_ratchet.py` (baseline 104). **6b ROM sidebar DEFERRED** — needs a focused
  session + user screenshot sign-off; B1 (its correctness driver) already fixed.
- **✅ Phase 7** — G5/G6 pure-logging (0x41 WARNING→DEBUG; log swallowed RPM/voltage exception). G1-G4 + G5-cap
  DEFERRED (wire-adjacent, bench-gated).
- **✅ Phase 8** — H1 (README WiCAN), H2 (CI win/3.14 leg), H5 (log prune 30), H7 (git rm debug artifact),
  H8 (`__test__=False`), H10 (rom_detector multi-match warn). H3/H4/H6/H9/H11 DEFERRED.
- **✅ Finalize** — simplify workflow (5 safe cleanups applied: redundant is_modified guard, dead return,
  display axis-gradient→clamp_ratio ×3, unused J2534Error/UDSError imports, read_dtcs→dedup_dtcs). Fable review
  (above) caught + fixed the axis-refresh bug. **Full suite 1557 passed / 12 skipped** (only the 2 env clipboard
  COM flakes fail). NOT committed — awaiting user validation. See goal doc for the full deferred-items handoff.
- **✅ Fable review pass (Jul 6)** — fixed the flagged pre-existing asymmetry: `_apply_external_axis_edits`
  now refreshes an already-open table window (mirrors `_apply_external_cell_edits`; compare-copy axis
  values no longer stale on screen). Tests: `tests/test_external_edit_refresh.py` (3). Also freshened two
  stale docstrings in `src/ecu/wican_sd_flash.py` (comment-only). CHANGELOG Fixed entry added.
  Review verdict: flash-prep unification byte-equivalent (dynamic diff uses raw rom_data in both old+new
  code → same flash_start_index); Phase 3 ownership, B-fixes, theme, lazy-matplotlib all clean.

## 🔶 ARCH HARDENING — Phase 3 STARTED (single-owner state) (Jul 6, 2026 — session 2)

**Branch:** `feature/architecture-hardening`. **/goal driver:** `.claude/plans/architecture-goal.md`
— Phase 3 partially done: the 3 self-contained cleanups landed; the coupled C1/C2/C3 core remains.

- **B15** (from prior manual test) — project tab now shows `*` when modified. RomDocument gains
  `tab_base_title` (`file_name` standalone / `[P] {name}` project, synced on Save As); `open_project_path`
  connects `modified_changed`; `_update_tab_title` prefixes `*` onto `tab_base_title`. Test:
  `test_tab_title_modified_marker.py` (5). **[RETEST-ON-BINARY]** confirm visually on next installed build.
- **3d (C6)** — deleted FlashMixin's dead ECU-session half (`_ecu_session` was always None) + the false
  `_main_window._ecu_session` read in ecu_window + the no-op `_cleanup_ecu_session()` closeEvent call.
  Kept `_on_patch_rom`. Behavior-preserving (dead code never ran). ECUProgrammingWindow = sole session owner.
- **3e (C7)** — MCP `/api/modified` routes through `ChangeTracker.get_pending_changes_for_rom(doc canonical path)`
  instead of reading `_pending` with a divergent path normalization. Test: `test_mcp_list_modified_scope.py` (3).
- **C8** — TableKey hints/docstrings fixed (`_pending`, `_stacks`, `get_or_create_stack`, `set_active_stack`,
  module docstrings); dead `\0` fallbacks dropped from `extract_table_address`/`extract_rom_path` (live
  bare-address path kept + documented); removed unused `Union` import.

**Gates:** black clean; enforced flake8 (E9,F63,F7,F82) clean for my changes (2 pre-existing F821 forward-ref
strings on main.py unchanged — identical on HEAD, CI's pyflakes tolerates them); full suite **1529 passed / 12
skipped** (1 warning = pre-existing H8). **NOT committed — awaiting user validation.**

**⏭️ NEXT (Phase 3 core — the risky coupled unit, do as ONE focused session):**
- **3a (C1)** move `modified_cells` + `original_table_values` (+ rom color) onto RomDocument; TableViewer +
  MainWindow go through document methods; NO dict handed across object boundaries (main.py:~1520 wiring,
  table_viewer.py mutations at ~704/721/807/841, main.py mutations ~1852/1902/1955).
- **3b (C2+C5)** signals carry rom_path; DELETE `_get_sender_rom_context()` + its silent active-tab fallback
  (main.py:~1799-1821). Window re-exposes viewer edit signals (kills the Law-of-Demeter break C5).
- **3c (C3)** QUndoStack (+ original snapshot) = the ONE "modified" owner; dirty derives from `isClean()`;
  reset pending baseline + borders on commit. Real-QUndoStack tests (not mocked).
- **3f (C4, optional)** extract McpMixin → owned `McpServerController`.
- **Smoke gate:** 2 ROMs + detached table windows, edits land on the RIGHT ROM.

## ✅ ARCH HARDENING — Phase 2 (robustness batch) LANDED (Jul 6, 2026)

**Branch:** `feature/architecture-hardening`. **/goal driver:** `.claude/plans/architecture-goal.md`
— Phase 2 ticked ✅. Nine robustness/correctness items, all host-side (no ECU wire behaviour change).

- **B6** excepthooks → `main._install_exception_hooks` (sys + threading, log CRITICAL then chain).
- **B7** flash-start `except Exception: pass` → log + "Flash Error" dialog (ecu_window, both handlers).
- **B9** metadata_writer atomic write (tmp+fsync+os.replace) + XPath variable (apostrophe-safe).
- **B10** definition_parser guards numeric parse → skips malformed table, rest of file loads.
- **B11** unconditional single-instance; bare re-launch focuses running instance (`_IPC_FOCUS_TOKEN`).
- **B12** compare_window closeEvent nulls whichever parent ref (`compare_window`/`_compare_window`) is self.
- **B13** test_runner table_browser screenshot target → `get_current_document()`.
- **B14** command_server rejects non-dict JSON body with 400.
- **G7** ECU status label shows "Disconnected — <reason>" (amber on loss), UI-only.

**Tests:** test_exception_hooks, test_metadata_writer, test_definition_parser, test_single_instance,
test_command_server, test_compare_window (+ B13 in test_runner path). **Gates:** black clean; full suite
**1521 passed / 12 skipped** (1 warning = pre-existing H8). **NOT committed — awaiting user validation.**

**Next:** Phase 3 — single-owner state (C1 root-cause structural refactor: move per-ROM edit state onto
RomDocument, delete `_get_sender_rom_context` sender-walk, QUndoStack dirty authority, delete FlashMixin
dead ECU half, MCP public accessor). **L, 2-3 sessions — recommend validating/committing Phases 0-2 first**
before layering the structural refactor. Still open for user: D4 (Phase 4), H2 (Phase 8).

## ✅ ARCH HARDENING — Phase 1 (critical data-integrity) LANDED (Jul 6, 2026)

**Branch:** `feature/architecture-hardening`. **/goal driver:** `.claude/plans/architecture-goal.md`
— Phase 1 ticked ✅. All six data-integrity bugs fixed with tests.

- **B1** tab-drag wrong-ROM desync → `MainWindow.on_tab_moved` reorders `rom_stack` in lockstep
  with the `tabMoved` signal (`tests/test_tab_reorder.py`).
- **B2** undo-after-save silent discard → `set_modified(True)` after successful write in both undo
  appliers (`tests/test_undo_dirty_flag.py`). Full QUndoStack-authority is Phase 3.
- **B3** commit folded in foreign ROMs + snapshotted active tab → commit now resolves the project
  doc via `working_rom_path`, saves/commits/clears only it. New `ChangeTracker.get_pending_changes_for_rom`
  + `clear_pending_for_rom` (`tests/test_commit_scope.py`).
- **B4** 2nd project rebound the singleton → `_ensure_single_project` guard (interactive prompt+close;
  restore skips via `prompt_on_switch=False`). Updated 3 session-test call asserts (`tests/test_project_switch_guard.py`).
- **B5** unguarded save-on-close → guarded in `_handle_close` + `close_tab`, cancel close on `RomFileError`
  (`tests/test_close_save_guard.py`).
- **B8** UI asserted un-written values → write-then-notify for single cell/axis edits;
  `_write_to_rom_and_mark_modified` returns success, revert viewer + surface error on failure.
  **Deviation (noted in goal doc):** scoped to single-edit handlers; bulk/external → Phase 3 (`tests/test_edit_write_commit.py`).

**Gates:** `black` clean; full suite **1507 passed / 13 skipped** (1 warning = pre-existing H8;
1 skip = flaky command_server 404 race, unrelated). **NOT committed — awaiting user validation.**

**Next:** Phase 2 (robustness batch: B6 excepthooks, B7 flash-start handlers, B9 metadata atomic write,
B10 definition parse guard, B11 single-instance, B12-B14, G7). Still open for user: D4, H2.

## ✅ ARCH HARDENING — Phase 0 (LLM-first guardrails) LANDED (Jul 6, 2026)

**Branch:** `feature/architecture-hardening` (created for the whole refactor). **/goal driver:**
`.claude/plans/architecture-goal.md` — Phase 0 ticked ✅, no deviations.

**What shipped (dev-facing, zero runtime change):**
- `CLAUDE.md` → new `## Architecture Rules` section (12 lines, imperative: layering, single-owner
  state, no-mixin, one-pipeline-copy, signals-carry-context, theme source of truth, ecu brick-critical).
- `tests/test_architecture.py` → AST import-ratchet enforcing the two layering directions (no ui from
  core/ecu/utils/api; no ecu from core/utils). Resolves absolute + relative + lazy imports; has a
  vacuous-pass guard (scanned>20). **Passes today** — layering below src/ui is already clean.
- `docs/internal/ARCHITECTURE.md` → one-page layer map, each rule tied to its audit incident
  (C1/C4/D1/C2/F1); added to CLAUDE.md Key Documentation.
- `CHANGELOG.md` → `### Added` entry under Unreleased.

**Gates:** `black` clean, full suite **1482 passed / 12 skipped** (lone warning = pre-existing H8
TestRunner collection notice). **NOT committed — awaiting user validation before any push.**

**Next:** Phase 1 (critical data-integrity: B1 tab-drag desync, B2 undo-after-save dirty, B3 commit
scope, B4 one-project guard, B5 save-on-close guard, B8 write-then-notify). Still open for user:
D4 (retire Option-A WiCANFlasher — recommend yes), H2 (add 3.14 CI leg — recommend yes).

## 📋 ARCHITECTURE AUDIT + HARDENING GOAL AUTHORED (Jul 6, 2026 — ultracode, 25 agents)

**What:** Full senior-architect audit of the host app (user request: find what will bite long-term;
maintainability + clean patterns, no over-engineering; bonus perf + UI). 8 Opus dimension auditors +
April-audit status check + adversarial verifier per critical/high finding + completeness critic
(~1.7M tokens, master @ fbaf144). Two dimensions (ecu, test) + critic re-run as direct agents after
StructuredOutput retry-cap failures; top 2 unverified findings re-verified by the orchestrator.

**Deliverables (NOT committed — .claude/plans is gitignored):**
- **`.claude/plans/architecture-goal.md`** — goal driver, 9 phases (P0 LLM-first guardrails → P8 hygiene),
  each independently landable via /goal. Settled decisions + binding "Not doing" fence inside.
- **`.claude/plans/architecture-audit-findings.md`** — full evidence base (file:line anchors, post-verification
  severities, verified-healthy list).

**Headline findings:** 🔴 B1 tab-drag desyncs tab_bar↔rom_stack → edits/saves/FLASH hit the wrong ROM
(main.py:343, setMovable(True), no tabMoved handler); B2 undo-after-save never sets dirty → silent discard on
close; B3 commit writes EVERY open ROM's pending edits + snapshots the ACTIVE tab (project_mixin.py:173);
B4 second project rebinds singleton ProjectManager (A's tab commits into B's history); B5 save-on-close
unguarded (RomWriteError escapes closeEvent); D1 brick-critical flash-prep pipeline duplicated verbatim in
wican_sd_package (rule-3, no equivalence test). ECU layer verdict: NO critical/high active brick bugs —
defense-in-depth items only (cleanup() bypasses BUSY guard; 30s idle window can false-fail a slow flash).
Healthy: layering below src/ui clean, atomic saves, MCP→Qt marshalling correct, 20/24 April items fixed,
packaging/CI solid.

**User decisions settled:** host-only scope; ECU conservative; UI = theming + ROM sidebar + perceived perf
(NOT settings dialog); guidelines LLM-first (rules in CLAUDE.md + tests/test_architecture.py ratchet, rationale
in docs/internal/ARCHITECTURE.md — repo is ~99% LLM-developed, memory `feedback_llm_first_guidelines`).
**Still open for user:** D4 retire Option-A WiCANFlasher? (recommend yes); H2 CI python leg (recommend add 3.14).

**Next session:** run /goal on `.claude/plans/architecture-goal.md` — Phase 0 (guardrails, ~1h) then Phase 1
(critical data-integrity fixes). Nothing committed this session (analysis + plans only).

## ✅ MOBILE HAMBURGER DRAWER + CONTENT-FIT — deployed & hardware-verified (Jul 3, 2026)

**What:** Replaced the phone-only horizontal-scroll tab strip with an off-canvas **hamburger drawer**
(fixed top bar `☰` + "NC Flash", slide-in sidebar over a dimmed backdrop, closes on tab select; desktop
untouched via `display:none >=641px`), and fixed **all horizontal page overflow** across the modern phone
width range. Shipped as **PR #17 → merged into `wican-pro`** (merge `44113ed`), 3 commits: `a84a6db`
(drawer + form-input box-sizing), `75d03ff` (form-table `td` border-box), `b3fe618` (`.content` border-box
+ scrollable Files table). CI green (build 3m28s). **Deployed to bench WiCAN** `v1.2.0-30-gb3fe618` via 3 OTA
cycles (boot_count 19→22, clean version-stamp provenance each time).

**Verification method (reusable):** OS window-resize is a no-op on the maximized automation window, so I test
responsive layout by injecting a **same-origin 390px `<iframe src="/">`** — an iframe establishes its own
viewport, so `@media(max-width:640px)` fires exactly like a phone, and same-origin lets me measure
`scrollWidth-clientWidth` per tab + drive the drawer. NOTE: **CSS transitions are frozen** in that background
iframe (compositor not ticking) — verify settled geometry with `*{transition:none!important}` and trust the
target state; the slide animates fine on a real foregrounded phone (proved with a control element).

**Live-device sweep (final, v1.2.0-30, real files + sleep banner visible):** 0 horizontal page overflow at
**320 / 360 / 375 / 390 / 412** px (full modern iPhone+Android range + legacy). Drawer opens to translateX(0)
+ backdrop; `#mobile_topbar` (z:200) topmost so ☰ is always tappable; the pink "You must agree" banner is the
transient `#notification` toast (z:1000, display:none normally). **Two overflow bugs were caught ONLY by
testing real hardware** (empty local render was clean): the sleep-mode agreement `td` (banner shown only when
sleep disabled) and the populated Files table — static checks/local server could never have surfaced them.

## ✅ WiCAN HARDWARE VALIDATION — deployed firmware GREEN across the board (Jul 2, 2026)

Ran a non-destructive hardware suite against the live bench WiCAN (192.168.1.169) + MX-5 NC ECU.
Device runs the **deployed base** `v1.2.0-21-g55c81b9` (= the P0 base; my uncommitted UI/mark changes are
NOT flashed, so this validates the wireless stack the changes build on, not the new UI itself).

- **Reachability/HTTP:** ping 3ms; `/check_status`, `/csv_status`, `/datalog`, `/event_log/ram` all healthy.
  fw_version 1.02, protocol `poll_log`, csv_log enable, SD mounted, logger running.
- **Coexistence verify (`wican_coexist_verify.py`): ALL PASS** — port-35001 SLCAN ping `NCFRv6`; `/datalog`
  park→resume REST cycle drives `datalog_parked` with clean `park_token` lifecycle (no protocol switch).
- **Dead-man verify (`wican_deadman_verify.py --reaper`): ALL PASS** — full lease round-trip
  (bus_claim/pause/keepalive/release/resume + stale-token→409); **reaper auto-resumed at t+81s** with host
  "vanished" (the brick-safety guarantee, on real hardware).
- **ECU UDS path:** `--read-dtc` via `--auto-config` returned 8 DTCs cleanly (C0121/C0155 + P0118/P0328/P0108
  = powered bench ECU, sensors open, engine off → explains `ecu_status:offline` while UDS works).
- **Full 1MB read fidelity:** two back-to-back `--fast-read` reads, **215.2s / 214.7s** (Tactrix parity),
  smoke 25/25 0% loss, **byte-identical to each other (1048576/1048576)** and to the newest historical read
  `wican_readback_hw9.bin` outside 15 known-adaptive bytes. The tool's "FAIL vs `wican_stmin0_full.bin`" is a
  **stale oracle** (Jun21, pre-reflash+drift), NOT a transport fault. See memory `project_wican_read_oracle_stale`.

**✅ OTA DEPLOYED (Jul 2, 2026):** flashed the feature build `wican-fw_obd_pro_v1_2_0-21-g55c81b9.bin` (embeds all
UI/mark changes — verified by content-grep of the binary) to the bench WiCAN via `curl -F file=@... /upload/ota.bin`
(HTTP 303, 2.8MB/18.9s). Device rebooted clean: boot_count 18→19, unexpected_resets=0, poll_log preserved, OBD Ready.
**Served UI now live:** `/` has "Datalogger (poll_log)"/"Mark Event"/"Event Log"; `/main.js` has consoleMarkClick/
consoleLoadEvents/MODE_NAMES/filesSortKey. **New C code works:** `POST /csv_logger?op=mark` (no session) → HTTP 409.
Coexist/datalog state machine healthy post-OTA. **Rollback secured** first: `rollback_current_55c81b9_clean.bin`
(clean same-commit baseline, sha256 cc5e7618, verified to LACK the UI markers = matches prior deployed build).
NOTE: auto app-rollback is OFF in this fw — a bad OTA won't self-revert; recovery = recovery-AP/safe-mode manual reflash.

**✅ COMMIT SPLIT + MERGE DONE (Jul 2, 2026):** ran `/simplify` on the diff first (4 behavior-preserving cleanups
applied: dead `cell` var + `.style.cssText` no-ops, dead `log_storage` "internal" branch, event-log DOM-rebuild skip,
chip-label alignment; JS-only, node/lint green). Then split: **P0-A `a65d7f0` + P0-B `486c34d` + P0-C `63658ce`
(lint_web.py) → PR #15**, admin-merged into `wican-pro` (merge `030dfee`). **UI features (Items 1-4,7) → `ca7df0f`
on stacked branch `feature/ui-field-console-v2` → PR #16**, retargeted to wican-pro, admin-merged (merge `9c98a01`).
Both CI firmware builds green (esp32s3, ~3.5min). Feature branches deleted. main.js hunk-split done via verified
patch files (byte-content-identical reconstruction confirmed before committing).
**⚠️ Device vs tip:** the bench WiCAN (192.168.1.169) still runs the pre-simplify OTA build — functionally identical
to wican-pro tip (the 4 cleanups are behavior-preserving) but ~4 JS cleanups behind. Rebuild+OTA only if exact parity
wanted. On-device human eyeball / phone-responsive pass still optional (browse http://192.168.1.169).

## ✅ UI-IMPROVEMENTS GOAL DOC EXECUTED — all 7 items implemented, firmware build green (Jul 2, 2026)

**What:** Executed `.claude/plans/wican-ui-improvements-goal.md` end-to-end in `../nc-flash-wican-fw` on
`feature/datalogger-trim` (@55c81b9). All edits done in the prescribed merge order; **NOT committed** (user-gated).
Full `idf.py build` (ESP-IDF v5.5.3) **green, exit 0** — every change compiles, incl. the csv_logger.c C changes.
After each item: `python tools/build_web.py` + `node --check main.js` + `tools/lint_web.py` all green.

- **P0-A** Files tab: re-added `var filesCwd/filesSortKey/filesSortDir` before `filesFmtSize` (main.js).
- **P0-B** Submit persistence: restored the `Load()` population block (recovered from `d372fc9` removed hunk,
  mqtt_* excluded), re-added `obj["ap_auto_disable"]` in postConfig, added `loadedLogPeriod` var + `obj["log_period"]`
  re-send, csv_grid_mode "fixed" coercion. `log_filesystem`/`log_storage` confirmed single-option (never clobbered).
- **P0-C** `tools/lint_web.py` (new, no-deps): checks getElementById literals resolve, inline on*= handlers are defined,
  and build_web --check. Green against the whole tree (316 getElementById refs OK).
- **Item 3** Mode chip: `MODE_NAMES` friendly map at `consoleLoadChips` (raw value in title); dropdown LABELS renamed
  datalogger-first (values byte-identical — persistence strcmps the strings).
- **Items 4+1** (one logical commit): un-brick banner removed from Console → `.guard-note` under System→Firmware
  Update + About "Docs/Releases" row; Event Log card (`/event_log/ram`, newest-first cap 15, severity pills, unsynced→
  uptime, textContent XSS-safe), refresh piggybacks the 1.5s csv_status poll (5s-throttled, tab-visibility-gated).
- **Item 2** Mark Event: csv_logger.c `csv_mark_pending` volatile int8 (writer-consumed), `,mark` header + `,X`/`,` row
  column (bounded-append), clear at session-open, `op=mark` in csv_control_handler (409 when no session) + `EVL_INFO`
  timeline emit; UI amber "Mark Event" button gated on `session_active` + `consoleMarkClick()`.
- **Item 7** Responsive: viewport→device-width + range-syntax queries normalized (atomic pair); ≤640px tier (sidebar→
  scroll strip, logo hidden, files Type-col hidden, touch targets ≥44px, 16px inputs); files-table inline styles→
  `#files_table` CSS rules (`cell=''` in filesRender); openTab scrollIntoView. Grep gates: no `width=1024`, no `width<=`.

**Change surface (uncommitted):** csv_logger.c (+59), csv_logger.h (comment), homepage_full.html (+247), src/main.js
(+174), src/homepage.html (regenerated), tools/lint_web.py (new). **Commit split still to do per goal doc** (P0 →
`feature/datalogger-trim`/PR#15; features → new branch). **Hardware bench test still pending** (device-gated; P0 first).

## 📋 UI-IMPROVEMENTS GOAL DOC AUTHORED + 2 TRIM REGRESSIONS FOUND (Jul 2, 2026 — ultracode, 11 agents)

**What:** User requested a goal document for 7 web-UI items (event-log card, Mark-event CSV column, Mode chip,
un-brick note relocation, Files-tab bug, Submit-changes verification, phone-responsive UI). Ran an ultracode
workflow (5 investigators + 5 adversarial verifiers + completeness critic) over `../nc-flash-wican-fw` @
`feature/datalogger-trim` (55c81b9). Deliverables (NOT committed):
- **`.claude/plans/wican-ui-improvements-goal.md`** — goal driver (settled decisions, merge order, test plan, rollout)
- **`.claude/plans/wican-ui-improvements-specs.md`** — the 5 full anchor-verified specs + adversarial verdicts

**🔴 CRITICAL findings — both are regressions inside open PR #15 (gate its merge; fix on the trim branch first):**
1. **Files tab renders empty** — commit `d372fc9` (MQTT cut) swallowed `var filesCwd/filesSortKey/filesSortDir`;
   `filesRender` throws ReferenceError after clearing the tbody. Backend healthy (live-probed). Fix = re-add 3 decls.
2. **Submit silently clobbers ~18 settings** — same commit also cut the `Load()` population block +
   `ap_auto_disable` send: every Submit persists stock HTML defaults (`csv_log→disable`, `ap_pass→"Testpass"`,
   `port→3333`, sleep/IMU/CSV-grid → defaults); BLE-enabled configs can't submit at all. Bench device verified
   UNAFFECTED so far (`/check_status` 2026-07-02: csv_log=enable, port=35000).
3. **New hard fence:** REFACTOR_PLAN Phase 3 item 4 (trim protocol dropdown) must NEVER run as written — removing
   poll_log ends in a boot-time `config_error` **full factory reset** (store accepts "", boot validation wipes config).
   Also: 5 protocol modes remain live (user's "single mode" premise false) → keep Mode chip, label-only renames.

**Also this session:** deleted 3 stale untracked files (roundtrip_flash.py, full_suite_deadman.txt, full_suite_hw9.txt)
via subagent per user ask. **Round-trip flash on the trim build (PR #15 belt-and-braces) NOT run** — the permission
classifier blocked spawning it from a tentative user remark; needs an explicit user go (source `wican_roundtrip_source.bin`
1 MiB verified present; script deleted with the cleanup — trivially recreatable, see git history of this session's notes).

## 🚀 #5 DATALOGGER TRIM COMPLETE — firmware PR #15 open (Jul 1-2, 2026)

**Goal executed end-to-end (user /goal, full autonomy):** `../nc-flash-wican-fw` branch `feature/datalogger-trim`
(rebased onto wican-pro tip via merge `711451a`) now carries the ENTIRE REFACTOR_PLAN.md trim: WiCAN collapsed to the
two-mode datalogger/flasher. **16 atomic commits, build green at each; PR #15 → wican-pro OPEN, NOT merged** (awaiting
user review; merge is user-gated per repo ruleset).

- **Phase 0:** web pipeline OWNED — `homepage_full.html` was BEHIND the shipped minified UI (Files tab/CSV settings only
  in src/); reconciled readable source regenerated FROM the live page (parse5+js-beautify), `tools/build_web.py`
  (pinned html-minifier-terser, --check mode) is now the only way to touch the embedded UI. `docs/TRIM_REGRESSION_RUNBOOK.md` added.
- **Cut:** ha_webhooks, vpn_manager+esp_wireguard, ftp, obd_logger+sqlite+Dashboard, realdash+gvret, debug_logs,
  orphan ws_router, autopid cloud destinations (MQTT/HTTP/ABRP + publish task + send-to plumbing), mqtt.c gateway
  (+canflt), https_client_mgr (+internet asset fallback), cert_manager, ws_server (+CAN Monitor/Terminal tabs),
  Vehicle-Specific + Destinations UI. **KEPT:** battery-alert MQTT in sleep_mode (own esp-mqtt client, batt_* keys),
  twai timing table, event-bit positions (MQTT/VPN bits defined-but-dead — FLASH_ACTIVE_BIT unmoved).
- **Added:** Field Console landing tab (one-tap Start/Stop Trip on POST /csv_logger, live rec state, SD/WiFi/proto/FW
  chips, Recent Trips w/ downloads, un-brick footer); /csv_list now mtime + NEWEST-first.
- **HW-validated on the bench (192.168.1.169 + live ECU):** size 3.61→2.81 MB (−22%); trips record real ECU data
  (213/844-row CSVs, 19-22 ch, download+parse OK); 35001 NCFRv6 ping; wican_coexist_verify ALL PASS; **production
  ECUSession.connect_ecu() coexist connect no-reboot + 8 DTCs + clean lease teardown on the trimmed fw**; OTA rollback
  drill old→new both ways. NC-Flash contract + un-brick guardrails **zero-diff** vs 711451a (2 comment lines only).
- **Gotchas hit:** (1) a non-v* git tag (`pre-trim-baseline`) broke `git describe`-derived fw_version (0.00) + binary
  naming → tag DELETED, baseline = merge commit 711451a + local `rollback_trim_baseline.bin`; version stamping needs
  `idf.py reconfigure` to pick up tag changes. (2) `.gitignore` now excludes `*.bin`/build logs (a git add -A briefly
  swept 10 MB of bench binaries into a commit — caught + amended before push).
- **Next session:** review/merge PR #15 (user), then optional: run a belt-and-braces SD coexist flash on the trim build,
  release tag, and the deferred §7 branches (cloud-upload-on-sleep, UDP telemetry).

## 🚀 RELEASED: host v2.9.0 + firmware v1.1.0 — #36 coexistence fully landed (Jun 28, 2026)

**What:** User validated the no-reboot coexistence via the NC-Flash UI ("working well") and authorized the merges. Both PRs admin-merged, version-tagged, and GitHub-released:
- **Host** `cdufresne81/nc-flash`: PR #81 → `master` (merge `4388dcc`); CHANGELOG `[Unreleased]`→`[v2.9.0]` (`dd96ace`); **tag `v2.9.0`** + release published. Bundles the whole-session bus reservation + 0x41-drain fix + the full accumulated WiCAN stack since v2.8.0.
- **Firmware** `cdufresne81/nc-flash-wican-fw`: PR #11 (`claude/coexistence-slcan-port`) → `wican-pro` (merge `d17733d`, incl. `283bb23` #43 lease dedup); **tag `v1.1.0`** + release published (no CHANGELOG in that repo — notes written by hand).

**Gotcha (recorded in memory):** host `master` is governed by a repo **ruleset** ("changes must be made through a pull request"), not classic branch protection (`branches/master/protection` → 404). `gh pr merge --admin` + direct `git push` to master both succeed via **admin bypass** (warns but the ref updates). This is why PRs show `reviewDecision: REVIEW_REQUIRED`.

**Notes:** OTA-from-fork-releases isn't wired yet (#8), so the firmware release tag does NOT auto-push to devices — flash via `/upload/ota.bin`. Device 192.168.1.169 already runs the released build. Optional confirmation left: a GUI coexist connect end-to-end + one more real SD coexist flash on v1.1.0. New backlog: firmware issue **#12** (event_log flash-operation visibility). See [[project_wican_slcan_coexistence]].

## ✅ #43 DONE: firmware lease_t refactor (dedupe claim/park primitives) — ultracode, built + adversarially verified (Jun 27, 2026)

**What:** Collapsed the two byte-identical dead-man lease families in `../nc-flash-wican-fw/main/can.c` — the host-bus-claim (`s_claim_*`) and datalog-park (`s_park_*`), each {active+token+owner_gen+deadline} with identical arm/renew/release/reap/token logic — into ONE `volatile lease_t` + generic `lease_arm/renew/release/reap/token` + `lease_clear`, with `can_host_bus_claim_*`/`can_park_lease_*` now thin one-line wrappers picking `&s_claim` / `&s_park`. Net **−52 lines** (88 ins / 140 del). Zero public-signature changes (`can.h` git-diff EMPTY). Single shared `s_token_seq` + `s_park_mux` preserved (token global-uniqueness, single-acquisition cross-lease snapshot). Every field kept `volatile` so the lock-free `.active` hot-path reads keep exact single-byte semantics.

**Method (ultracode):** Workflow A (3 agents) = invariant census (13 brick-class invariants) + caller census (all callers rely only on public sigs → internal-only) + design (verdict SOUND). Implemented inline. ESP-IDF 5.5.3 esp32s3 build = **0 errors, no new warnings, 30% free**. Workflow B (6 skeptics, one per invariant) = **ALL PRESERVED**; the volatile/dual-core lens compiled+disassembled both versions and proved **byte-identical Xtensa codegen** (`memw; l8ui; extui`) for every lock-free read + zero `memcpy` of `lease_t`. Only finding: cosmetic comment "already resumed"→"already released" in the shared release helper (no runtime effect).

**State (Jun 28 update):** user **lifted the brick-critical test gate** ("update the wican software and flash the ECU to confirm validity"). #43 is now **HW-VALIDATED + /simplify-clean + committed & pushed**: OTA'd the #43 build to the live WiCAN (192.168.1.169) and ran `wican_deadman_verify.py --reaper` → **ALL PASS** (arm issued tokens 1/2 from the shared seq, renew accepted, release cleared, stale-token resume → 409, and the dead-man **reaper auto-cleared claim+park at t+81 s** = claim-TTL 75 + grace 3 — every refactored primitive exercised on real hardware, zero ECU contact). `/simplify` (4 agents) found the refactor already clean — **no code changes** (one optional redundant-comment finding skipped as locally useful; one pre-existing naming-asymmetry note out of scope). Committed to `claude/coexistence-slcan-port` + pushed. See [[project_wican_slcan_coexistence]], [[project_wican_firmware_build_env]], [[project_wican_hardware_in_loop]].

## ✅ #36 FIX: WiCAN no-reboot connect/DTC/scan hung on SID 0x3E — host never reserved the bus (Jun 27, 2026)

**Symptom (user bench):** connect over the coexist port logged "No-reboot coexistence firmware NCFRv6 detected" then froze ~60 s and died `Timed out waiting for response to SID 0x3E`. I first **mis-diagnosed it as ECU-side** (ignition/CAN); user **falsified** it by reading DTCs fine with a Tactrix on the same ECU/ignition. (Memory `project_wican_coexist_ecu_silent_diag.md` rewritten with the correct root cause so I don't repeat that.)

**Root cause (confirmed in firmware + on live HW):** on the coexist port the device stays in `poll_log`, where the datalogger is the SOLE TWAI consumer. Firmware only forwards the ECU's UDS reply to port 35001 when the host holds a reservation — `can_rx_task` (main.c ~448): `coexist_session = (park || bus_claim) && !flash`. The **flash** path worked because `_datalog_fence` claims+pauses; **connect/DTC/scan never reserved the bus**, so poll_log ate every reply. Proven on the bench: with a manual `bus_claim`+`pause` held, the identical production `tester_present()` + `read_dtc_status()` return instantly over WiFi.

**Fix (host-side; user chose "whole-session reservation"):** `ECUSession` now holds a refcounted bus reservation for the LIFE of a coexist connection. New `WiCANDatalogClient.acquire_bus()/release_bus()/reserved()` (refcounted: real `bus_claim`+`pause`+settle once on 0→1, `bus_release`+`resume` once on 1→0). `_connect_wican` acquires BEFORE the first Tester-Present; `_teardown_wican` releases on every teardown. The flash `_datalog_fence` now nests on the SAME client (session's `wican_datalog` passed to `WiCANSdFlasher`) → exactly ONE bus owner, never a double-claim on the single-owner firmware lease. Dead-man reaper still auto-resumes the logger if the host vanishes. `PRE_SESSION_SETTLE_S` moved to `constants.py`.

**HW-validated (bench 192.168.1.169, NCFRv6):** real `ECUSession.connect_ecu()` over coexist path → no reboot, `parked=True claimed=True` while connected, `read_dtc_status()` returned the 8 DTCs over WiFi, disconnect resumed the logger (`parked=False claimed=False`). Full suite green (1479). Tests added: refcount nesting/exception/no-op (`test_ecu_wican_config.py`), `test_coexist_reserves_bus_for_whole_session` (`test_ecu_session_wican.py`); existing fence tests now drive the real `reserved()`; `test_ecu_window_flash_driver.py` asserts the `datalog=` kwarg.

**Files:** `src/ecu/{constants,wican_config,wican_sd_flash,session}.py`, `src/ui/ecu_window.py` + tests. CHANGELOG updated (Fixed). **NOT committed** (no user land-the-plane yet). Firmware UNCHANGED — pure host fix.

**Follow-on (Jun 27):** the coexist connect logged benign `UDS: unexpected response byte 0x41 for SID 0x3E` WARNINGs — `0x41` = OBD Mode-01 (`0x01+0x40`) responses to the datalogger's polls that were already in-flight when the host took the bus; `pause()` stops new polls but not in-flight replies, so they bled into the first Tester-Present receive (the UDS loop discarded them and connected fine — just noisy). Fix: `_connect_wican` now `coexist.flush()`es after `acquire_bus()` and before the first Tester-Present (drain-until-quiet via the existing `WiCANTransport.flush`). Test `test_coexist_drains_stale_datalog_frames_before_first_uds` asserts order `acquire_bus → flush → tester_present` (fails if flush removed). Confirmed it fails without the line, restored. WiCAN-related suites green (143). **NOT committed.**

## ✅ #36 GAP-2 dead-man's-switch FIRMWARE CORE built + ultracode-verified + DEVICE-VALIDATED (Jun 26, 2026)

Built the firmware core (`../nc-flash-wican-fw` `claude/coexistence-slcan-port`, uncommitted): spinlock park/claim
lease + 1 Hz reaper (`main/datalog_lease_task.c`), 5-op `/datalog` handler + state JSON (`csv_logger.c`),
`slcan_port_conn_gen()`. **Ultracoded the core** (user asked): a 6-lens adversarial Workflow found a **brick-class
TOCTOU** — the reaper snapshotted the lease, dropped the lock, then force-cleared the claim/park UNCONDITIONALLY, so a
fresh `bus_claim`/`pause` re-armed in the gap was destroyed → un-park into a live auth session → brick. **Fix:** moved
park+claim ENTIRELY under `s_park_mux` as `{volatile bool flag + token + owner-gen + u64 deadline}` (only
FLASH_ACTIVE_BIT stays a codec-owned event-group bit, INV-1), and made the reaper a **token+deadline-matched
compare-and-act** (`can_host_bus_claim_reap` / `can_park_lease_reap` — clears only if the lease is STILL the exact
sampled `(token,deadline)`; any in-gap arm/renew bumps it → reap aborts). A focused **3-lens re-verify = ALL CLEAN**
(TOCTOU closure proven, dual-core flag memory-model safe, host-contract+liveness regression clean, 43 contract tests pass).

**Device-validated** (OTA'd the rebuilt bin to 192.168.1.169; new `/datalog` deadman fields confirm the build took):
`tools/wican_deadman_verify.py --reaper` = ALL PASS — every `/datalog` op + token-matched **409**, and the **reaper
auto-resume on real HW** (armed claim+park, no keepalive → both auto-cleared at ≈78 s = claim-TTL 75 + grace 3, with
`bus_idle_ms` climbing as the claim quiesced poll_log). **Zero ECU contact → brick-safe test.** UI-path proven by
`tests/test_ecu_wican_sd_flash.py::TestDeadmanUiPathIntegration` (real `WiCANDatalogClient` through the flash path vs
the firmware-faithful `_MockDatalogServer`; success + failure-teardown + port-only soft-degrade).

**✅ HW-1 PASSED on the LIVE ECU (the brick-critical test).** Raised the fence, entered programming session `0x10 0x85`
over the coexist port (FLASH_ACTIVE_BIT clear = §2 unfenced window), vanished the host mid-auth (no teardown). Fence
HELD (host_bus_claimed True + poll_log parked, bus_idle 24→76304 ms = **zero 0x7E0 injected**) for 81 s; reaper resumed
only at ≈81 s (claim-TTL 75 + grace 3); **ECU survived** (ROM-ID SW-LFDJEA000.HEX, not bricked). Atomic-reap fix proven
on real HW.

**✅ HW-9 PASSED byte-perfect on the LIVE ECU.** Drove the real SD coexist flash (byte-identical LFDJEA reflash from
`wican_roundtrip_source.bin`) over the coexist port with the fence: 1022/1022 → NCFWDONE in 70 s, fence released, **WiCAN
did NOT reboot**. User power-cycled → fenced 1 MB read-back (215 s) → **sha256 byte-identical** (`wican_readback_hw9.bin`).
HW-9 caught a REAL host bug: the preflight link gate pinged the ECU BEFORE the fence was raised, so on the coexist port
poll_log owned the bus and the gate HUNG. Fixed: `_datalog_fence` contextmanager in `wican_sd_flash.py` brackets the WHOLE
host-driven window (gate→auth→fast_write) + a settle so poll_log parks before the first ping; regression test
`test_preflight_gate_runs_inside_the_datalog_fence` locks order [bus_claim,pause,gate,auth,flash,bus_release,resume].

**#36 is now FULLY device-validated** (firmware core ultracode-verified, HW-1 brick-critical + HW-9 byte-perfect on the
live ECU). **Remaining = commit + push (USER-GATED).** Host tree (wican_sd_flash gate-fence fix + UI-path/regression
tests + tools/wican_deadman_verify.py + constants/config/transport from the host half + doc/changelog/memory) and the
firmware tree (`../nc-flash-wican-fw` `claude/coexistence-slcan-port`, currently uncommitted). No push without explicit
user validation. Mark task #36 done after push.

## 🔧 #36 Stage 1 RX-forward HW-VALIDATED + dead-man's-switch host half BUILT (Jun 26, 2026 — late)

Picked up #36 hardware E2E. Found the committed `1ce134a` coexistence build was **incomplete**: it wired the TX
half (host→ECU dispatch on 35001) but NOT the RX half, so host-driven UDS over the dedicated port HUNG (bench smoke
60s, read 320s). Two firmware gaps + the user's dead-man's-switch question, all in `docs/internal/WICAN_DEADMAN_AUTORESUME.md`.

**OTA:** flashed `v1_0_0-5-g6bea7e3` then `-6-g1ce134a` to the bench WiCAN (192.168.1.169) myself via `POST /upload/ota.bin`
multipart. User **waived the USB-backup precondition** (can't pull a backup over WiFi; dual-partition keeps old build
in the inactive slot + bench is USB-recoverable).

**GAP 1 — RX-forward: FIXED + HW-VALIDATED.** `main/main.c can_rx_task` (uncommitted on `claude/coexistence-slcan-port`):
`coexist_session = can_datalog_park_active() && !can_flash_active()`; gate now lets `can_rx_task` take over TWAI when
the logger is parked; new branch parses ECU frames as SLCAN regardless of `protocol` → routes to
`xMsg_SlcanPort_Tx_Queue`. Built `v1_0_0-6`, OTA'd. **Bench PASS:** smoke 25/25 0% loss; full 1MB fast-read 214.8s
(=Tactrix floor) over 35001 with datalog parked, NO reboot; `validate_wican_read` PASS (0 checksum corrections).

**GAP 2 — dead-man's-switch (auto-resume on lid-close/crash, brick-safely).** 13-agent adversarial design workflow
(`wf_831b2db5-a27`) found the brick trap: host runs the UDS auth session (0x10→0x27) BEFORE the codec sets
`FLASH_ACTIVE_BIT`, so that window is unfenced — naive auto-resume injects 0x7E0 → brick. Design = host-asserted
`HOST_BUS_CLAIM_BIT` (BIT3) bracketing the whole window + firmware dead-man reaper. **Host half BUILT + tested
(host-first, user chose):** `WiCANDatalogClient` bus_claim/bus_release/keepalive-daemon/token-aware pause+resume
(409=success)/close + token-aware reconcile (skips when flash_active OR host_bus_claimed); `wican_sd_flash` brackets
`bus_claim→pause→…→teardown-on-abort→bus_release→resume`; `wican_transport.open` SO_KEEPALIVE 5/5/3; `constants.py`
timing contract. **Full suite 1470 pass** (+6 new incl. real-thread keepalive lifecycle); 2 FAILs are unrelated
table-viewer clipboard flakes (OleSetClipboard COM error, not my files).

**NEXT:** GAP 2 **firmware core** (BIT3 + spinlock lease state + 1Hz reaper task + 4 REST ops + idle stamps) → build →
brick-risk OTA → **HW-1 (kill host mid-auth, assert zero 0x7E0)** + HW-2..9 → then commit host tree + push both.
All host work UNCOMMITTED (no push without user validation). Design/HW-tests: `docs/internal/WICAN_DEADMAN_AUTORESUME.md`.

---

## 🚧 #36 BUILT (Jun 26, 2026) — firmware committed + host client built; HARDWARE E2E + commits pending

Executed the goal plan via ultracode (6-agent adversarial audit workflow `wny47i6kg`, Opus tier — note: a
named `agentType` like `Explore` overrides the inherited model with its own cheap default; pass `model:'opus'`).
Built the WHOLE coexistence stack. **Two repos, current state:**

**Firmware (`../nc-flash-wican-fw`, branch `claude/coexistence-slcan-port`) — COMMITTED `1ce134a` (NOT pushed):**
- **36.A** new `main/slcan_port.c/.h` — dedicated always-on SLCAN listener on **35001** (self-contained, shares
  no state with comm_server). Tagged `DEV_SLCAN_PORT`, shares RX queue, private TX queue. Early dispatch branch
  in `can_tx_task` before the `protocol==SLCAN` gate. `NCFRv5→NCFRv6`. Hardened: conn-generation guard + bind retry.
- **36.C** `POST/GET /datalog?op=pause|resume` (`csv_logger.c` + `config_server.c`). Drives a **separate
  `DATALOG_PARK_BIT`** (NOT the codec's `FLASH_ACTIVE_BIT` — reusing it was a last-writer-wins brick trap a stray
  resume could trip). Restores exact pre-pause mode.
- **Brick-class fixes (audit-confirmed):** AUTO_PID poller parks on `can_should_park()` (was unguarded — #1 brick
  path; refuse-on-protocol REJECTED since protocol stays auto_pid during a coexist flash); `mqtt.c` can_send guarded;
  `can_rx_task` parks during a flash; `can_send` documents the single-owner contract.
- **Builds clean** (ESP-IDF 5.5.3 esp32s3, 0 warn/err, 30% partition free). Skipped a false-positive "stack
  underalloc" finding (ESP-IDF `StackType_t` is byte-width).

**Host (`nc-rom-editor`, branch `master`) — BUILT + 1466 tests green, UNCOMMITTED working tree:**
- `WiCANDatalogClient` (`wican_config.py`): `pause/resume/get_state/reconcile`, stdlib-only, **airtight
  soft-degrade** (404/timeout/non-JSON → None, NEVER aborts a flash). Host-keyed `%TEMP%` crash breadcrumb.
- Wired into `wican_sd_flash._trigger_firmware_flash` (pause→auth→flash→resume in `try/finally`, resumes on FWERR).
- `reconcile()` at `session._connect_wican` with **two-instance guard** (`GET /datalog` flash_active).
- +13 tests (`test_ecu_wican_config.py`, `test_ecu_wican_sd_flash.py::TestDatalogCoexistence`). CHANGELOG updated.

**REMAINING (all USER-gated):** (1) commit the host working tree (user chose "commit firmware, then build host
client" — host commit not yet authorized); (2) push both branches; (3) **live HARDWARE E2E** on the bench ECU
(brick-risk; needs deployed-build backup first per `feedback_wican_firmware_backup`): dedicated-port fastread
no-reboot, zero-CSV/zero-0x7E0 during flash, pause/resume round-trip, AUTO_PID park, WiFi-drop recovery.
Done-gate + build details in `.claude/plans/wican-firmware-integration-goal.md`.

---

## ✅ SESSION END (Jun 26, 2026) — #35 + #37 LANDED, #36 re-oriented to REST datalog

**Everything this session is committed + pushed + merged. Only #36 remains open.**

- **#37 host (RPM gate + coexist-port detect)** → **MERGED to master** (PR #79, `033c500`). Post-merge
  `/simplify` cleanup → **PR #80 MERGED** (`7f80209`): dropped dead `_coexist_port`, deduped the rpm-compare,
  gave the 3 new bench tools the standard `_REPO_ROOT` sys.path bootstrap + `NO_ACK_BITS`. Local master synced.
- **#35 firmware (FWB SD-flash on wican-pro)** → hardware-validated (read-back byte-perfect, see below) →
  **MERGED to `wican-pro`** (firmware PR #10, `187604b`). Branch `claude/integrate-fwb-onto-wican-pro` pushed.
- **#36 firmware (no-reboot coexistence)** → branch `claude/coexistence-slcan-port` (`6bea7e3`) PUSHED;
  only the `FLASH_ACTIVE_BIT` interlock core is done. **RE-ORIENTED (user, Jun 26):** no-reboot coexistence is
  now **host-driven REST `pause/resume` datalog, LAYERED ON the dedicated SLCAN port** (REST can't replace the
  port — flash codecs are trapped behind `if(protocol==SLCAN)`). Full design + build plan rewritten in
  `.claude/plans/wican-firmware-integration-goal.md`; investigated via ultracode workflow. Key conclusions:
  - **New endpoint needed:** `POST /datalog?op=pause|resume` driving `csv_logger_set_manual_override()` +
    `can_flash_active_set/clear()`. (`/csv_logger?op=stop` only stops SD persistence, NOT the CAN poller.)
  - **Dedicated SLCAN port (35001 listener + early `DEV_SLCAN_PORT` dispatch + protocol-independent RX-forward)
    is the load-bearing prerequisite — NOT built yet** (enum only). Build it FIRST.
  - 🔴 **MUST-FIX before any live flash (brick-class):** AUTO_PID poller does **NOT** honor `FLASH_ACTIVE_BIT`
    (zero refs in `autopid.c`) → park it on the bit OR refuse flash when `protocol==AUTO_PID`. Plus: host resume
    must be a **standalone path** (NOT folded into `_restore_wican_protocol`, which no-ops on the coexist path);
    **two-instance sidecar guard** (a 2nd NC Flash can resume datalog mid-flash); "HTTP 200 ≠ parked".
- **Next session:** run the goal skill on `.claude/plans/wican-firmware-integration-goal.md` → build #36
  (dedicated port first, then `/datalog`, AUTO_PID gate before any live flash). Bench ECU @ 192.168.1.169,
  brick-authorized. Memory `project_wican_slcan_coexistence` + `project_wican_protocol_revert_gotcha` carry this.
- **Untracked bench scratch left in working tree (intentional):** `roundtrip_flash.py` (writes to ECU),
  `wican_*.bin` (gitignored). Pre-existing non-mine: `examples/metadata/lf9veb.xml`, `tools/validate_autoblip_defs.py`.

---

## ✅ Firmware #35 (read-back PASSED) + ⏳ #36 interlock (Jun 25–26, 2026)

User granted **brick-risk authorization on the bench ECU** (192.168.1.169) + pointed me at the installed
ESP-IDF. Build recipe in memory `project_wican_firmware_build_env` (export.ps1 picks wrong venv → use
`idf5.5_py3.10_env`). Firmware repo `../nc-flash-wican-fw`.

**#35 — integrate FWB onto wican-pro (`claude/integrate-fwb-onto-wican-pro` @ `c8bcd54`):** merge had ZERO
git conflicts but all 5 SEMANTIC landmines verified correct (engine_on_volt, max_uri_handlers=48,
FAST_LOG/POLL_LOG enum, CMake superset, both main.c dispatch + /upload/sd). Built clean (ESP-IDF v5.5.3,
esp32s3). **OTA'd to the adapter** (now `v1.0.0`, was `b79549b`) via `curl -F file=@... /upload/ota.bin`
(A/B-safe; user waived exact-backup rule, rollback_deployed_wican-pro.bin is the fallback). **Live-validated
on the bench ECU:** NCFRv5 ping, DTC read (10), RAM scan+auth seed/key, full 1 MB ROM read (checksum-Δ0),
and a **LIVE FULL FLASH 1022/1022 NCFWDONE** (round-trip of the ECU's own ROM via `roundtrip_flash.py`).
✅ **#35 COMPLETE (Jun 26):** read-back byte-compare **PASS** — full 1 MB read post-power-cycle is
**byte-for-byte identical** to oracle `wican_roundtrip_source.bin` (`wican_readback_postcycle.bin`; ROM ID
SW-LFDJEA000.HEX; 29 dropped blocks all recovered; 338.7s @ 3 KB/s). Proof chain closed: NCFWDONE write →
power cycle → app booted (auth'd RMBA works, real powertrain DTCs) → read-back identical. Task #35 → completed.
NOTE: `tools/wican_bench_read.py` defaults to port **3333** — pass `--port 35000`.
**OPERATIONAL GOTCHA (cost ~1h):** after a power-cycle the WiCAN reverts to its default protocol (`poll_log`),
NOT slcan. Raw bench tools WITHOUT `--auto-config` connect to :35000, ack C/S6/O, but the device doesn't
bridge CAN → bus looks dead-silent + every UDS times out → *false "ECU bricked"*. ALWAYS pass `--auto-config`
(or use NCFlash, which switches to slcan) after any reboot. Also: the `slcan_session()` RESTORES poll_log on
exit, so each flash/read leaves the device non-bridging for the next raw probe. Memory: `project_wican_protocol_revert_gotcha`.
New diag tools added (untracked): `tools/wican_bus_sniff.py` (raw all-IDs CAN sniff), `tools/wican_state_probe.py`
(bootloader-vs-app via RMBA NRC 0x11 vs 0x33), `tools/wican_bus_status.py` (SLCAN `F` flags — firmware doesn't impl).

**#36 — coexistence firmware (`claude/coexistence-slcan-port` @ `6bea7e3`, off #35):** ✅ **FLASH_ACTIVE_BIT
interlock done + builds clean** — the brick-critical core (plan §5): bit+accessors (can.c/.h), poll task
parks on it (poll_log.c), both fast-op codecs set-before-suspend / clear-last-on-every-exit + unified
mutual-exclusion guard (ncflash_fastwrite/fastread.c), DEV_SLCAN_PORT enum (types.h).
⏳ **Remaining:** dedicated-port listener (35001) + `can_tx_task` early-route + `can_rx_task` #476 RX-forward
fix. **Design fork flagged (plan §6):** the UDS auth handshake (plain slcan) over the dedicated port runs
BEFORE the fast-op sets FLASH_ACTIVE_BIT → needs park-and-hold arbitration vs the running datalogger. Then
bump the version marker to NCFRv6 so host #37 detects coexistence. **#36 needs brick-critical hardware
interlock proof** (zero CSV rows / zero 0x7E0 frames during a flash) = bench session w/ user.

**Uncommitted host bench artifacts** (gitignore candidates): `roundtrip_flash.py`, `wican_roundtrip_source.bin`,
`roundtrip_read.log`.

## ⏳ Host #37 — RPM gate + WiCAN no-reboot capability detection (Jun 25, 2026)

**UPDATE Jun 25 PM:** pushed + **PR #79 open** (user said push+PR). The "NOT pushed" / "firmware-gated NOT
started" notes below are superseded — #35 and #36 are both now underway (see the section above).

Branch **`feature/wican-host-rpm-gate-coexist`** (off master `58738a0`). Host half of the WiCAN
coexistence plan (`docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md` §3 host-REQUIRED). All software, fully
unit-tested — the firmware-coupled happy paths validate once #36 ships. **Full suite green: 1453 passed.**

- **RPM gate enforced in code** (`enforce_rpm_gate()` in `flash_manager.py`; `RPM_FLASH_GATE=1.0`;
  `EngineRunningError`). Was UI card-colour only (blocked nothing). Now a one-shot PID 0x0C read **before**
  the programming session (in-session OBD → NRC 0x11, unreadable) refuses to flash when engine running;
  explicit override **off by default**; unreadable RPM does NOT block (no PID 0x0C ECUs). Wired into
  `_on_flash_current`/`_on_full_flash` via `_check_rpm_gate`. Guards J2534 too.
- **No-reboot dedicated-port detection** (`ECUSession._try_open_coexist_port`; `WICAN_DEDICATED_SLCAN_PORT
  =35001`; `COEXIST_MIN_FW_REV=6`). WiCAN connect first probes the always-on dedicated SLCAN port via
  `version_ping` (short timeout); new-enough firmware → connect there, **skip WiCANConfigurator + the ~6 s
  reboot**. Every current build (NCFRv4/5) fails the probe → falls back to the proven reboot path. Strictly
  non-breaking. Contract is shared with #36 firmware (it must bump the marker to NCFRv6+ AND open 35001).
- **Pre-session settle** (`PRE_SESSION_SETTLE_S=0.2`) before SD-flash auth — host brick-safety margin so a
  stray datalogger poll frame can't corrupt the UDS handshake (firmware `FLASH_ACTIVE_BIT` is the real
  guarantee; inert on the legacy path).
- Tests: `TestEnforceRpmGate`, `test_ecu_session_coexist.py`, `TestWiCANCoexistConnect`,
  `test_settles_before_authenticating`. CHANGELOG updated. **Committed to the branch, NOT pushed/merged.**
- **Still firmware-gated (NOT started):** #35 (FWB→wican-pro merge + re-bench, needs ESP-IDF build +
  brick-critical hardware flash) and #36 (coexistence firmware PR, brick-critical). #37's hardware
  done-gate (coexist firmware connects w/o reboot) waits on #36.

## ✅ Drop macOS support + fix master CI ZeroDivisionError (Jun 25, 2026)

**PR #78 merged to master (`58738a0`).** Two commits:
- **`22ab86b` — the real CI fix.** Master CI red on the #76 merge was **misdiagnosed first as a
  "macOS teardown abort"** (all-pass-but-exit-1 theory). Actual cause: `ZeroDivisionError` in
  `FlashManager` completion-speed log lines (`flash_manager.py:704` transfer, `:878` read) — `bytes /
  elapsed` with no `elapsed == 0` guard; a mocked read finishing inside one `time.monotonic()` tick →
  `elapsed == 0.0` → crash **after** the work completed. Hit the **Windows** runner (backslash path gave
  it away), not macOS. Guarded both lines like the per-block lines already were; regression test pins
  `monotonic`. (Lesson saved to memory `feedback_verify_ci_failure_os`: read the raw failing-job log +
  confirm the OS before triaging.)
- **`b2b0418` — dropped macOS support** (no users; flash path is Windows-only J2534). CI matrix →
  Ubuntu 3.10/3.12 + Windows 3.12; removed the `darwin` branch from `paths.get_user_data_dir()` (falls
  through to XDG/Linux); macOS mentions out of README / `docs/internal/LOGGING.md` / test comments.
  **Historical shipped release notes left intact** (user's call).

Housekeeping: local `master` was 8 commits behind origin → ff-synced; merged branch deleted (origin
auto-deleted on merge). **#34 (FWD→wican-pro) confirmed done** (origin/wican-pro carries `poll_log` via
firmware PR #6) → task marked complete. Created **`.claude/plans/wican-firmware-integration-goal.md`** —
execution driver for #35/#36/#37 (still uncommitted).

## 🗂️ Housekeeping (Jun 25, 2026)

- **Larger read/transfer blocks** investigation moved to GitHub: **nc-flash#77** (it's a host/ECU-protocol
  question — the gateway only relays frames; the ECU rejects ReadMemoryByAddress > 0x400 w/ NRC 0x31).
  Was internal task #23 (deleted).
- **Firmware fast-read protocol — SHELVED** (was task #24, deleted). Decision: stop chasing the ~60 s
  firmware optimisation; current ~214 s = Tactrix parity, floor is ECU response-pending. Host `fast_read()`
  client stays in mainline (experimental/opt-in, documented, requires the firmware fork). Preserved entry
  point: branch **`shelf/firmware-fast-read`** (@ ce2bbff, pushed). ESP32 firmware remains on the fork's
  `feature/fast-rom-read` branch.
- **`.claude/settings.local.json` untracked + gitignored.** Investigated "ignore all of `.claude/`" → bad
  pattern here: `.claude/` holds shared hooks (the CHANGELOG-guard), commands, plans, and `notes.md` that
  the workflow needs. Only the machine-local `settings.local.json` should be ignored (standard CC pattern).

## ⏳ WiCAN Scan progress dialog + Cancel (Jun 25, 2026)

User asked: add "Scanning for device…" with a timer + Cancel; is there a max timeout?
Answer surfaced: **yes — `wican_discovery.DEFAULT_TIMEOUT_S` (4 s) hard-bounds the scan**
(`done.wait(timeout_s)`); it can never run forever.

- **Settings Scan went off-thread.** New `_WiCANScanWorker(QObject)` runs `discover()` in a
  `QThread`; a `QProgressDialog` shows **"Scanning for WiCAN adapters… Ns (up to 4s)"** with a
  100 ms `QTimer` ticker (determinate bar capped at max-1 so it never auto-closes early). Worker
  self-disposes via `finished/error → deleteLater`; result returns as a signal arg on the GUI thread.
  Synchronous picker/identity-staging moved to `_present_scan_results` (unchanged logic, unit-testable).
- **Cancel plumbing (headless, stays stdlib-only):** `discover()/_browse()` gained `cancel_event`;
  new `_wait_for_browse()` polls early-exit + cancel on a monotonic deadline. **No-cancel path is
  byte-for-byte `done.wait(timeout_s)`** — connect-time resolve untouched. Cancel returns the worker
  sub-second.
- **Adversarial review (Workflow, 4 lenses → verify): 4 real findings fixed, ~20 false positives
  dismissed** (mostly reviewers misreading Qt cross-thread `deleteLater`; verifiers refuted correctly).
  Fixes: (A) `done()` marks `_scan_cancelled` on close so a late `finished` can't pop a picker after
  the dialog is gone; (B) slots drop stale/duplicate signals when no scan is active; (C) `_on_scan_cancel`
  stops the ticker + `_on_scan_tick` guards on cancelled so it can't overwrite "Cancelling…";
  (D) orchestration test now asserts full signal wiring.
- **⚠️ Caught in real use → fixed:** the app aborted with `QThread: Destroyed while thread is still
  running` right after a scan found the adapter. Root cause: the worker self-disposal design dropped the
  Python refs to a still-running `QThread` in `_teardown_scan`, so PySide6 GC'd the wrapper mid-run. **Fix:**
  `_teardown_scan` now *owns* the lifecycle — captures thread/worker, then `quit()`+`wait()`+`deleteLater`
  via `_cleanup_scan_thread` (deferred `QTimer.singleShot` on the finished path; **synchronous** on dialog
  close via `done(blocking=True)`) — mirrors `ecu_window`'s proven pattern. **Why the mocked tests missed it:**
  the orchestration test patched `QThread`, so no real thread ran. Added **real-`QThread` E2E tests**
  (`TestScanRealThread`) that install a `qInstallMessageHandler` and assert no destroyed-while-running
  warning; verified they *fail* (process abort, exit 9) on the buggy version. **Lesson: thread-lifecycle
  needs a real-thread test, not a mock.**
- **Validation:** full suite **1436 passed / 12 skip**; black + CI flake8 gate clean. **Live E2E** on the
  deployed adapter: normal scan 4.05 s; pre-set cancel **0.13 s**; mid-scan cancel @0.4 s → **0.45 s**;
  **full GUI scan flow (real dialog + real QThread + real mDNS) → host filled `192.168.1.169`, 0 Qt
  warnings.** **Not committed.**

## 📡 WiCAN mDNS auto-discovery — no hardcoded IP (Jun 24, 2026)

User: the WiCAN IP is hardcoded/typed; add auto-discovery. Firmware already advertises
`_wican._tcp` mDNS (firmware `wc_mdns.c`). **Confirmed live** on the deployed adapter @
`192.168.1.169` → `WiCAN-WebServer._wican._tcp.local.`, TXT `device_id=dcb4d91511b9`,
`mac=DC:B4:D9:15:11:B8` (firmware/hardware TXT came back **empty** → treat optional).

- **New module `src/ecu/wican_discovery.py`** — lazy `zeroconf` import (headless modules stay
  stdlib-only; app runs without zeroconf, degrades to manual IP). `WiCANDevice` dataclass,
  `discover()` (full browse, dedup by stable_id→host), `resolve_host_for_device_id()` (early-exit
  via `threading.Event` + `stop_when` snapshot → sub-second when online). mDNS advertises **port 80
  (HTTP)**, NOT the 35000 SLCAN port — discovery fills the **host IP only**.
- **Settings**: `get/set_wican_device_id`. **Settings ▸ ECU ▸ WiCAN** "Scan…" button → picker →
  fills host + stages device_id (persisted on apply; manual edit/`textEdited` clears it).
- **Connect-time re-resolve** (`ecu_window._resolve_wican_host`): opt-in (only when device_id stored),
  bounded ≤3 s, fail-safe (any error → stored host), caches fresh IP. Survives DHCP changes.
- **Adversarial review hardening:** (1) ambiguous identity (same id at >1 IP) → return None / fall back
  (brick-safety, never guess which ECU); (2) zeroconf shared state lock-guarded + snapshot (no listener
  race). Kept connect-resolve **synchronous** on purpose (the SLCAN reboot already blocks ~6 s).
- **Decisions:** synchronous (not QThread) for consistency w/ existing WiCAN connect; 3 s timeout kept
  (early-exit covers happy path); manual edit always wins over stored identity.
- **Tests:** `test_ecu_wican_discovery.py` (+ambiguity/dedup/early-exit), `test_ecu_window_wican_resolve.py`
  (6 connect branches), `test_settings_dialog_wican_scan.py` (scan flow + identity lifecycle),
  `test_settings_ecu_adapter.py` (device-id). **Full suite 1406 passed / 12 skip; black + CI flake8 gate clean.**
  **Live E2E**: `discover()` found the device in 4.0 s; connect-resolve early-exit **0.33 s**. New dep
  `zeroconf` in requirements.txt. **Not committed.**

## 🏷️ WiCAN staged SD file named after the ROM (Jun 24, 2026)

User: timestamp-only staged names (`<CAL_ID>_<YYYYMMDD>-<HHMM>.bin`) say nothing about the tune →
name the SD file after the ROM shown in NC Flash, `<display-name>_<YYYYMMDD>_<HHMM>.bin`.
Must be robust to spaces + accents (éà).

- **Constraint (why not verbatim):** the staged name is used raw in 3 ASCII-only hops — FAT name,
  `/upload/sd/<name>` HTTP path, and firmware `W<mode><name>\r` (`.encode("ascii")`). Non-ASCII →
  `UnicodeEncodeError` (flash never starts); a space could truncate the firmware command → wrong/missing
  file. So preserving accents/spaces verbatim is a firmware-change, brick-path issue — declined.
- **Impl:** new `_sanitize_filename_stem` in `wican_sd_package.py` — basename only, drop trailing ext,
  **transliterate accents via NFKD** (é→e, à→a; readable, not `_`), spaces/unsafe→`_`, collapse `__` and
  **`..`** (upload guard rejects `..`) but KEEP a single `.` (so `12.5` survives), cap 64, fallback `ecu_rom`.
  Pure-ASCII, no spaces/separators guaranteed. New `source_name` param threaded `build_flash_package` ←
  `WiCANSdFlasher` ← `_build_flash_driver` ← `_start_flash` ← `_on_flash_current`/`_on_full_flash`
  (`rom_path.name`). Manifest `rom_id` identity UNCHANGED; `source_name` only names the staged file
  (falls back to cal-ID label when blank). Format separator now `_` (was `-`): `YYYYMMDD_HHMM`.
- **Example (user-confirmed):** `Testé AFR à 12.5.bin` → `Teste_AFR_a_12.5_20260624_1039.bin`.
- **Tests:** `test_ecu_wican_sd_package.py` (transliteration/space/`..`/length/empty + source_name drives
  name / blank-falls-back / new `_HHMM` format), `test_ecu_wican_sd_flash.py::TestSourceName`,
  `test_ecu_window_flash_driver.py` (UI forwards `rom_path.name`). 83 passed / 1 skip; black clean.
  **No change to staged bytes / manifest plan / flash sequence — SD filename only.** Not committed.

## 🐞 DTC toggle regression FIXED (Jun 24, 2026) — pre-existing, NOT from WiCAN work

User flipped a DTC Activation Flags toggle (1-D toggle-category table, e.g. `P0222`/`P0122`) and hit
`AttributeError: 'str' object has no attribute 'address'` in both the modification tracker
(`table_viewer._on_cell_changed_track_modifications`) and the undo recorder
(`main._on_table_cell_changed → table_undo_manager.record_cell_change`).

- **Root cause:** `table_viewer._on_toggle_changed` emitted `cell_changed` with `current_table.address`
  (a `str`), but every other edit path AND all three `cell_changed` consumers pass/expect the **Table object**
  (they read `.address`/`.name` themselves). Pre-existing since the toggle feature shipped — commit `05ebbeb`,
  **2026-02-07** (verified via `git log -L`); **none of this session's WiCAN files touched table_viewer/main/undo**.
- **Fix:** emit `self._ctx.current_table` (matches `editing.py`'s normal cell-edit emit). One-line change.
- **Test:** `test_table_viewer_window.py::TestSignalForwarding::test_toggle_emits_table_object_not_address`
  drives the **real** `_on_toggle_changed` handler (the prior signal tests emitted manually → never caught this).
  Fails against the old `.address` emit. 15/15 in that file pass; black clean. (2 `test_paste_*` failures in the
  combined run are pre-existing Windows clipboard-contention flakes — pass in isolation.)
- **Not committed** — awaiting user / land-the-plane.

## ✅ HARDWARE-VALIDATED + /simplify (Jun 23, 2026 — late evening)

**User re-tested all Flash operations via the UI → green** (the bound-method threading fix holds; no more setParent/endPaint).

**Live-device bench validation (WiCAN @ 192.168.1.169, MX-5 NC ECU), all non-destructive:**
- `wican_fw_ping` → link alive, **NCFRv5**.
- `wican_bench_ecu --read-dtc` → auth + security + `read_dtc_status`/`read_dtc_count` clean (15 DTCs) — exercises the `quiet_nrcs` change on real HW, no errors.
- `wican_bench_read` full 1 MB via `FlashManager.read_rom` (the changed `_read_block_with_retry`) → **27 block(s) re-requested after dropped frames, ALL recovered**, byte-for-byte identical to the post-flash oracle (`GATE: PASS`), 341 s, no WARNING/ERROR. **Directly proves the read-retry+backoff fix on real hardware** — 27 drops/1024 blocks is exactly what aborted the read under the old 4-retry cap.

**/simplify pass (4 agents: reuse/simplification/efficiency/altitude):** 1 fix applied — `_ConsoleScopeFilter` now precomputes the dotted prefix pairs in `__init__` (no per-record `p + "."` alloc on the log hot path). All other findings skipped as preference/YAGNI/wrong-for-use-case (notably: rejected "capture stack only on QtFatalMsg" — the crash precursor was a WARNING, so that would have missed it). Full suite stayed green (1341 passed).

**Still NOT committed** — awaiting user / land-the-plane.

## 🔧 POST-ENABLE HARDWARE FIXES (Jun 23, 2026 — evening) — read robustness, clean state log, crash diagnostics

User drove the now-enabled UI write path on the live ECU and hit two issues; both addressed (host-only, unit-tested):

1. **READ couldn't finish** — a 1 MB WiCAN read aborted at block ~957/1024 (`0x0EF400`) after only **4** back-to-back
   ISO-TP consecutive-frame N_Cr timeouts at one offset → ~5 min of work lost to a sub-second link stall.
   Fix (`flash_manager.py`): `READ_BLOCK_RETRIES` **4 → 8** + a short growing backoff between retries
   (`READ_BLOCK_RETRY_BACKOFF_S=0.2 × attempt`, cap `1.0 s`; only the failing block waits, never after the last
   attempt). Reads are idempotent so this can't corrupt; clean blocks still return on attempt 1. Tests in
   `TestReadBlockRetry` (backoff grows / never-after-last / recovery still flushes). **Write path untouched** (no resend).
2. **App closed unexpectedly after a dynamic flash — ROOT-CAUSED & FIXED via the diagnostic.** The op itself
   SUCCEEDED (flash + RAM scan both completed on the ECU); the crash was purely post-op UI. Added Qt diagnostics
   (`src/utils/qt_diagnostics.py` + `install_qt_diagnostics()` in `main.py`, routes Qt warnings to the `qt` logger +
   dumps a Python stack for crash-trigger substrings + arms `faulthandler`). **The captured stack pinned it exactly:**
   `_on_flash_finished → _btn_done.setVisible(True)` running on the WORKER thread. **Cause:** `_start_flash` connected
   `worker.finished`/`worker.error` to **bare lambdas** with `Qt.QueuedConnection` — a bare lambda has no receiver
   QObject, so Qt ran the slot in the *sender (worker)* thread, and every GUI mutation in `_on_flash_finished`
   (setVisible / QMessageBox / repaint) fired off the GUI thread → "Cannot set parent … different thread" +
   intermittent `endPaint` paint crash. (`progress → self._on_flash_progress` never crashed because it's a bound
   method = GUI-thread receiver.) **Fix:** connect bound methods `_on_worker_finished`/`_on_worker_error` (read stored
   `_flash_thread`/`_flash_worker`) so the queued slot lands on the GUI thread. Tests:
   `test_ecu_window_flash_driver.py::TestWorkerFinishedHandlers`. **→ NEXT: user re-test a dynamic flash; the
   `setParent`/`endPaint` warnings should be gone.** Diagnostics stay in (cheap, catches any future cross-thread bug).
3. **Spurious log ERROR** — `WiCANSdFlasher._authenticate_ecu` called `_authenticate()` from `IDLE`, logging
   "Invalid state transition blocked: idle → authenticating" on every SD flash. Now calls `_connect()` first
   (borrowed → Tester Present + `IDLE→CONNECTING`), like the read path. Clean log + liveness check. No flash behaviour change.

Suite for touched areas green (flash_manager / wican_sd_flash / window_flash_driver / wican_flash / qt_diagnostics:
78 passed, 2 pre-existing secure-module skips). black clean. **Not committed** (awaiting user / land-the-plane).

## ✅ OPTION B COMPLETE (Jun 23, 2026 PM) — WiCAN SD flash ENABLED at the UI, verify decoupled, key-cycle UX

**RESOLVED: user ignition-cycled → "ECU rebooted, it seems happy."** Post-cycle full read-back (1024 blocks,
214s) confirms the restored LFDJEA tune is **byte-perfect**: the only diffs vs the pristine read are (a) the
ECU flash counter @0xFFB00 (8B, masked) and (b) the 4-byte main checksum @0x0FF73C, which is
`correct_rom_checksums` itself rewriting factory `53f37de9`→our `f6871842` (ECU runs fine on our value). So
the flash mechanism AND the verify-compare logic are both proven correct.

**Root cause of the PM "verify failed" — NOT a byte mismatch, a SESSION/timing issue:** after the firmware
`ECUReset` the NC ECU sits in its **bootloader** (RMBA 0x23 + OBD 0x01 → NRC 0x11) until a **physical ignition
cycle** boots the app — the host can't trigger that, so an **inline read-back verify is impossible**. The
J2534 path proves the design: `FlashManager.flash_rom` ALSO does **no inline read-back** — per-block positive
UDS responses + TransferExit ARE the integrity proof. Both paths end with the **identical** `ECUReset 0x11
0x01` (J2534 `RESET_HARD`; firmware `fw_ecu_reset {0x11,0x01}`), so the key-cycle requirement is an ECU
property, adapter-independent (user confirmed: Tactrix flashes need a key cycle too). See memory
`project_wican_post_flash_bootloader`.

**Phase 6 SHIPPED (host/UI, all unit-tested, suite 1316 passed / 2 known clipboard-env fails):**
- `WICAN_WRITE_ENABLED = True` — WiCAN `flash`/`dynamic_flash` route to `WiCANSdFlasher` (behind NCFRv5 rev-gate
  + link/battery + CRC32 digest gates); gate still works as a kill-switch (`test_ecu_window_flash_driver`).
- `WiCANSdFlasher.flash_rom`/`dynamic_flash`: `verify` now defaults **OFF** — write completes on firmware
  `NCFWDONE` (J2534 parity). `_verify_readback` kept as an explicit, **post-ignition-cycle** opt-in; its
  not-readable error now guides the user to cycle the key (bench: `tools/wican_fastread_verify.py`).
- UI: both adapters now show a "**Flash written & confirmed — cycle the ignition (key OFF ~10s, then ON)**"
  completion dialog (removed the misleading J2534 "ECU is rebooting"+auto-reconnect; reconnect happens on Done
  after the user cycles). `_confirm_wican_flash` rewritten to describe the SD-staged flow + post-flash cycle.

**Earlier PM host fixes (still in place):** `_authenticate_ecu` (firmware fastwrite needs a host prog session);
flash-counter mask in `_verify`/`_verify_readback` (`FLASH_COUNTER_OFFSET/SIZE`); firmware `/upload/sd` path
buffers `FILE_PATH_MAX`→`160` (long `.part` name overflow). Firmware live: NCFRv5, `NCFW_ALLOW_LIVE=1`.

**Open (optional follow-ups, not blockers):** one-click in-UI "Verify" (read-back + auto-compare after the
cycle) instead of manual Read-ROM compare; commit/land the host + firmware work (user-gated). **WiCAN device
note:** it wedges on :35000 after an OBD power-cycle (HTTP :80 stays up, reports `protocol=slcan`); a POST to
`/system_reboot` clears it cleanly — no physical unplug needed.

## ✅ WiCAN WRITE WORKS (Jun 23, 2026) — Option B live flash SUCCEEDED + ECU recovered over CAN (AM)

**🎉 MILESTONE: WiCAN SD-staged WRITE is PROVEN end-to-end on the live MX-5 NC ECU.** The first live flash
failed at SBL block 6 (`FWERR st=11 nrc=FF`) and soft-bricked the ECU (recoverable). The firmware fix
(longer ISO-TP timeouts: FC 250→2000ms, resp 250→5000ms, MAX_PENDING 16→24 + granular FWSUB_* codes) fixed
it: the **retry flashed to completion** (NCFWDONE, 1022/1022 blocks, ECU reset `0x51 0x01`), and the
**read-back is byte-for-byte the source except the 7-byte ECU-managed flash counter @0xFFB00** → flash GOOD,
**ECU RECOVERED** (re-flashed its own checksum-corrected LFDJEA ROM; un-bricked, runs + reads normally).
Block 6 was a slow-response timing issue (the ECU goes quiet during SBL-completion/erase longer than the old
250ms window). Tasks #20 + #31 DONE.

**Two production-path bugs found by reasoning through the UI flow + FIXED (host, unit-tested):**
1. `WiCANSdFlasher` never authenticated the ECU before `fast_write` (my manual flash auth'd separately) →
   added `_authenticate_ecu()` in `_trigger_firmware_flash` AFTER the rev-gate (no ECU contact on old fw).
2. `WiCANFlasher._verify` didn't exclude the flash counter → would FAIL a perfect flash on the 0xFFB00 diff.
   Now masks `FLASH_COUNTER_OFFSET=0xFFB00`/`SIZE=8` (new constants) on both sides, matching the host CRC.
   Tests: `test_verify_tolerates_flash_counter`. Full suite was 1313; +these.

**REMAINING (needs user OK — auto-classifier blocked an extra flash as beyond "recover"):**
- End-to-end validation of `WiCANSdFlasher.flash_rom` (the actual UI code path with the 2 fixes) requires ONE
  more flash (re-flash the same good ROM). The recovery used the manual auth+WL path, NOT the production
  WiCANSdFlasher path — so the integration (gates→package→upload→auth→fast_write→verify-with-mask) is
  component-validated + unit-tested but not yet run as a whole on HW.
- THEN flip `WICAN_WRITE_ENABLED=True` (Phase 6 final) to enable the UI write path (behind NCFRv5 rev-gate +
  experimental warning + link/battery/digest gates + read-back-ON). Do NOT enable before the e2e flash passes.

**(prior) STATUS: ECU RECOVERED (user reflashed via Tactrix, confirmed working). `/goal` is executing the
Option B plan with the user's blessing to test write features (they'll inspect the ECU if anything bricks).**
- **SD upload metered:** ~1 s/MB over WiFi (`tools/wican_upload_meter.py`) → Option B budget ≈ 1 s + ~55 s flash.
- **Phases 0, 2-host, 3-host: DONE** (host-side, all unit-tested, suite green 1305 passed). See Recent Completed
  Work below. Make-safe gate (`WICAN_WRITE_ENABLED=False`) + the full host pipeline: `wican_sd_package.py`
  (staged image + manifest, byte-identical to J2534 prep), `wican_sd_upload.py` (verified multipart upload),
  `wican_sd_flash.py` (`WiCANSdFlasher` orchestrator, firmware trigger rev-gated to NCFRv5+). Wired behind
  `_build_flash_driver` (swap WiCANFlasher→WiCANSdFlasher) under the make-safe gate → zero live-flash risk.
- **⚠️ WiCAN DEVICE WEDGED (blocks all hardware phases).** As of this session the WiCAN @ 192.168.1.169 accepts
  TCP on :80 and :35000 but BOTH the HTTP config server and the SLCAN server immediately close every connection
  (persisted across a 12s wait; my very first probe already failed, so it predates this session). Only read-only
  probes were run. This is the ADAPTER, not the ECU (ECU untouched). HTTP `/system_reboot` is unreachable, so it
  needs a **physical power cycle** by the user. Until then: Phase 1 OTA+test, Phase 2 firmware/bench, Phase 4
  dry-run, Phase 5 live flash are all blocked.
- **Firmware Phases 1 + 2: WRITTEN + BUILD-VERIFIED (HW-test pending, blocked on device).** On branch
  `feature/option-b-sd-write` in `nc-flash-wican-fw` (UNCOMMITTED working tree):
  - **Phase 1 teardown fix** (`main/ncflash_fastread.c`): bounded `tx_send()` (2 s, replaces every
    `portMAX_DELAY` `xQueueSend` — the wedge cause), single `goto cleanup` teardown always resumes
    `can_rx_task` (via `was_suspended`), drains CAN RX, `twai_read_alerts` to clear alerts, and flushes the
    TX queue ONLY on `host_gone` (never on success — would truncate a good read). Re-entry guard
    `s_fastop_busy`. Read happy-path bytes are unchanged. Version marker stays NCFRv4 (read wire identical).
  - **Phase 2 `/upload/sd/*` endpoint** (`main/config_server.c`): wildcard POST handler → `/sdcard/roms/<name>`,
    `.part`-temp + atomic rename, filename guard (no `..`/sep/ctrl, require ext), 4 MB cap, returns
    `{bytes_written, crc32}`. **CRC is a hand-rolled zlib-compatible CRC-32 — VERIFIED byte-for-byte vs
    Python `zlib.crc32` (incl. chunked) so host upload verification works.** Non-destructive (no CAN/BLE
    disable, no reboot).
  - **Both build clean** with ESP-IDF v5.5.3 (`idf.py build` OK, 32% free). **Build env gotcha:** must
    `$env:IDF_PYTHON_ENV_PATH="C:\Users\dufre\.espressif\python_env\idf5.5_py3.10_env"` then
    `. C:\esp\esp-idf-v5.5.3\export.ps1` (NOT `C:\esp\esp-idf` — that's a stale v5.1; default py is 3.14 so
    export mis-derives the venv name).
- **DEPLOYED + HARDWARE-VALIDATED (device recovered on its own; user approved "rebuild NCFRv4 + OTA now"):**
  - Built the exact deployed NCFRv4 from source (git-stash the WIP → clean tree @ 84445a2 → build →
    `ncfrv4-recovery.bin`, confirmed it carries the NCFRv4 marker, unlike the old pre-fast-read
    `rollback_deployed_wican-pro.bin`) as the recovery image. Restored WIP, rebuilt Phase 1+2.
  - **OTA-flashed the Phase 1+2 build** (`POST /upload/ota.bin`, 3526352 B, HTTP 303 in 22.6s); device
    rebooted cleanly into it (HTTP 200, NCFRv4 read marker intact).
  - **Phase 2 validated:** `/upload/sd/*` works — uploaded the full ~1.03 MB staged LF9VEB image; device
    CRC `0xE08DBFC3` == host CRC exactly; manifest sidecar uploaded; traversal rejected. Staged ROM now on
    the SD card at `/sdcard/roms/`.
  - **Phase 1 validated:** full authenticated fast-read = 214.7s (parity, unchanged); TWO reads byte-for-byte
    identical → read byte-perfect (the 1 checksum mismatch @0xFF73C is the ECU's real reflashed LFDJEA cal,
    not a read error). CAN-wedge fix proven: after a host-disconnect-mid-read, a full re-auth on the SAME
    firmware session (no reboot) succeeded in 0.3s → `can_rx_task` resumes, CAN not wedged. (Test gotcha:
    polling version_ping during the wait re-feeds the firmware TX socket and confuses timing/tester_present;
    use a quiet wait + full re-auth as the clean check.)
- **Phase 4 `ncflash_fastwrite` (dry-run): DONE + HARDWARE-VALIDATED.** New `main/ncflash_fastwrite.c` (+.h,
  wired into main.c dispatch after fastread, added to CMakeLists). ISO-TP *sender* (FF+CF, honor ECU FC,
  lock-step `0x76` ACK, NO resend/NO counter), SD reader, manifest parse (cJSON), pre-erase CRC32 digest
  gate, progress markers via bounded `tx_send` + the same clean-teardown as fastread. **HARD SAFETY GATE
  `NCFW_ALLOW_LIVE` (=0 in this build): mode 'L' is REFUSED → the deployed firmware is physically incapable
  of an ECU write.** Command: `W` + mode('D'/'L') + filename + CR; reads `/sdcard/roms/<name>` + `<stem>.json`.
  Built + OTA'd (NCFRv4 read marker kept — host rev-gate stays closed). **Validated on HW (ECU never
  touched):** dry-run happy path NCFWSYNC→1022/1022 blocks→NCFWDONE; digest gate hard-blocks a CRC mismatch
  (FWERR st=5, no progress); live 'L' refused (FWERR st=7); missing file → st=2. The firmware CRC32 matches
  the host `zlib.crc32` (proven in Phase 2). Recovery image `ncfrv4-recovery.bin` still valid.
- **⚠️ Phase 5 LIVE FLASH ATTEMPTED — ECU SOFT-BRICKED (re-flashable over CAN), AWAITING USER DIRECTION.**
  Built NCFRv5 live build (`NCFW_ALLOW_LIVE=1`), OTA'd, re-flashed the ECU's OWN ROM (LFDJEA, checksum-
  corrected — 4-byte diff). Host auth + `WL` trigger: **RequestDownload accepted (0x74) + 5 SBL blocks ACKed
  (0x76 each)**, then FAILED on the 6th/final SBL block: `FWERR a=101400 st=11 nrc=FF` (bare recv-timeout —
  ECU went silent). **This VALIDATES the auth-handoff (host-auth carries to firmware bus ownership) AND the
  ISO-TP sender (multi-frame RequestDownload + 5 lock-step blocks) — furthest ever (host-driven never passed
  SBL block 1).** ECU state: no default-session tester_present, BUT re-auth (prog session + security) SUCCEEDS
  → **soft-bricked but RE-FLASHABLE over CAN** (a completed flash recovers it; flash intact, pre-erase). User
  DENIED the recovery-options question → HOLDING, no further ECU contact without explicit go.
- **FIX STAGED (built, NOT OTA'd):** improved `ncflash_fastwrite.c` ISO-TP diagnostics — granular FWSUB_*
  nrc codes (FF-send/FC-timeout/FC-bad/CF-send/ACK-timeout/ACK-pci/ACK-sid, 0xE1-0xE7) so a retry says exactly
  WHERE block 6 dies, + realistic timeouts (FC 250→2000ms, resp 250→5000ms, MAX_PENDING 16→24 ≈ host's 60s
  0x78 budget — my 250ms was way tighter than the host flash's TIMEOUT_RESPONSE_PENDING_MAX=60000). Builds
  clean. **Hypothesis:** block 6 completes the SBL (0x1800) → ECU jumps to SBL / starts erase → silent longer
  than my 250ms first-response timeout. The longer timeouts + granular codes will confirm on the next retry.
- **Phase 6 HOST GLUE: DONE + tested (while holding the ECU).** `WiCANTransport.fast_write(name, mode, progress_cb)`
  sends `W<mode><name>\r`, resyncs on NCFWSYNC, parses NCFWPROG/NCFWDONE/FWERR (mirror of fast_read's reader:
  resync-past-CAN, FWERR surfacing via `_fwerr_suffix`, stall + peer-close detection, NO host abort).
  `WiCANSdFlasher._trigger_firmware_flash` now drives it (mode 'L') → FlashProgress 35→90% band. Tests:
  `test_ecu_wican_fast_write.py` (socketpair replay, 8) + updated `test_ecu_wican_sd_flash.py`. Full suite
  **1313 passed**. **`WICAN_WRITE_ENABLED` stays False** — the UI write path is NOT enabled (no live-flash from
  the app) until a flash is proven. The ONLY remaining Phase 6 step is flipping that flag, AFTER a successful
  live flash + read-back.
- **NEXT once user approves ECU recovery:** OTA the staged firmware fix (granular FWSUB_* codes + longer
  timeouts) → retry the full flash over CAN (restart-from-scratch; ECU stays re-flashable across attempts).
  If NCFWDONE → mandatory read-back compare → then flip `WICAN_WRITE_ENABLED=True` to finish Phase 6.
  **Do NOT enable the UI write path until a flash actually completes.**
- **(superseded) Phase 5 plan:** flip `NCFW_ALLOW_LIVE=1` + bump marker
  NCFRv5, rebuild+OTA. **Resolve the auth-handoff unknown FIRST** (OPEN DECISION #1: does the host-authenticated
  programming session carry over when the firmware takes the bus, or is a seed-relay needed? watch the gap vs
  the ECU S3 ~5s timeout → may need interleaved TesterPresent). Then a real flash: lock-step `0x76` ACKs, FC
  handling, no-resend abort, NCFWDONE, mandatory read-back compare, interrupted-flash→clean-abort→restart.
  **RISK:** a partial live flash soft-bricks the ECU (stuck in programming mode; needs the user's Tactrix
  reflash, NOT a power cycle — memory `project_wican_write_bricks_on_interrupt`). Do on a recoverable ECU
  with the user present + Tactrix ready. THEN Phase 6 enable (`WICAN_WRITE_ENABLED=True`, host
  `WiCANTransport.fast_write()` parsing NCFWSYNC/NCFWPROG/NCFWDONE/FWERR → FlashProgress).
- **Build env:** `$env:IDF_PYTHON_ENV_PATH="C:\Users\dufre\.espressif\python_env\idf5.5_py3.10_env"` then
  `. C:\esp\esp-idf-v5.5.3\export.ps1` then `idf.py build` (NOT `C:\esp\esp-idf` = stale v5.1).
- **UNCOMMITTED (no commit without user ok):** firmware branch `feature/option-b-sd-write` (ncflash_fastread.c
  + config_server.c + ncfrv4-recovery.bin); host repo (5 new modules + tests + CHANGELOG + this file). The
  deployed firmware is running but its SOURCE is only in the working tree — recommend committing for
  traceability (hospital-critical) when the user is ready.

**PRIOR HISTORY (Jun 21) — root cause that drove the pivot:** the host-driven, block-by-block WiCAN write
soft-bricks the ECU on a mid-flash drop (NC ECU FC = BS=0/STmin=0 → unpaced CF burst overruns the gateway;
interrupted programming session needs a Tactrix reflash, not a power cycle). Block-level `tx_stmin` pacing fix
exists + is hardware-validated at the block level but is too slow (~8–10 min) and brick-prone for full flash →
REJECTED as the path. Option B (SD-staged, firmware-driven local-CAN flash) is the answer. See memory
`project_wican_write_bricks_on_interrupt`, `WICAN_PART_C_FINDINGS.md` §3.

**WHAT HAPPENED THIS SESSION:**
1. User reported the GUI FULL FLASH over WiCAN failed: auth/security/`RequestDownload` OK, then the first
   SBL `TransferData` (SID 0x36) timed out at 60 s. READ/RAM/DTC all work over the same link.
2. **Root cause (HARDWARE-CONFIRMED via `tools/wican_flash_diag.py`):** the NC ECU's ISO-TP Flow Control
   advertises **BS=0 / STmin=0** ("send all ~146 Consecutive Frames back-to-back"). The unpaced outbound
   CF burst overruns the WiCAN gateway's TCP→CAN buffer → a frame drops *inside the gateway* → ECU never
   completes reassembly → no `0x76` → 60 s timeout. (Mirror of the receive-side overflow `rx_stmin` already
   guards; reads work because the ECU is the *sender* + host has N_Cr fast-fail + idempotent retry. Write
   has neither — no mid-stream resend by design.) Verified by a 3-agent adversarial workflow (all confirmed).
3. **Block-level FIX implemented + unit-tested + HARDWARE-VALIDATED (but UNCOMMITTED):** outbound CF pacing
   floor. With a 3 ms floor, a paced 147-frame SBL block **ACKed in 638 ms** (unpaced failed). Files:
   - `src/ecu/isotp.py` — `IsoTpSession(tx_stmin=…)`; pacing = `max(peer_stmin, floor)` via
     `_pace_consecutive_frame` (replaced `_sleep_stmin`). Default `tx_stmin=0` → J2534/reads byte-identical.
   - `src/ecu/wican_transport.py` — `DEFAULT_TX_STMIN=3`, plumbed into the session.
   - `tools/wican_flash_diag.py` — NEW instrumented diagnostic (captures the ECU's FC; `--sbl-blocks`,
     `--tx-stmin`, `--commit`/`--yes` for a real flash). **SAFETY BUG: it leaves the ECU mid-download →
     this is what soft-bricked the ECU. Before any reuse, add a clean-exit (ecu_reset/transfer_exit) — or
     do NOT re-run it on the live ECU.**
   - Tests: `tests/test_ecu_isotp.py::TestOutboundTxStminFloor` (4) + `test_ecu_wican_transport.py` (1 wiring).
     `tests/test_ecu_isotp.py` + `test_ecu_wican_transport.py` = **98 passed**; flash/protocol = **80 passed**.
     black clean. CHANGELOG updated (Fixed). **NOT committed** (ECU bricked / WIP / user hasn't said land).

**KEY DECISION — PIVOT WiCAN WRITE TO OPTION B (SD-staged firmware write):**
Host-driven write (the pacing fix) is a **slow, brick-prone stopgap** — REJECTED as the long-term path:
  - **Too slow:** 3 ms × ~150k outbound frames ≈ **8–10 min** vs J2534 **~19 KB/s ≈ 55 s** (user's hard req).
  - **Brick-prone:** any single residual dropped frame over ~150k frames aborts (no resend); and every
    failed attempt = a Tactrix reflash (not safely interruptible).
**Option B = the real answer (speed AND brick-safety):** bulk-upload ROM → WiCAN SD over HTTP/FTP (reliable
TCP, the only WiFi step, verifiable BEFORE touching the ECU), then firmware `ncflash_fastwrite` (mirror of
the proven `ncflash_fastread`) runs the program sequence locally over CAN at line rate ≈ J2534 speed, no
WiFi in the flash loop. User maintains the fast-read firmware fork → feasible. Docs: `WICAN_PART_C_FINDINGS.md`
§3, `WICAN_TRANSPORT.md` §6.

**OPEN QUESTION TO MEASURE TOMORROW (gates Option B's total time):** how long to upload 1 MB to the WiCAN SD?
**NEVER METERED.** All our throughput numbers (fast-read ~214 s, SLCAN ~1.4 KB/s) are **ECU-limited**, not
raw WiFi. The docs only cite the SD card's *own* write speed (~10–50 MB/s SDMMC), not an end-to-end WiFi
upload. ESP32 WiFi TCP is typically a few MB/s → est. 1 MB in ~1–3 s, but UNVERIFIED. Easy to measure: time
an HTTP multipart POST (or FTP `STOR`) of a 1 MB file to `/sdcard`. (Closest proxy done = OTA firmware
upload, which was never timed.) Option B total ≈ (this upload) + ~55 s firmware flash.

**TODO NEXT SESSION:** (1) user reflashes ECU. (2) meter the SD upload. (3) decide/scope Option B
(`ncflash_fastwrite` firmware + host SD-upload + flash-trigger + host-side SHA/CRC integrity). (4) decide
whether to keep the host-driven pacing fix as a documented emergency fallback or drop it; consider
DISABLING the host-driven WiCAN flash in the UI until Option B exists (it bricks on failure). (5) commit
the pacing fix + diag tool only once we've decided + the diag's clean-exit safety bug is fixed.
Tasks: **#20 in_progress** (WRITE), #24 (shelve fast-read?), #23 (larger read/transfer blocks).

## Recent Completed Work (Jun 23, 2026) - Option B Phases 2-host + 3-host: SD-staged flash pipeline (host)
- **`src/ecu/wican_sd_package.py`** — `build_flash_package(rom, flash_type, archive_data, rom_id, when)` → a
  self-checked `FlashPackage(image, manifest)`. Image = `[checksum-corrected ROM (1MB)] ++ [SBL (0x1800)]`.
  Replicates `FlashManager._flash_rom_inner` host prep EXACTLY (validate→gen→correct+verify-zero-residual→
  flash_start_index full/dynamic→get_sbl_data→program slice→assemble→SHA256/CRC32), cross-checked in tests vs an
  independent recompute. Manifest freezes the firmware contract (`MANIFEST_VERSION=1`): download_addr/size,
  block_size, flash_start_index, sbl_offset/len, program_offset/len, image_len, image_sha256/crc32, rom_sha256,
  staged_filename `<ROM_ID>_<YYYYMMDD>-<HHMM>.bin`.
- **`src/ecu/wican_sd_upload.py`** (`WiCANSdUploader`) — stdlib multipart POST to `/upload/sd/<name>`, verifies
  device `{bytes_written, crc32}` vs host digest, refuses partial/corrupt. `upload_package`/`upload_manifest`.
- **`src/ecu/wican_sd_flash.py`** (`WiCANSdFlasher`) — package→upload→trigger orchestrator; reuses WiCANFlasher
  `_gate`/`preflight`/`_verify` by composition (no mixin); read-back verify default ON; firmware trigger
  REV-GATED via `version_ping` (`FASTWRITE_MIN_FW_REV=5`) — refuses cleanly on the current NCFRv4 (no ECU
  contact). `WiCANTransport` gained public `.host`/`.port`. Wired into `_build_flash_driver` (WiCANFlasher→
  WiCANSdFlasher) under the unchanged `WICAN_WRITE_ENABLED=False` make-safe gate.
- Tests: `test_ecu_wican_sd_package.py` (21+1skip), `test_ecu_wican_sd_upload.py` (19, in-proc HTTP server),
  `test_ecu_wican_sd_flash.py` (15). Full suite 1305 passed / 12 skipped. black clean. **NOT committed.**

## Recent Completed Work (Jun 23, 2026) - Option B Phase 0: make-safe (host-driven WiCAN write disabled)
- **Hard-disabled the host-driven WiCAN flash at the UI seam** (it soft-bricks on a mid-flash link drop;
  superseded by the SD-staged Option B path). Single source of truth: new module-level flag
  `WICAN_WRITE_ENABLED = False` in `src/ecu/wican_flash.py` (Phase 6 flips it on behind the firmware rev-gate).
- **Two enforcement points (defense-in-depth):** (1) `_build_flash_driver` (`src/ui/ecu_window.py`) — the
  single choke point all flash/read routes through — returns `None` for WiCAN `flash`/`dynamic_flash` when the
  flag is off (the testable backstop). (2) `_confirm_wican_flash` — shows a plain-language "WiCAN flash
  temporarily disabled, use a J2534 cable" dialog instead of the old experimental-risk prompt.
- **Unaffected:** WiCAN read / RAM scan / DTC read+clear; J2534 flashing (never gated by the flag).
- Tests: `tests/test_ecu_window_flash_driver.py` (8) — duck-typed fake `self`, no QApplication needed: WiCAN
  write blocked by default (no session acquired), WiCAN reads still build a `FlashManager`, J2534 flash never
  blocked, flag-on rebuilds `WiCANFlasher`. Suite for the area = 42 passed. black clean. CHANGELOG (Changed).
- **NOT committed** (WIP / user hasn't said land the plane).

## Recent Completed Work (Jun 21, 2026) - WiCAN goal 3: adapter UI + settings (BUILT, suite green)

- **Goal 3 grilled (light) + executed.** `/goal execute .claude/plans/wican-adapter-ui-goal.md`. Adapter is now selectable from the UI.
- **The seam: `ECUSession` is now adapter-aware** (`src/ecu/session.py`). One positional-back-compat `__init__` (`ECUSession(dll_path)` still works) + new `adapter_config={"kind":"wican"|"j2534", ...}`. Split into `_connect_j2534`/`_connect_wican` and `_teardown_j2534`/`_teardown_wican`. **J2534 path byte-for-byte unchanged** (existing patch targets `src.ecu.j2534.*` untouched → all old session tests pass). WiCAN: switch device→SLCAN **once per session** (guarded by `_slcan_switched`), restore the ORIGINAL protocol only on `disconnect_ecu(restore_protocol=True)`/`cleanup()` — **NOT** on `release(connection_dead=True)** nor the auto-reconnect. `disconnect_ecu` gained a `restore_protocol` param; new `progress` signal for connect-step messages; `adapter_kind`/`transport` properties.
- **Reboot-storm avoidance (key design):** the post-read auto-reconnect **reuses the same session** (`_auto_reconnect` → `session.connect_ecu()` for WiCAN) so the original protocol is never lost across recreation and the adapter isn't rebooted twice per read. `release(connection_dead=True)` tears down with `restore_protocol=False`.
- **ECU window routing** (`src/ui/ecu_window.py`): `_build_flash_driver(op)` picks J2534=`FlashManager`+`use_session`, WiCAN read/scan=`FlashManager`+`use_uds`, **WiCAN flash=`WiCANFlasher`** (gate+battery+abort-restart, no mid-stream resend). `abort` is `hasattr`-guarded (WiCANFlasher has none; writes can't abort anyway). `_confirm_wican_flash` gates writes with an *experimental/keep-ignition-ON* dialog. `_build_adapter_config` reads settings.
- **Settings** (`src/utils/settings.py` + `src/ui/settings_dialog.py`): `get/set_ecu_adapter` (j2534 default), `get/set_wican_host` (192.168.1.169), `_port` (35000), `_auto_config` (True). New **ECU ▸ Adapter** dropdown + **ECU ▸ WiCAN** page (host/port/auto-config/**Test Connection**). Added a `"text"` widget type to the registry-driven dialog (factory+load+apply). `_test_wican_connection` opens the link, runs `check_link_quality`, reports loss/p95, and restores cursor+transport+protocol on every exit path.
- **`flash_mixin._on_ecu_connect`** also builds adapter_config from settings (secondary connect path).
- **Tests:** `tests/test_ecu_session_wican.py` (8: switch-once/restore-once/reconnect-keeps-SLCAN/connect-fail-restores/acquire-no-device) + `tests/test_settings_ecu_adapter.py` (12). **Full suite 1233 passed, 11 skipped, black clean.**
- **Ultracode verification (2 rounds) found + fixed 8 real issues, incl. 2 protocol-loss bugs:** (a) `_on_connect` orphaned a stale WiCAN session without restoring → adapter stranded in SLCAN, original protocol lost; fixed by `cleanup()`-before-reassign + `cleanup()` now restores even from DISCONNECTED-but-switched. (b) bare `switch_to_slcan` never wrote the crash-recovery sidecar → a hard kill mid-session lost the protocol; fixed with `_enter_slcan_durable` (write breadcrumb BEFORE switch, prefer a recorded original) + additive `WiCANConfigurator.write_recovery` (proven `switch_to_slcan`/`restore` left untouched). Also: `processEvents` gated to WiCAN (J2534 byte-for-byte), `disconnect_ecu` refuses while BUSY (brick-safety on the seam), reverted dead-code dup in flash_mixin, progress steps now paint. Re-verified: all 8 confirmed fixed, no new bugs, **full suite 1236 passed**.
- **KNOWN LIMITATION:** WiCAN connect is **synchronous** — the first SLCAN switch / restore (~6 s reboot) briefly blocks the UI thread (`QApplication.processEvents()` paints the step labels first, but the loop is blocked during the HTTP reboot poll). Threading the connect is a deferred polish. WiCAN flash is wired but **still bench-unvalidated (task #20)**.
- **Task #23 (NEW):** investigate whether the ECU honours larger read/transfer blocks (probe 0x800/0xFFE on the live ECU; reads idempotent so safe).

## Recent Completed Work (Jun 21, 2026) - WiCAN goal 2 kickoff: ECU functions (Part A) + reboot/SD investigations

- **Three new `/goal` driver docs** under `.claude/plans/`: goal 1 (`wican-read-speed-goal.md`) marked **fully closed** (Tactrix parity); goal 2 (`wican-ecu-functions-goal.md`) — READ RAM/DTC/CLEAR DTC + **build-only** WRITE logic; goal 3 (`wican-adapter-ui-goal.md`) — adapter UI **stub** (grill-me pending after goal 2). Now executing goal 2.
- **HARDWARE IS REACHABLE from the dev machine** (WiCAN PRO @ 192.168.1.169 + live MX-5 NC ECU; firmware NCFRv4) — bench tools run directly against real hardware. See memory `project_wican_hardware_in_loop`. **READ DTC + READ RAM CONFIRMED on the live ECU (2026-06-21):** DTC returned the bench's 17 codes; RAM dumped clean 48 KB. Hardware exposed a bug — `FlashManager.scan_ram` had no per-page retry, so one dropped frame aborted the scan (died at page 53/192); FIXED to use `_read_block_with_retry` (idempotent), re-ran clean (recovered a dropped page at 0xFFFF1100). **CLEAR DTC user-authorized + confirmed: reduced 17→7 (10 cleared, 7 hard faults re-set immediately).** Part A now FULLY hardware-confirmed (all 3 functions). Remaining: Part B WRITE hardware flash (built+tested, NOT flashed — explicit go + brick risk).
- **Part A — READ RAM / READ DTC / CLEAR DTC over WiCAN (BUILT + unit-tested; reads HW-confirmed, clear pending).** New `tools/wican_bench_ecu.py` drives the *existing transport-agnostic* `FlashManager` seam (`scan_ram`/`read_dtcs`/`clear_dtcs` over a borrowed WiCAN `UDSConnection`) — no new flash-core code; the functions already work over any transport. CLEAR DTC gated behind `--yes` (mutates state). New `tests/test_ecu_wican_ecu_functions.py` (16 tests) proves the three over a `FakeTransport` (the path WiCAN rides) + the tool's pure helpers. **Full suite 1189 passed, 11 skipped, black clean.** Hardware-confirm steps in `WICAN_MANUAL_TEST.md` §3b. Key seam fact: `scan_ram` ignores its `uds=` arg and reads over `self._uds` — so RAM needs `fm.use_uds(uds)` first (auth happens inside); DTC read/clear take `uds=` directly and need no auth (just tester_present).
- **Part C + #22 investigated (3-agent workflow; findings in `docs/internal/WICAN_PART_C_FINDINGS.md`).** (1) **CAN-wedge reboot root cause CONFIRMED against firmware:** `ncflash_fast_read` uses `xQueueSend(..., portMAX_DELAY)`, so a host socket-close mid-stream blocks it forever → `can_rx_task` never resumed → wedged CAN → reboot. Fix = bounded `xQueueSend` timeout + single clean-teardown (resume rx_task on every exit, clear TWAI alerts, flush TX, reset SLCAN). (2) **No-reboot protocol switch:** recommend a coexisting always-on SLCAN port (~35001) over a risky hot-switch. (3) **#22 unified-SD RESOLVED:** KEEP the streaming read (it's ECU-limited, SD gains nothing); if WRITE goes SD, use a MIXED arch (read stays streaming, SD only for write). Firmware changes are gated/future.
- **Part B — WRITE logic Option A BUILT (host-driven safety layer; NOT hardware-flashed).** New `src/ecu/link_quality.py` (pre-flight link-quality gate: Tester-Present burst, 0-loss + p95-latency verdict) + `src/ecu/wican_flash.py` (`WiCANFlasher`: gate + 12.0 V battery guard + **abort-and-restart-from-scratch** on a drop — each retry a fresh whole flash, NEVER a mid-stream resend — + optional read-back verify). Wraps the existing transport-agnostic `FlashManager`; J2534 path unchanged. `flash_rom`/`dynamic_flash`/`preflight` are the UI integration points. 23 new tests (gate verdict, restart-from-scratch asserting fresh FlashManager per attempt, non-restartable passthrough, verify pass/mismatch). **Build-only — no real WiCAN flash performed; must be bench-validated (user-gated) before production.** Chose Option A (design-of-record, brick-safe, ~80% pre-built); Option B (SD-autonomous) stays a documented future option.
- **Tasks:** #17/#18/#19 (RAM/DTC/clear) built — pending the user's hardware confirm. #10/#21/#22 investigations delivered → completed.

## Recent Completed Work (Jun 21, 2026) - WiCAN firmware fast-read: byte-perfect, Tactrix parity (Phase 3 DONE)

- **Firmware fast-read works end-to-end, byte-for-byte identical to the J2534 oracle, at Tactrix parity.** Flashed the `feature/fast-rom-read` firmware (v4, `NCFRv4`) to the WiCAN PRO via OTA and validated: full authenticated 1 MB read **= `wican_stmin0_full.bin` byte-for-byte** at **~214 s (4.8 KB/s)**. The user's own Tactrix measured **215.8 s** on the same ECU → **we matched the reference tool exactly.** The ~60 s goal was optimistic; the real floor is the ECU's per-block **response-pending (~211 ms/block, universal — confirmed at low 0x0 and high 0xD8400 regions)**, which every CAN tool pays. Firmware already removed the per-block WiFi RTT (339 s → 214 s). **Goal MET (Tactrix parity); done-gate PASSED.**
- **Three firmware bugs found + fixed to get byte-perfect** (each masked the next): (1) **response-pending** — `read_one_block` treated `7F 23 78` as the block → desync; now loops past 0x78 (600 ms wait, cap 16) for the real 0x63. (2) **leading CAN-junk shift** — frames queued before `can_rx_task` suspend prefixed the ROM stream, shifting every byte; firmware now emits an **`NCFRDATA` sync preamble** and the host resyncs onto it. (3) **long-stream timeout that looked like a stall** — one 1 MB command runs ~214 s but the host budget was 180 s; host now **chunks into 128 KB commands** (each a fresh suspend/resume), which also stops the device wedging on host-close.
- **Diagnostics added (firmware v4 + host):** firmware **version ping** (sentinel addr `0xFFFFFFFE` → `NCFRv<rev>` marker, no CAN) surfaced as `WiCANTransport.version_ping()` + `tools/wican_fw_ping.py`; firmware **on-abort `FRERR` line** surfaced by `fast_read`. Bench gained `--fast-read-start/-len`; new `tools/wican_fastread_verify.py` (region read + oracle byte-compare). `docs/internal/WICAN_MANUAL_TEST.md` documents the hardware checklist.
- **`/simplify` pass done + re-validated on hardware:** `_frerr_suffix` tail-only scan, Phase-1 search cursor, deleted redundant `wican_fastread_diag.py`, `version_ping()` promoted onto the transport. **Full suite 1173 passed, black clean**; post-simplify hardware read still byte-perfect at 214.8 s.
- **NOT in the UI yet:** `flash_setup_dialog.py` is J2534-only — reading the ROM via WiCAN through the UI needs the adapter-selector (pending task). Bench tool is the supported read path today.
- **Next (tasks created):** confirm READ RAM, READ DTC, CLEAR DTC over WiCAN; then get WRITE/flash working over WiCAN (highest-risk, two-step SD-card design to re-evaluate).

## Recent Completed Work (Jun 21, 2026) - WiCAN read-speed /goal + Phase 0 bench instrumentation

- **`/goal` prompt for the read-speed work** — `.claude/plans/wican-read-speed-goal.md`. Target **reframed to J2534/Tactrix parity ~60 s** (1 MB read; was ~16 min). Diagnosis verified by a 4-agent workflow: the 948.8 s = 448.5 s STmin pacing floor (149,504 CFs × 3 ms) + 116.7 s round-trips + 172 s drop-waste + 211.6 s residual. **CF count (~149.5k) is invariant to block size**, so the STmin floor (`149,504 × STmin`) is the wall: 180 s needs STmin ≤ 1.2 ms, and HW drops at STmin ≤ 1 ms (gateway TX queue is 16×65 B). **≤180 s / ~60 s is firmware-gated** (light: enlarge the WiCAN TX queue; heavy: Frame99-style in-firmware ISO-TP reassembly), barring two fragile software shots (TCP_NODELAY+STmin=1, or BS-paced bursting) to be falsified on the bench. Ordered plan P0→P3 with gates; firmware work goes in **`cdufresne81/nc-flash-wican-fw` on a NEW branch**, and the device's current firmware **must be `esptool read_flash`-dumped + verified before flashing any new build**.
- **Phase 0 — bench instrumentation (DONE, tool-only, no read-path change)** — Extended `tools/wican_bench_read.py`: `--probe` (does the ECU honour read sizes >0x400? tries 0x400/0x800/0xFFE), `--bench-blocks N` (per-block latency distribution + extrapolated 1 MB time), and sweep knobs `--rx-stmin` / `--rx-block-size` / `--block-size` / `--read-timeout-ms` plumbed into `WiCANTransport`. New `tests/test_wican_bench_read.py` (7 tests, hardware-free via a duck-typed fake UDS): summary stats/extrapolation, probe OK/NRC/short, bench clean-vs-drop counting + flush. black clean. **STmin sweep (shot A pacing) and BS-bursting (shot B) are already runnable on the bench via these knobs — no engine change needed.**
- **Gate 1 — MEASURED on the live ECU (2026-06-21), firmware decision made.** Per-block = ~294ms fixed + 146×STmin. Sweep: STmin3=732ms(~749s), 2=586(~600), 1=441(~451), **0+TCP_NODELAY=307(~314s), 1/48 drops**; BS-bursting worse; **reads >0x400 ECU-rejected (NRC 0x31)**. **TCP_NODELAY killed the STmin=0 overflow** → software 948s→**338.7s full read (3×, validated, `wican_stmin0_full.bin`)**. The ~294ms/block WiFi-RTT overhead ×1024 ≈ 300s is the wall → **software can't reach ≤180s; firmware required. Phase 2 (queue enlarge) measured INSUFFICIENT** (only removes STmin pacing already gone). Only **Phase 3** (in-firmware ISO-TP reassembly / autonomous read loop) reaches ~60s. Firmware fork cloned to `C:\Users\dufre\Projets\nc-flash-wican-fw` (branch wican-pro, ESP32-S3, OTA via config_server.c esp_ota; ESP-IDF ≥5.1). Device: 192.168.1.169 slcan:35000 http:80, stored protocol already slcan. **Plan rewritten in `.claude/plans/wican-read-speed-goal.md` as the Phase 3 driver** (user wants: update doc → /compact → /goal execute firmware).
- **Phase 1.5 — STmin=0 is now the WiCAN default** (`DEFAULT_RX_STMIN=0`, was 3). The 3× win, behaviour-safe (idempotent reads + N_Cr + retry). CHANGELOG updated. 117 transport/isotp/flash tests green.
- **Phase 3 — firmware autonomous read (BUILT, NOT YET FLASHED — blocked on backup decision).** Firmware fork cloned to `C:\Users\dufre\Projets\nc-flash-wican-fw`, branch `feature/fast-rom-read`. New `main/ncflash_fastread.c/.h` (parse `X<addr8><len8>`, suspend `can_rx_task`, locally loop `ReadMemoryByAddress(0x400)` over CAN + ISO-TP reassemble + stream blocks to TCP TX queue; per-block retry; periodic `vTaskDelay` for the task-WDT) + `main.c` hook (routes `X` cmd) + CMakeLists. **Requires ESP-IDF v5.5.3** (5.1 fails on `restart_tracker.c` esp_cache API; installed to `C:\esp\esp-idf-v5.5.3`, build via Python 3.10 + `VIRTUAL_ENV` cleared). Built clean: `build/wican-fw_obd_pro_v449p_beta-05-dirty.bin` (3.5 MB, WDT yield verified in it). OTA flash = multipart `POST /upload/ota.bin` (curl -F). NC Flash side: `WiCANTransport.fast_read()` + bench `--fast-read` (tested, full suite 1167 passed). **BLOCKED:** flashing was denied because the user's explicit "keep a copy of the current build deployed" boundary isn't met — can't read the device's exact firmware (OTA is write-only, no USB for esptool). Awaiting user's backup decision (USB dump / they provide the .bin / build a clean rollback / accept inactive-partition+safe-mode net). Validation pending: `--fast-read --reference wican_stmin0_full.bin` (target ~60s + byte-identical).
- **Phase 1 — software levers (DONE, implemented + unit-tested; final values to be tuned from bench)** — (1) **N_Cr fast-fail** in `isotp.py` (`IsoTpSession(n_cr_ms)`): a mid-message Consecutive-Frame gap > N_Cr raises a definitive `IsoTpError` (not `IsoTpTimeout`) so the read-retry re-requests at once instead of stalling ~4 s. `WiCANTransport` default `DEFAULT_N_CR_MS=500`; **J2534 passes `None`, byte-identical.** (2) **TCP_NODELAY (default on) + optional SO_RCVBUF** in `WiCANTransport.open()`. (3) **Configurable read block size** — `FlashManager.read_rom(read_block_size=…)`, clamped to `MAX_ISOTP_READ_SIZE=0xFFE`, default still `0x400`. Bench tool exposes `--n-cr-ms`/`--no-tcp-nodelay`/`--so-rcvbuf`/`--block-size`; `full_read` honours `--block-size`. +10 tests (isotp N_Cr ×4, transport tuning ×3, read_rom block-size ×3). **Full suite 1165 passed, 11 skipped, black clean.** Defaults are behaviour-preserving; bench (Gate 1, #13) tunes N_Cr down to the clean-block gap and decides bigger-block size. Remaining: Gate 1 bench sweep (user/hardware), then firmware Phases 2/3 if software floor > target.

## Recent Completed Work (Jun 20, 2026) - WiCAN read path hardware-validated end-to-end (secure module + 3 reliability fixes)

- **Installed the private `_secure` module** (`src/ecu/_secure/` from `cdufresne81/nc-flash-secure`, gitignored) → `SECURE_MODULE_AVAILABLE=True`; its own seed/key + SBL tests now run (suite +secure tests). Enables authenticated ROM read/flash on this machine.
- **Live validation against a real MX-5 NC ECU** over WiCAN PRO (`192.168.1.169:35000`, slcan mode): smoke 25/25 @ ~55 ms, seed/key auth, multi-frame VIN, and a **full authenticated 1 MB ROM read** completing end-to-end. Found + fixed 3 hardware-only issues:
  1. **Open warm-up prime** (`WiCANTransport._prime_channel`) — the adapter drops the first frame after the SLCAN `O` ack, so the first real request hung ~60 s. Now sends a throwaway TesterPresent `3E 80` then **drains its reply** (the NC ECU NAKs `0x80` with `7F 3E 12`; the drain stops that stray frame polluting the first real request). First real request now answers in ~60 ms.
  2. **Receive flow-control pacing** — at STmin=0 the ECU blasts ~146 CFs/1 KB faster than the gateway forwards → CAN→TCP buffer overflow → silent frame drops. `WiCANTransport` now advertises **STmin=3** (BS=0); tuned on HW (STmin 0→drop@8/64, 1→40/64, 2→64/64). `rx_block_size`/`rx_stmin` are ctor knobs.
  3. **Per-block read retry** (`FlashManager.read_rom` + `_read_block_with_retry`) — pacing makes drops rare but not impossible over 1024 blocks; lost/garbled blocks are re-requested (reads are idempotent) up to 4× on a tight ~4 s budget, flushing stale frames between tries via new **`EcuTransport.flush()`** (no-op J2534, frame-drain WiCAN) + `UDSConnection.flush()`. `send_request`/`read_memory_by_address` gained an optional `pending_max`. Live: a drop happens ~1/25 blocks and every one recovers on the next attempt. **Flash/write path untouched** (resend bricks).
- Tests: +10 (`TestReadBlockRetry`, WiCAN/J2534/UDS flush, prime-reply-drain, read-budget passthrough). Full suite **1147 passed, 11 skipped**, black clean. New `tools/validate_wican_read.py` (checksum self-consistency / determinism validator). Docs: `WICAN_TRANSPORT.md` §5/§6/§8b updated. NOTE: rapid re-auth trips the ECU security cooldown (NRC 0x22) — leave a few seconds between programming-session attempts.

## Recent Completed Work (Jun 20, 2026) - WiCANConfigurator (auto slcan protocol switch/restore)

- **`src/ecu/wican_config.py`** — Headless, stdlib-only HTTP helper that switches the WiCAN PRO's top-level `protocol` to `slcan` and restores the previous value (e.g. the user's custom `poll_log`), via a **surgical** read-modify-write of `/load_config`+`/store_config` that preserves every other config field byte-exact (passwords incl.). `(?<!_)"protocol"` regex with exactly-one-match guard + parse-check before POST; tolerates the ~6s reboot (POST-drop expected, poll bounded by `reboot_timeout_s`). `slcan_session()` context manager + a **host-keyed recovery sidecar written BEFORE the switch** so a hard kill can't strand the device in slcan with the original mode lost; next run restores the recorded original. Proven manually against the real device (192.168.1.169:35000): `poll_log→slcan→poll_log`.
- **`tools/wican_bench_read.py`** — `--auto-config`/`--http-port` flags: wraps the read in `slcan_session()` so the device auto-switches to slcan and restores on success/error/Ctrl-C. Off by default.
- Tests: `tests/test_ecu_wican_config.py` (27, mock-HTTP, incl. field-preservation, crash-recovery, persist-before-switch). Full suite: **1120 passed, 26 skipped**, black clean. See [[project_wican_custom_firmware]] — long-term goal is a firmware-side no-reboot switch (task #10).

## Recent Completed Work (Jun 16, 2026) - WiCAN transport hardened + bench-read tool

- **Adversarial-review fixes (all on the new WiCAN/ISO-TP path; J2534 path verified byte-identical)** — `isotp.py`: short non-final Consecutive Frame and short First Frame now raise `IsoTpError` (was silent under-fill → misaligned/corrupt reassembly); `_pad` raises on >8 bytes; added `IsoTpTimeout(IsoTpError)` raised ONLY on deadline paths. `wican_transport.py`: handshake BEL/NAK detection is now coalescing-proof (scans whole ack buffer, routes early data frames into the stream); `receive_message` maps ONLY `IsoTpTimeout`→`None`, re-raising real protocol errors as `WiCANError` (no more silent retry of corruption). `slcan.py`: `is_error_ack` uses BEL membership not `endswith`; `SlcanFrameStream.feed` all-or-nothing contract documented. Pinned BS=0x0F and 0xFFF max-payload round-trips. Full suite: 1093 passed, 26 skipped, black clean.
- **`tools/wican_bench_read.py`** — Standalone non-destructive GO/NO-GO harness: a Tester-Present link smoke test (loss + latency; #476 check, no security module needed) then a full 1 MB ROM read with throughput measurement and optional byte-perfect diff vs a J2534 dump (`--reference`). Drives `WiCANTransport` + `FlashManager.use_uds`. UTF-8 console reconfigure so output can't crash on a legacy code page. For the user's bench validation (task #8).

## Recent Completed Work (Jun 15, 2026) - UDSConnection refactored onto EcuTransport seam

- **`UDSConnection.__init__(transport)`** — Now takes an `EcuTransport` instead of `(j2534_device, channel_id)`. `send_request` calls `self._transport.send_message(request_data, timeout)` and `self._transport.receive_message(timeout)`; the NRC 0x78 retry loop, response-pending budget, 0x7F/positive-SID parsing, logging, and `J2534Error`-propagate / other→`UDSTimeoutError` behaviour are all unchanged. The transport's `receive_message` returns `None` for both empty-read and `DataSize<=4`, folding into the single `if not resp_data:` branch (matches the seam contract). J2534 flash I/O stays byte-for-byte identical.
- **session/flash wiring** — `ECUSession.connect_ecu` and `FlashManager._connect` (owns branch) + the `read_dtcs`/`clear_dtcs`/`read_vin_block` no-borrow branches + `src/ui/flash_setup_dialog.py` now build `UDSConnection(J2534Transport(device, channel_id))`. J2534 is the default everywhere.
- **`FlashManager.use_uds(uds)`** — New transport-agnostic injection point (mirrors `use_session` borrowed semantics: `_owns_connection=False`, no device handles) so a WiCAN-built `UDSConnection` can drive non-flash ops without a J2534 device.
- **`create_ecu_transport(config)` factory** in `transport.py` — `{"kind":"j2534","device":..,"channel_id":..}` or `{"kind":"wican","host":..,"port":..,...}`. Lazy-imports `WiCANTransport` to avoid the `wican_transport`↔`transport` circular import.
- **Exports** — `src/ecu/__init__.py` now exports `EcuTransport`, `J2534Transport`, `FakeTransport`, `WiCANTransport`, `WiCANError`, `create_ecu_transport` (WiCAN/session guarded by try/except).
- **Tests** — Updated `mock_uds` fixture (conftest) + `test_ecu_obd._make_uds` to the new constructor; added factory tests (`test_ecu_transport.py`) and `TestUseUds` (`test_ecu_flash_integration.py`). Full suite: 1074 passed, 26 skipped (pre-existing `_secure`/RomDrop skips). black clean. transport.py stays headless (no PySide6 at module scope).

## Recent Completed Work (Jun 14, 2026) - V2 TCM definitions imported (#70)

- **Imported 4 V2 TCM definitions from NC_TCM into `examples/metadata/`** — LFG1TF000, LFG1TG000, LFACTA000, LFAMTA000 (`*_v02.xml`). Removed the legacy V1 `lfg1tf000.xml`. Added `examples/LFG1TF000.bin` (real dump) and `tests/test_tcm_v2_detection.py`.
- **No parser change needed** — V2 TCM defs use the SAME RomDrop XML schema as the ECU defs, so `DefinitionParser`/`RomDetector` handle them unchanged. Detection matches via `internalidstring` `SW-LFG1TF000.HEX` at `internalidaddress` 0x10612.
- **Validation status** — Only LFG1TF000 is hardware-validated (against `examples/LFG1TF000.bin`); LFG1TG000 awaits a real TCM dump before it can be trusted.
- **LFACTA000 & LFAMTA000 removed** — Owner confirmed these two imported definitions were incorrect, so `examples/metadata/LFACTA000_v02.xml` and `LFAMTA000_v02.xml` were removed (along with their references in `tests/test_tcm_v2_detection.py`, CHANGELOG, and README). Only LFG1TF000 and LFG1TG000 remain.
- **Scope: Phase A only** — This is data + test + docs (zero `src/` changes), closing #70. Phase B (TCM flashing) is a separate future R&D effort, NOT in this branch. See `.claude/plans/tcm-v2-import.md`.
- **Phase A merged** — PR #73 merged to master (admin-merge after CI green; master requires review + CI, see memory `project_master_branch_protection`). Phase B filed as #72.

## Recent Completed Work (Jun 14, 2026) - TCM README + checksum investigation (follow-up)

- **Checksum investigation (re: TCM brick risk)** — `correct_rom_checksums()` (`src/ecu/checksum.py`) is ECU-only (table @ 0xFF650) and called from ONE place: `flash_manager._flash_rom_inner` (ECU dynamic flash), on a copy. It NEVER runs on save and NEVER on a TCM ROM. So editing+saving a TCM today is non-destructive; there's no TCM checksum handling and none is needed until Phase B. Added an ECU-only docstring guard-note to `correct_rom_checksums()` so it isn't reused for TCM. Phase B must implement its own TCM checksum routine.
- **NC_TCM has no flash source** — public NC_TCM `tools/` ships only `NC_TCM_Read.exe` + `.gitkeep`. David's TCU flashing is an external/private tool; need him to share seed/key + flash sequence for Phase B.
- **README** — documented TCM read support (read-only) and the V2 defs + example dump in Features and project structure.

## Recent Completed Work (Jun 10, 2026) - Auto-Blip table definitions for LF9VEB

- **Auto-Blip category added to `examples/metadata/lf9veb.xml`** — 8 scalings + 8 tables (Enable, APP Threshold, Min VSS, Min RPM, Max RPM Target, Decay Factor, Max Duration, RPM Delta→TP Offset 2D) for the custom autoblip patch living in ROM free space (calibration block 0xFCAC0). These addresses only contain valid data on autoblip-patched ROMs (generator: nc-flash-re/tools/autoblip_patch.py); on stock or plain-Romdrop ROMs they read 0xFF. Validated with `tools/validate_autoblip_defs.py` (new one-shot checker: parses defs with DefinitionParser, reads expected defaults from a patched ROM — all 8 PASS).
- Note: autoblip-patched ROMs cannot go through the Patch ROM dialog (`patch_rom` hard-fails on `romdrop.crc` patch CRC mismatch, by design). Workflow is full-image flash of the pre-patched bin; `correct_rom_checksums` handles the 3 stale Mazda checksum entries.

## Recent Completed Work (May 27, 2026) - Calibration mismatch warning before dynamic flash (#68)

- **Pre-flash cal-ID check** — `_on_flash_current()` in `ecu_window.py` now compares the calibration ID of the ROM being flashed against the archive (last known ECU state) before starting a dynamic flash. If they differ, a `QMessageBox.warning` asks the user to confirm before proceeding. Check runs in the main thread before the worker starts, so no threading complexity. Gracefully skips validation if cal-IDs can't be read.
- **3 new tests** in `TestCalIdCompatibility` (`test_ecu_rom_utils.py`) — verify same cal-ID matches, different cal-IDs mismatch, tuned ROM retains stock cal-ID.

## Recent Completed Work (Apr 17, 2026) - ROM utils vectorization + patch dialog fix

- **Vectorized XOR in `patch_rom`** — replaced Python byte loop (1MB, ~1-2s) with numpy in-place `^=` on bytearray buffer view (sub-ms). `src/ecu/rom_utils.py`. Added `import numpy as np` at module level.
- **Vectorized `find_first_difference`** — replaced Python byte loop with `np.where(a != b)[0]`. Same file.
- **Patch dialog UX fix** — `PatchRomDialog._apply_patch` now hides the result group before each attempt, so stale CRC/cal-ID from a prior success isn't shown after a failed retry. `src/ui/patch_dialog.py`.

## Recent Completed Work (Apr 7, 2026) - Paste scaling clamp bug

- **Paste silently dropped out-of-range cells** — `clipboard.py::paste_selection` clamped pasted values against the XML-declared scaling `min`/`max` and silently skipped anything outside. Bug was latent since the clipboard refactor (commit `c5b0623`), not a recent regression. Broke copy between sibling tables where the source held raw bytes exceeding the stated max (VCT Target → [Flex] VCT Target with `35`s against `max=25`), and fully disabled paste for scalings with placeholder `min=0/max=0` (Speed Density - Volumetric Efficiency). Removed the clamp — `display_to_raw` is the real safety net. Added 2 regression tests in `TestPasteIgnoresScalingClamp`.

## Recent Completed Work (Apr 5, 2026) - Bugfixes & Regression Tests

- **Interpolation auto-round fix** — V/H/2D interpolation used `round_one_level_coarser()` instead of `round(val, precision)` when auto-round was enabled, coarsening values (2.04→2.0, 0.01→0.0). Extracted `compute_interpolated_1d_values()` and `compute_interpolated_2d_values()` as pure functions. Added 24 regression tests.
- **MCP workspace.json path fix** — `workspace.json` was written to `get_app_root()` which resolves to per-process `_MEIPASS` in PyInstaller builds. Moved to `get_user_data_dir()` via new `get_workspace_path()` helper in `paths.py`.
- **Black target-version pinned** — Added `pyproject.toml` with `target-version = ["py312"]` to prevent formatting divergence between local Python 3.14 and CI Python 3.12.
- **Flaky CI test fix** — `test_wrong_path_returns_404` handled `ConnectionAbortedError` race condition.
- **Parameter naming standardized** — `fmt_precision` → `precision` across all pure computation functions.

## Recent Completed Work (Apr 5, 2026) - MCP Single Source of Truth

**Unified MCP data path** — All MCP tools now delegate to the running NC Flash app via its command API (single source of truth for ROM definitions and data). Previously, disk-read tools (list_tables, get_rom_info, read_table, etc.) had their own standalone ROM detection and definition loading, which failed for ROMs whose definition XML wasn't in the MCP server's metadata directory (e.g., LF4XEG).

- Added 4 new command API endpoints: `/api/rom-info`, `/api/list-tables`, `/api/table-statistics`, `/api/compare-tables`
- Rewrote `rom_context.py` — removed standalone `RomDetector`/`RomReader`/`load_definition`, all tools now use `_post_to_app()`
- `read_table` and `read_live_table` are now equivalent (both read from app)
- Updated all MCP tests to delegation-based (mock `_post_to_app`)
- Files: `src/api/command_server.py`, `src/ui/mcp_mixin.py`, `src/mcp/rom_context.py`, `src/mcp/server.py`, `tests/test_mcp_server.py`

## Recent Completed Work (Apr 4, 2026) - Architectural Review & Refactoring

**Full architectural review** — 40K lines read into 1M context, produced review and plan at `.claude/plans/zippy-pondering-puppy.md`.

**Completed refactoring (Phase 3 & 5 of 7):**
- **Unified table CSS** — Created `get_table_stylesheet()` in `src/utils/constants.py`, replaced 3 duplicate CSS blocks in `table_viewer.py`, `display.py` (removed dead method), and `compare_window.py`
- **TableKey namedtuple** — Replaced null-byte `\0` separator in composite keys with `TableKey = namedtuple('TableKey', ['rom_path', 'table_address'])` in `table_undo_manager.py`. Updated `change_tracker.py`, `version_models.py`, `main.py`, `mcp_mixin.py`, `table_viewer_window.py`, and all tests. `extract_rom_path`/`extract_table_address` still work as backward-compatible wrappers.
- **Architecture rules saved** — 6 rules in memory (`feedback_architecture_rules.md`)

**Remaining phases (not yet started):**
- Phase 1: Extract edit pipeline service (3 copies → 1)
- Phase 2: Replace shared mutable dicts with ModificationTracker
- Phase 4: Eliminate signal forwarding hop in TableViewerWindow
- Phase 6: Extract shared table population logic from compare_window
- Phase 7: Convert mixins to composition (incremental)

**Open GitHub issues:** #65 (test dead flash mixin removal with hardware), #66 (test auto-save dedup with hardware)
**Pending branches:** `fix/remove-dead-flash-mixin`, `fix/dedup-auto-save` (need Tactrix dongle validation)

## Recent Completed Work (Apr 4, 2026) - Full Code Audit & Cleanup

**Code audit session** — loaded entire ~42K line codebase into 1M context, identified and fixed:
- 4 bugs (select_all off-by-one, scaling `^` conversion, interleaved duplicate read, compare window cleanup)
- Removed ~500 lines of dead code (GraphViewer, dead flash mixin methods, dead CSS, stale refs)
- Deduplicated error handler, toolbar wrappers, auto-save helpers
- 70 new UI tests (compare_window, table_browser, graph_viewer, table_viewer_window)
- Fixed test_runner.py set_level_filter bug
- Cleaned up debug scripts, updated README, made pytest coverage opt-in

**Audit document:** `docs/internal/CODE_AUDIT.md` — full findings

## Recent Completed Work (Apr 4, 2026) - Copy All, Workspace, Settings Redesign
- **Comparison Copy All** — Two new toolbar buttons ("Copy All A→B" / "Copy All B→A") in CompareWindow. Copies all eligible differing tables in one operation with progress dialog, cancellation support, and partial failure handling. Sidebar labels update to show "(identical)" after copy.
- **Workspace directory** — New `paths/workspace_directory` setting provides a single root for all user content. All path settings (projects, exports, metadata, colormaps, roms, screenshots, reads) now derive defaults from the workspace root. Individual path overrides still work. Migration copies bundled metadata/colormaps on first run. File dialogs now default to workspace subdirectories.
- **Settings dialog redesign** — Replaced QTabWidget with tree sidebar + stacked pages + search bar. Data-driven `SettingDescriptor` registry makes adding settings a one-line change. Search scores settings by label, keywords, description with highlighted results. Ctrl+F focuses search, Escape clears. Click search result to navigate to setting page.
- **New settings**: `get/set_workspace_directory()`, `get/set_roms_directory()`, `get/set_screenshots_directory()`, `get/set_reads_directory()` in AppSettings
- **New file**: `src/utils/workspace.py` — workspace directory creation and migration logic
- **Branch**: `feature/copy-all-workspace-settings` (not yet committed)

## Recent Completed Work (Apr 3, 2026) - Table Browser Columns & Splitter Persistence
- **Table browser columns auto-sized** — Type column: Fixed 40px, Address column: Fixed 75px, Name column: Stretch (fills remaining space). Removed `TABLE_BROWSER_COLUMN_WIDTH` constant.
- **Column visibility settings** — New `show_type_column` / `show_address_column` boolean settings in AppSettings, with checkboxes in Settings > Appearance > Table Browser group. Changes apply immediately to all open tabs.
- **Splitter position persisted** — Main splitter (`main_splitter`) state saved/restored via `get/set_splitter_state()` in session close/init. Previously these AppSettings methods existed but were never called.

## Recent Completed Work (Apr 3, 2026) - Safety-Critical Bounds & Atomic Writes (#57-#61)
- **Integer overflow validation (#59)** — Added `STORAGE_TYPE_BOUNDS` to `storage_types.py` and `_validate_and_pack()` method on `RomReader`. All 3 write methods (`write_table_data`, `write_cell_value`, `write_axis_value`) now validate integer values against storage type bounds before `struct.pack()`. Raises `RomWriteError` with value, type, and range.
- **Interleaved 3D read bounds (#57)** — `_read_interleaved_3d()` now validates M/N are non-zero and total table footprint fits in ROM before any data access. Raises `RomReadError` with table name, M, N, address.
- **Interleaved 3D write bounds (#58)** — `write_table_data()` interleaved branch now validates entire write footprint fits in ROM and rejects multi-byte storage types (stride too small). Raises `RomWriteError`.
- **Cell/axis index validation (#60)** — `write_cell_value()` validates row/col against table dimensions before computing address. `write_axis_value()` validates index against axis length. Uses ROM-derived M/N for interleaved tables. Raises `RomWriteError`.
- **Atomic project file writes (#61)** — Added `_atomic_copy()` and `_atomic_write_binary()` to `ProjectManager`. Replaced 4 non-atomic writes: create_project (v0 + working), commit_changes (snapshot), revert_to_version (working ROM overwrite). All use tmp+fsync+os.replace pattern with cleanup on failure.
- **18 new tests** — 15 in `test_interleaved_tables.py` (read/write bounds, index validation, integer overflow), 3 in `test_project_manager.py` (atomic writes).
- **Branch:** `fix/safety-critical-bounds-atomics-57-61` — NOT committed yet.

## Recent Completed Work (Apr 2, 2026) - DTC NRC 0x22 Fix (#52)
- **Handle NRC 0x22 gracefully in DTC reads** — `read_dtc_count()` and `read_dtc_status()` in protocol.py now catch NRC 0x22 and return 0/[] instead of raising. Defense-in-depth added in flash_manager.py `read_dtcs()`.
- **Isolate DTC reads from VIN/ROM ID** — In flash_setup_dialog.py and flash_mixin.py, DTC read is now wrapped in its own try/except so failures don't discard already-read VIN and ROM ID.
- **Fix PassThruMsg struct for Linux 64-bit** — Changed `c_ulong` to `c_uint32` in j2534.py struct definitions. Fixes CI failure on ubuntu runners where c_ulong is 8 bytes.

## Next Tasks
- CI secret `SECURE_REPO_PAT` is configured and matches workflows. No graceful fallback if missing (CI hard-fails), but this is acceptable.
- `examples/metadata/LFDJEA.xml` is untracked — may need committing
- **Review romdrop.crc fallback** — `src/ecu/rom_utils.py:169` silently skips CRC verification if `romdrop.crc` is missing. Patching still proceeds without validation. Need to decide: should patching be blocked without the CRC database, or is a warning sufficient?

## Recent Completed Work (Mar 30, 2026) - MCP Second Window Fix (#41)
- **Fixed compiled version opening second blank window for MCP server** — In PyInstaller builds, `sys.executable` is the app exe, so `subprocess.Popen([sys.executable, "-m", "src.mcp.server"])` re-launched the entire GUI. Now sets `NCFLASH_MCP_MODE=1` env var when spawning subprocess; `main()` checks this and bypasses GUI to run MCP server directly. Also uses `STARTUPINFO`/`CREATE_NO_WINDOW` on Windows to suppress any window creation.

## Recent Completed Work (Mar 29, 2026) - Scan RAM UI
- **"Scan RAM" button in ECU window** — Exposes the existing `scan_ram()` backend (reads 192 blocks of 0x1F0 bytes from ECU RAM 0x0000-0xBFFF) as a UI button alongside Read ROM and DTCs
- **Threaded with progress** — Uses `_FlashWorker` pattern with `SCANNING_RAM` state, shows block-by-block progress, abortable
- **Auto-saves RAM dump** — Saves to `~/.nc-flash/reads/{ROM_ID}_RAM_{timestamp}.bin` and opens Explorer to the file

## Recent Completed Work (Mar 29, 2026) - Clear DTCs from Read Dialog (#33)
- **"Clear DTCs" button in read-DTC dialog** — After reading DTCs, the results dialog now shows a "Clear DTCs" button alongside OK. Clicking it sends the clear command immediately without a second confirmation prompt
- **Extracted `_do_clear_dtcs()` helper** — Shared by both the dialog button and the standalone "Clear DTCs" toolbar button

## Recent Completed Work (Mar 29, 2026) - Checksum Table Fix (P0601/P0606)
- **CHECKSUM_TABLE_OFFSET was 0xFF658 — corrected to 0xFF650**: The 8-byte misalignment caused every checksum entry to be misread (checksum field parsed as start address), corrupting all 35 table entries with CHECKSUM_MAGIC before flashing
- **End address is inclusive, not exclusive**: Table stores last byte of range; fixed `correct_rom_checksums` to pass `end_incl + 1` to `mazda_checksum`
- **Removed unnecessary exclude_offset logic**: No real checksum entry's range covers the table at 0xFF650; the self-reference fix from the prior commit was a red herring
- **Added real ROM verification tests**: `test_real_rom_no_corrections` and `test_real_rom_idempotent` using `examples/lf9veb.bin`

## Recent Completed Work (Mar 28, 2026) - Rounding Feature
- **Round Selection (R key)** — New bulk operation rounds selected cells one decimal level coarser. Uses scaling format to determine max precision, detects effective decimals, rounds to one less. Repeatable: 12.11 → 12.1 → 12.0. Works on data + axis cells via `apply_bulk_operation`
- **Auto-round setting** — New `editor/auto_round` boolean setting (default off). When enabled, interpolation (1D + 2D) and smoothing automatically round computed values one decimal coarser. Checkbox added to Settings > Editor tab
- **Rounding utilities** — `get_effective_decimal_places()`, `round_one_level_coarser()`, `_get_format_precision()` added to `src/utils/formatting.py`
- **Round toolbar icon** — Curve-with-dots icon added to `icons.py`
- **29 tests** in `tests/test_formatting.py` covering all rounding functions

## Recent Completed Work (Mar 27, 2026) - House Cleaning
- **CHANGELOG restructured** — Unreleased section was a mess (contained v2.1.0 through v2.3.0 items). Split into proper `[v2.3.0]`, `[v2.2.0]`, `[v2.1.1]`, `[v2.1.0]` sections with correct dates. Unreleased now only has current house-cleaning work
- **GitHub v2.3.0 release notes updated** — Were using stale Unreleased dump; now match the proper v2.3.0 changelog section
- **README overhaul** — Version v2.0.0 → v2.3.0, rewrote ECU Flashing for native J2534 (removed RomDrop references), added missing feature sections (Project Management, cross-definition compare, toolbars, setup wizard), corrected test coverage and CI versions
- **Docs reorganized** — Moved internal docs to `docs/internal/`, removed 19 obsolete files (~6 MB): code audits, mockups, error screenshots, EcuFlash examples, archived design docs. Updated CLAUDE.md and README paths
- **Deleted `run-dev.bat`** — Vestigial, identical to `run.bat` since `--enable-projects` removed
- **Removed `examples/LF5AEG*`** — 3 tracked ROM/patch files removed from git
- **Removed `Thinking-pad.md` from git** — Added to `.gitignore`, kept local file
- **Branch `fix/house-cleaning`** created from `origin/master` with all changes staged

## Recent Completed Work (Mar 27, 2026) - Drag-and-Drop
- **Drag-and-drop ROM files** — Users can drag `.bin`/`.rom` files onto the main window to open them. Translucent blue overlay with dashed border shows during drag-over. Invalid file types show a descriptive error. Multiple files supported. Reuses existing open-file flow. 11 tests in `test_drag_drop.py`. (#20)

## Recent Completed Work (Mar 27, 2026) - Issue #21 Fix
- **Battery voltage warning differentiated by operation** — Read ROM now shows a softer "communication timeouts" message instead of "bricking" warning. Flash operations keep the strong warning. (#21)

## Recent Completed Work (Mar 26, 2026) - Build Fix
- **J2534 bridge frozen-app fix** — PyInstaller builds failed to load 32-bit DLL because frozen ctypes raises a different OSError than native bitness mismatch. Bridge fallback now detects both.

## ECU Module Status (feature/ecu-flash-module branch)
- **Read ROM**: Working end-to-end. Threading verified safe (explicit `Qt.QueuedConnection` on all worker signals).
- **Flash ROM**: Working. CheckFlashCounter resolved — moved from `_authenticate()` to flash-only path (`flash_manager.py:525-527`), matching romdrop binary analysis.
- **Security algorithm**: Working (3-byte seed + "MazdA" → 8-byte LFSR)
- **32-bit bridge**: Working. Auto-builds on first dev use via `packaging/build_bridge.py`
- **_secure module**: Private repo only (nc-flash-secure). CI pulls via `secrets.SECURE_REPO_PAT` (not `SECURE_MODULE_PAT` as previously noted)

## Recent Completed Work (Mar 24, 2026) - ECU Programming Window
- **ECU Programming window** — Dedicated window replacing scattered ECU menu items. Auto-connects, status cards (battery/engine/ECU), one-click dynamic flash, inline progress, auto-save ROM reads as `{ROM_ID}_{timestamp}.bin`
- **OBD-II PID reading** — Battery voltage (PID 0x42) and engine RPM (PID 0x0C) confirmed working on NC2 ECU. Voltage is soft warning (12V threshold), engine running is hard block
- **Checksum 67x faster** — struct.unpack batch decode replaces Python for-loop. Bounds checking added for invalid table entries
- **Safety audit** — Fixed _ecu_busy stuck True, abort signal accumulation, missing __init__ attrs, _owns_connection reset, subprocess error handling, closeEvent thread cleanup
- **Per-session logs** — `./logs/YYYY-MM-DD_HHMMSS.log` per app launch
- **UDS log direction prefixes** — `ECU >>` / `Tool >>` on protocol messages
- **DTC log deduplication** — "Read 15 DTCs (7 unique)"
- **Window geometry persistence** — Saves/restores position and size
- **Tester Present demoted to DEBUG**

## Recent Completed Work (Mar 24, 2026) - ECU Flash Module Hardening
- **Security algorithm fix** — Seed-to-key was wrong: ECU sends 3-byte seed, must append 5-byte challenge constant "MazdA" to form 8-byte LFSR input. Found by tracing romdrop.exe binary at 0x0040587C. Verified against 2 known pairs from romdrop logs.
- **32-bit bridge exe** — Built j2534_bridge.py as standalone 32-bit PyInstaller exe. Updated NCFlash.spec to bundle it, build.bat to build it, release.yml for CI. j2534.py looks for exe first, falls back to py -3-32.
- **ECU Info cleanup** — VIN strips non-printable bytes, ROM ID strips 2-byte echo prefix, DTCs deduplicated. Added P0F01, U3F01-U3F04, U3F21, U3FC1 to DTC table.
- **CheckFlashCounter moved to flash-only** — Was in _authenticate(), bricked ECU when called during Read ROM. Binary analysis confirmed it's only in flash path (0x00404C72), never in read path.
- **_secure module purged from public repo** — git filter-branch rewrote all 232 commits. .gitignore updated. Private repo nc-flash-secure updated with corrected algorithm.
- **Thread safety fixes** — Qt.QueuedConnection on all worker→UI signals (was missing in flash path). Abort flag changed from bool to threading.Event.
- **Error handling** — J2534Error now propagates instead of being masked as UDSTimeoutError. Bridge timeout overhead reduced from +5s to +2s.
- **Removed redundant "Clear DTCs" menu item** — Read DTCs already offers clear.
- **Bridge log levels** — Demoted bridge startup messages from INFO to DEBUG.
- **Abort during read** — Enabled abort button during READING state (safe — no write transaction).

## Recent Completed Work (Mar 23, 2026) - Interleaved 3D Tables
- **Interleaved 3D table support** — Added `TableLayout` enum (`CONTIGUOUS`/`INTERLEAVED`), `layout` attribute parsing in definition parser, and interleaved read/write/cell-edit/axis-edit in `RomReader`. 256 lines of tests in `test_interleaved_tables.py`. Enables TCM ROM support where Y-axis values are interleaved with data rows.

## Recent Completed Work (Mar 26, 2026) - README Audit & Cleanup
- **Rebased feature/ecu-flash-module onto master** — Picked up v2.1.0 changelog and merge commit
- **README audit and update** — Fixed version (v2.0.0 → v2.3.0), corrected test coverage stats, fixed CI Python versions (3.10/3.12), added `mcp_mixin.py` to project structure tree
- **Deleted `run-dev.bat`** — Vestigial launcher identical to `run.bat` since `--enable-projects` flag was removed
- **CHANGELOG updated** — Added README changes and run-dev.bat removal to Unreleased section

## Recent Completed Work (Mar 5, 2026) - Pipeline Fixes
- **Black formatting** — Ran black on 21 unformatted files (was failing CI lint)
- **Release pytest fix** — Changed `requirements.txt` to `requirements-dev.txt` in release.yml (pytest was missing)
- **workflow_dispatch** — Added manual trigger to both CI and release workflows
- **CLAUDE.md** — Added `black` to quality gates checklist

## Recent Completed Work (Mar 3, 2026) - Code Audit & Cleanup
- **Full codebase audit** — Read all 47 source files (16,453 lines), all 19 test files (243 tests), all docs/configs. Wrote detailed audit to `docs/CODE_AUDIT_2026_03.md`.
- **Dead code removal** — Removed 7 items: 4 unused dataclasses from `version_models.py`, 4 legacy methods from `table_viewer.py`, deprecated `get_modified_tables()` from `change_tracker.py`, legacy `ScalingEditDialog` from `scaling_edit_dialog.py`, unused `HistoryPanel` from `history_viewer.py`, deprecated `update_modified_tables()` from `table_browser.py`.
- **Duplication consolidation** — Created `src/utils/formatting.py` with shared `printf_to_python_format`, `format_value`, `get_scaling_range`, `get_scaling_format`. Eliminated triple-duplication across `display.py`, `compare_window.py`, `rom_context.py`.
- **Interpolation dedup** — Unified near-identical `interpolate_vertical`/`interpolate_horizontal` (~250 lines each) into shared `_interpolate_1d(direction)` + extracted `_apply_axis_interpolation` and `_apply_data_interpolation` helpers. Fixed bug where horizontal emit was per-range instead of once-after-all-ranges.
- **Error handling fixes** — 3 silent `except: pass` in `main.py` now log with `logger.debug`; exception chain added in `project_manager.py`.
- **Test fix** — Fixed pre-existing `test_get_table_font_size_default` (expected 9 but default was changed to 11).
- **Test runner dispatch refactor** — Replaced 159-line if/elif chain in `_execute_command` with dispatch table + small handler methods.
- **Dependency split** — `requirements.txt` now runtime-only; dev tools in `requirements-dev.txt`. CI updated to use `requirements-dev.txt`.
- **Doc cleanup** — Archived abandoned `MODIFICATION_TRACKING_PLAN/SUMMARY.md` to `docs/archive/`. Updated ROM comparison spec to reflect implemented features.
- **MCP mixin extraction** — Moved 13 MCP/API methods (~500 lines) from `main.py` into `src/ui/mcp_mixin.py`. `main.py` is now 1,970 lines (was 2,606). Also fixed latent import bug where API handlers referenced renamed `_printf_to_python_format`.

## Recent Completed Work (Mar 2, 2026) - Rebrand: NC ROM Editor → NC Flash
- **Full project rename** — Renamed all references from "NC ROM Editor" / "NCRomEditor" to "NC Flash" across the entire codebase. Display name is "NC Flash", exe/filenames use "NCFlash" (no space), GitHub repo is `cdufresne81/nc-flash`. Updated: app name, exe name, asset files, installer, build scripts, CI workflow, MCP server, QSettings keys, user data directory, launcher scripts, setup wizard, documentation, tests, and CHANGELOG.

## Recent Completed Work (Mar 1, 2026) - Project Management Refactor: Tuning Log with Mandatory Snapshots
- **Tuning log auto-generation** — Every commit appends a markdown section to `TUNING_LOG.md` with version name, description, table change summary (count + direction: ↑/↓/→/~ with avg %), based-on reference, ROM filename, and a "Results" placeholder. Header written on project creation with vehicle/ECU/checksum info.
- **Mandatory version snapshots** — Removed optional snapshot checkbox from commit flow. Every commit always creates `v{N}_{ROMID}_{name}.bin`. Version name is required and auto-sanitized (lowercase, spaces→underscores, strip special chars).
- **Soft delete versions** — `soft_delete_version()` moves snapshot to `_trash/`, marks `deleted=True` in commits.json. Cannot delete v0.
- **Revert to version** — `revert_to_version()` loads snapshot bytes, overwrites working ROM, soft-deletes all newer versions. Appends revert entry to tuning log.
- **Simplified working ROM naming** — Changed from `v1_{ROMID}_working.bin` to `{ROMID}.bin`. Old projects still work (backward compat via `project.json.working_rom` field).
- **Removed `--enable-projects` feature flag** — Projects are now always enabled. Removed `projects_enabled` from main.py, session_mixin.py, recent_files_mixin.py, settings_dialog.py, run-dev.bat. All project menu items always visible.
- **Commit dialog redesigned** — Single required version name field with auto-sanitization and real-time filename preview. Optional message field. Removed snapshot checkbox, suffix field, and `QuickCommitDialog`.
- **History viewer enhancements** — Added "Revert to this version" and "Delete this version" buttons. Deleted commits hidden by default with "Show deleted" toggle. Deleted items shown with strikethrough + gray when toggled on.
- **Version model cleanup** — Added `deleted: bool` to Commit dataclass. Removed `last_suffix` and `settings` from Project dataclass.
- **37 new tests** — Full coverage for: create_project (working ROM naming, tuning log, v0), commit_changes (snapshot, tuning log, direction, sequential versions), soft_delete (trash, marks, persistence, guards), revert (overwrite, cascade delete, log, v0, monotonic), backward compat, commit dialog sanitization.
- **History viewer polish** — Snapshot filename as primary column (removed Version+Message columns), "Show deleted" toggle, read-only CompareWindow for version diffs (single instance, parented to history dialog so it appears on top), git-log style toolbar icon for version history (enabled when project is open).
- **Default author to system user** — `Commit.create()` uses `os.getlogin()` instead of hardcoded "User".
- **Window geometry persistence** — History viewer and compare window save/restore size, splitter position, and column widths via QSettings. History viewer uses `done()` override (not `closeEvent`) since `accept()` doesn't trigger `closeEvent` on modal dialogs.
- **Commit clears modified flag** — `document.set_modified(False)` called after successful commit so the close prompt doesn't ask about already-committed changes.
- **Commit message preserves line breaks** — Newlines in commit messages rendered as `<br>` in the HTML details view.

## Recent Completed Work (Mar 1, 2026) - RomDrop Setup Wizard & Definitions → Metadata Rename
- **RomDrop setup wizard** — Rewrote `setup_wizard.py` from single-page definitions directory picker to two-step QWizard: Step 1 selects RomDrop installation folder (validates romdrop.exe + metadata/ presence), Step 2 confirms derived paths with green/red status indicators and editable fields. Saves both `romdrop_executable_path` and `metadata_directory` on completion.
- **Renamed "definitions" → "metadata" across codebase** — Renamed `get_definitions_directory()`/`set_definitions_directory()` → `get_metadata_directory()`/`set_metadata_directory()` in settings.py. Updated QSettings key from `paths/definitions_directory` to `paths/metadata_directory`. Default path changed from `definitions/` to `examples/metadata/`. Updated all callers: main.py, session_mixin.py, settings_dialog.py, project_wizard.py, rom_detector.py, rom_context.py, server.py. MCP CLI flag renamed `--definitions-dir` → `--metadata-dir`.
- **Restructured project directories** — Moved `definitions/lf9veb.xml` to `examples/metadata/lf9veb.xml`. Deleted `definitions/` directory. Updated packaging spec, test fixtures, README project tree.

## Recent Completed Work (Mar 1, 2026) - Configurable CSV Export Directory
- **Configurable export directory** — Added "Export Directory" setting (Settings > General) with browse button. CSV exports (Ctrl+E) default to `%APPDATA%/NCFlash/exports` (or platform equivalent). Configurable to any folder.
- **Projects UI hidden behind feature flag** — Projects directory setting in Settings > General and the View menu (which only contained "Commit History") are now hidden unless `--enable-projects` is passed

## Recent Completed Work (Mar 1, 2026) - Table Browser & run.sh Fixes
- **"Modified only" filter auto-expands categories** — Table browser now expands category folders when "Modified only" filter is active, matching search filter behavior
- **run.sh CLI argument passthrough** — Added `"$@"` to `python3 main.py` call in `run.sh` to match `run.bat`'s `%*`

## Recent Completed Work (Mar 1, 2026) - README & Project Cleanup
- **README Linux install docs** — Added Linux `.tar.gz` download/extract instructions alongside Windows in Installation section
- **Project structure reorganization** — Moved build files (`build.bat`, `installer.iss`, `NCFlash.spec`, `requirements-build.txt`) to `packaging/` directory; moved `WINDOWS_SETUP.md` to `docs/`; updated all references in CI, build scripts, and README
- **WINDOWS_SETUP.md cleanup** — Fixed hardcoded paths, removed WSL-specific dev notes
- **Junk file cleanup** — Deleted `nul` (Windows artifact) and `testsguitemp_screenshot.txt`; added `nul` to `.gitignore`

## Recent Completed Work (Mar 1, 2026) - Linux Release Build
- **Linux build in release pipeline** — Added `build-linux` job to `release.yml` (ubuntu-22.04, PyInstaller → tar.gz). Release job now collects artifacts from both Windows and Linux builds. Cross-platform `NCFlash.spec` (conditional icon). Tests use dedicated port 18766 to avoid conflicts with running app.

## Recent Completed Work (Mar 1, 2026) - CI Pipeline Fix
- **Fixed CI pipeline** — Relaxed `numpy>=2.4.0` → `numpy>=2.2.0` (Python 3.10/3.11 support), ran `black` on 63 files, optimized CI matrix from 9→4 jobs (Ubuntu 3.10+3.12, Windows 3.12, macOS 3.12). Lint job updated to Python 3.12, codecov trigger updated to match.

## Recent Completed Work (Feb 28, 2026) - Live App Bridge & AI Write
- **Command API server** — `src/api/command_server.py` runs an HTTP server on daemon thread (127.0.0.1:8766) that bridges MCP requests to Qt main thread via `queue.Queue` + `QTimer` (50ms poll). Handles POST to `/api/read-table`, `/api/modified`, `/api/edit-table`. Auto-starts/stops with MCP server. No new dependencies.
- **AI write access to ROM tables** — `write_table` MCP tool sends cell edits through the full app pipeline: undo tracking (`table_undo_manager.record_bulk_cell_changes`), change tracking (`change_tracker.record_pending_bulk_changes`), ROM write (`rom_reader.write_cell_value`), modified flag, cell border highlighting, and table viewer refresh. Values are in display units; conversion to raw via `ScalingConverter.from_display()`.
- **Live table reading** — `read_live_table` MCP tool reads from the app's in-memory `RomReader` (includes unsaved edits) instead of disk. `list_modified_tables` queries `change_tracker._pending` for modified table names and change counts.
- **`_handle_api_request()` dispatcher in main.py** — Routes API requests to `_api_list_modified`, `_api_read_table`, `_api_edit_table`. The `_api_edit_table` handler follows the same pattern as `apply_compare_copy` (undo, change tracker, ROM write, border tracking, viewer refresh).
- **`_post_to_app()` in rom_context.py** — MCP server reads `command_api_url` from `workspace.json`, POSTs to it with `urllib.request`. 10s timeout. Graceful errors for missing workspace, connection refused, timeout.
- **12 new tests** in `tests/test_command_server.py` — 7 for CommandServer HTTP mechanics (start/stop, POST routing, 404/405/400 errors, callback exception), 5 for RomContext live bridge (no workspace, connection refused, delegation tests).

## Recent Completed Work (Feb 28, 2026) - Workspace State File & MCP Toggle
- **Workspace state file for MCP auto-discovery** — App writes `workspace.json` to project root listing open ROMs (path, file_name, xmlid, make/model/year, is_modified, active_rom). Written on ROM open, close, save, and project open. Deleted on app exit. MCP server reads it via new `get_workspace()` tool in `rom_context.py` and `server.py`. Graceful fallback when file missing or corrupt. 3 new tests in `test_mcp_server.py`. File gitignored.
- **MCP server toggle in app** — Tools menu checkable action + toolbar button (broadcast antenna icon, green when on) to start/stop MCP server subprocess. Uses **SSE transport** on `http://127.0.0.1:8765/sse` so any MCP client can connect. "Start MCP server on startup" checkbox in Settings > Tools. Server auto-stopped on app exit via `_handle_close()`. Methods: `_start_mcp_server()`, `_stop_mcp_server()`, `_toggle_mcp_server()`, `_update_mcp_ui()`, `_is_mcp_running()`.
- **MCP server SSE transport** — `server.py` refactored to support `--transport stdio|sse` and `--port` flags. `_create_mcp()` factory builds the FastMCP instance with all tools. STDIO remains default for CLI clients (`.mcp.json`); SSE used when app starts the server. Fixed latent bug: `description=` kwarg replaced with `instructions=` (correct FastMCP 1.26 param).

## Recent Completed Work (Feb 28, 2026) - MCP Server
- **MCP server for AI assistant access** — Read-only Model Context Protocol server (`src/mcp/`) with 5 tools: `get_rom_info`, `list_tables`, `read_table`, `compare_tables`, `get_table_statistics`. STDIO transport, LRU-cached ROM loading (4 entries), no Qt dependency. Works with Claude Code (`.mcp.json`), Claude Desktop, ChatGPT, Gemini. 34 unit tests in `tests/test_mcp_server.py`. Added `mcp>=1.0.0,<2.0.0` to requirements.txt.

## Recent Completed Work (Feb 28, 2026) - Flash ROM via RomDrop
- **Flash ROM to ECU via RomDrop** — Added "Flash ROM to ECU..." action (Ctrl+Shift+F) in Tools menu and toolbar (lightning-bolt icon). Shows safety warning dialog before flashing (engine off, battery healthy, don't interrupt). Auto-saves unsaved changes ("Save and Flash" vs "Flash"). Launches `subprocess.Popen([romdrop.exe, rom_file], cwd=romdrop_dir)` with resolved absolute path. RomDrop executable path configurable in Settings → Tools tab.
- **README disclaimer** — Added prominent vibe-coded / use-at-your-own-risk notice at the top of README.md.

## Recent Completed Work (Feb 28, 2026) - Windows Packaging
- **PyInstaller packaging support** — Added `src/utils/paths.py` with `get_app_root()` that resolves `sys._MEIPASS` when frozen or `Path(__file__)` tree when running from source. Replaced all 4 `Path(__file__).parent.parent.parent` references in `settings.py` with `get_app_root()`. Created `NCFlash.spec` (one-dir, windowed, bundles definitions/colormaps/examples, excludes tkinter/test/unittest), `build.bat` (activates venv, installs pyinstaller, runs build), and `requirements-build.txt` (pyinstaller>=6.0,<7.0).

## Recent Completed Work (Feb 28, 2026) - Unified Open Action
- **Unified "Open" action** — Replaced separate "Open Project..." (folder picker) and "Open ROM..." (file picker) menu items with a single "Open..." (Ctrl+O) that shows a file picker. If the selected ROM's parent directory is a project folder (`project.json` present), opens as project via `open_project_path()`; otherwise opens as standalone ROM. Toolbar button updated to match. Removed `open_project()` from `ProjectMixin`.
- **`--enable-projects` feature flag** — All project UI (New Project, Commit Changes, Commit History, project auto-detection in Open/Save/session restore/recent files) is hidden unless `--enable-projects` is passed on the command line. `run.bat` passes args through (`%*`); `run-dev.bat` launches with the flag enabled.

## Recent Completed Work (Feb 27, 2026) - Project Management UI Fixes
- **ROM comparison NaN filter** — `_compute_diffs()` now skips tables where both sides (or a one-sided table) have all-NaN values, preventing unpatched ROM tables from cluttering the comparison sidebar.
- **Session restore for projects** — Session save uses `document.project_path` to detect project tabs; stores `project:<path>` entries; restore calls `open_project_path()` to reopen with full project context (`[P]` prefix, history, etc.).
- **Project tab color swatch** — `open_project_path()` calls `_assign_rom_color()` and `_create_tab_color_button()` so project tabs get the same color swatch as standalone ROM tabs.
- **Flat project structure** — Projects no longer create `history/` subfolder. `commits.json` and all snapshots live at project root. Backward compat: `_load_commits()` and `get_snapshot_path()` fall back to `history/` paths for old projects.
- **v0/v1 project layout** — `create_project()` creates `v0_{romid}_original.bin` (pristine, never modified) and `v1_{romid}_working.bin` (editable copy). No more `original.bin` or `modified.bin`.
- **New project gets [P] prefix** — `new_project()` now calls `open_project_path()` instead of `_open_rom_file()`, so newly created projects get the `[P]` tab prefix, color swatch, and recent files entry.
- **Projects in recent files** — `open_project_path()` adds `project:<path>` to recent files. `RecentFilesMixin` displays them as `[P] folder_name` and opens via `open_project_path()`.
- **RomDocument.project_path** — New attribute tracks project association per-document (used by session save and recent files).
- **Fixed closeEvent MRO bug** — `SessionMixin.closeEvent` was shadowed by `QWidget.closeEvent` (C++ slot) in Python's MRO, meaning session data was *never* saved on app close. Renamed to `_handle_close()` with an explicit `MainWindow.closeEvent` override that delegates to it. Added MRO regression tests.
- **Legacy session/recent data handling** — `_restore_session()` and `open_recent_file()` detect ROM files inside project folders (parent has `project.json`) and open them as projects. Covers stale QSettings data from before project-aware code.

## Environment Notes
- Use `python` not `python3` (Windows environment)

## Recent Completed Work (Feb 24, 2026) - Compare Window Fixes
- **Cell border highlighting after compare copy** — `apply_compare_copy()` now updates `self.modified_cells[rom_path]` for both cell and axis changes, so `ModifiedCellDelegate` draws borders on copied cells. Also refreshes open table viewer windows via bulk `update_cell_value()` calls.
- **Copy buttons moved between panels** — Copy table buttons (→| and |←) moved from the toolbar to a narrow centered column between the two table panels, vertically centered. Fixed-width 32px column in the QSplitter, non-collapsible.

## Recent Completed Work (Feb 24, 2026) - Compare Window Enhancements
- **Panel labels show ROM filenames** — Replaced generic "Original"/"Modified" labels above compare panels with actual ROM filenames.
- **Copy table between ROMs** — Two toolbar buttons (→| and |←) to copy a table's values from one ROM to the other. Routes through `MainWindow.apply_compare_copy()` which uses the full edit pipeline: undo support, change tracker, modified indicator (*) on tab, pink table highlighting in browser, cell-level modification tracking. Confirmation dialog before copy. Disabled for one-sided tables and shape mismatches.
- **Main window toolbar** — Added 4 quick-access buttons: Open ROM, Save, Compare, Settings. Programmatic QPainter icons (high-DPI aware).
- **Tools menu** — Replaced single-item "Compare" menu with "Tools" menu.

## Recent Completed Work (Feb 23, 2026) - Cross-Definition ROM Comparison
- **Enabled cross-definition ROM comparison** — Removed the xmlid gate that blocked comparing ROMs with different definitions (e.g., NC1 vs NC2). `CompareWindow` now accepts two separate `RomDefinition` objects (one per ROM) and uses name-based table matching. Features: one-sided tables (A-only/B-only) shown with one panel populated and the other cleared, shape mismatches display each panel at native shape with all cells highlighted, sidebar labels include ◀/▶/≠ indicators, status bar shows context-appropriate messages. Each side uses its own definition for scaling, formatting, axis ranges, and flip flags — no cross-contamination. Window title shows both xmlids when definitions differ.

## Recent Completed Work (Feb 23, 2026) - ROM Comparison Tool
- **Added ROM comparison tool** — New `CompareWindow` (`src/ui/compare_window.py`) provides side-by-side table comparison between two open ROMs. Features: category tree sidebar listing modified tables with change counts, synchronized scrolling between original and modified panels, changed cells highlighted with gray border (matching `ModifiedCellDelegate` pattern), "Changed only" toggle that dims unchanged cells, keyboard navigation (Up/Down to switch tables, T to toggle, Esc to close). Window supports maximize. Compact "Original"/"Modified" labels above each table panel. Accessible via Compare > Compare Open ROMs (Ctrl+Shift+D). Supports 1D, 2D, and 3D table types with proper axis display, flip handling, and thermal gradient coloring. Spec at `docs/specs/ROM_COMPARISON_TOOL.md`, HTML mockups at `docs/mockups/`.

## Recent Completed Work (Feb 22, 2026) - Table Viewer Toolbar
- **Added action toolbar to table viewer window** — 12 quick-access buttons below the menu bar with programmatic QPainter icons (high-DPI aware). Grouped by function: File (clipboard, export CSV), Basic edits (increment, decrement), Value ops (add to data, multiply, set value), Interpolation (vertical, horizontal, 2D, smooth), View (graph toggle). Edit actions auto-disabled in diff mode. Graph toggle button syncs checked state with View menu. Toolbar height accounted for in auto-sizing.

## Recent Completed Work (Feb 10, 2026) - Table Viewer Auto-Size Fix
- **Fixed table viewer window not showing all rows for 3D tables** — `_auto_size_window()` rewritten to use `header.length()` API instead of manual row/column iteration. Added one-row-height safety padding to prevent the last row from being clipped behind the horizontal scrollbar (the scrollbar `sizeHint()` underreports actual size on themed/high-DPI systems). Also subtracts 40px from available geometry for OS window frame. Verified on 1D, 2D, 3D, and large 3D tables.

## Recent Completed Work (Feb 10, 2026) - Graph Performance Optimization
- **Removed constrained_layout from GraphWidget Figure** — `layout='constrained'` was broken for 3D axes (warning: "axes sizes collapsed to zero"), adding ~200ms overhead per draw for a failing constraint solver. Removed in favor of default layout. Canvas.draw() dropped from ~380ms to ~220ms.
- **Vectorized `_calculate_colors()` and `_calculate_colors_1d()`** — Replaced per-cell Python loop (O(rows*cols) function calls through 3 layers of indirection) with numpy array operations: batch ratio-to-index mapping + LUT lookup. Color computation dropped from ~6-30ms to ~1ms.
- **In-place facecolor update for selection changes** — Added `_update_3d_facecolors()` that calls `set_facecolors()` on the existing Poly3DCollection instead of removing and recreating the surface. Selection update pre-draw cost dropped from ~60-100ms to ~1-2ms.
- **Net result (29x25 table, 725 cells):** Initial graph open 764ms→419ms (**45% faster**), selection update 275-342ms→142-157ms (**55% faster**).

## Recent Completed Work (Feb 7, 2026) - Post-Remediation Re-Audit
- **Comprehensive re-audit of all 40 findings** — Systematically verified every fix by reading source files. All 40 fixes confirmed in place. No regressions introduced by the remediation work. Updated `docs/CODE_AUDIT_REPORT.md` with: all items moved to DONE table, updated scores (Maintainability 9/10, Reliability 9/10, Test Quality 7/10, Performance 8/10, Security 9/10), Post-Remediation Notes section assessing new code (mixins, context manager, vectorized scaling, deferred init), and updated priority list for future improvements.
- **Test suite results:** 240 passed, 1 skipped, 1 failed (platform-specific path normalization in `test_custom_definitions_dir` — Windows backslash vs forward slash, not a production bug).

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #19, #20
- **Deferred heavy init work to `_deferred_init()` (#19)** — `MainWindow.__init__` was synchronously doing file I/O, modal dialogs, XML parsing, ROM detection, and session restore, blocking startup. Moved `check_definitions_directory()` + `show_setup_wizard()`, `RomDetector` initialization, `log_startup_message()`, and `_restore_session()` to `_deferred_init()` called via `QTimer.singleShot(0, ...)`. Set `self.rom_detector = None` initially; it's already handled as None by `_open_rom_file()`.
- **Extracted 3 mixin classes from MainWindow (#20)** — Reduced `main.py` from ~1,513 lines / 44 methods to ~1,104 lines / 33 methods. Created: `RecentFilesMixin` (3 methods: `update_recent_files_menu`, `open_recent_file`, `clear_recent_files`) in `src/ui/recent_files_mixin.py`; `ProjectMixin` (5 methods: `new_project`, `open_project`, `commit_changes`, `show_history`, `_on_view_table_diff`) in `src/ui/project_mixin.py`; `SessionMixin` (5 methods: `_restore_session`, `closeEvent`, `show_settings`, `on_settings_changed`, `show_about`) in `src/ui/session_mixin.py`. MainWindow now inherits from `QMainWindow, RecentFilesMixin, ProjectMixin, SessionMixin`.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #30
- **Extracted header resize mode save/restore into `frozen_table_updates` context manager (#30)** — The pattern of saving per-section header resize modes, setting them to Fixed for bulk operations, and restoring them in a finally block was duplicated 8 times across `operations.py` (2), `clipboard.py` (1), `interpolation.py` (3), and `display.py` (2 in `begin/end_bulk_update`). Created `frozen_table_updates()` context manager and `save_header_resize_modes()`, `set_headers_fixed()`, `restore_header_resize_modes()` helper functions in `context.py`. Replaced all 6 inline try/finally patterns with `with frozen_table_updates(...)`, and refactored `begin_bulk_update`/`end_bulk_update` in `display.py` to use the shared helpers. Removed now-unused `QHeaderView` imports from `operations.py`, `clipboard.py`, and `interpolation.py`.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #39
- **Moved function-level imports to module level (#39)** — Moved `import matplotlib.pyplot as plt` in `graph_viewer.py`, `from pathlib import Path` in `version_models.py`, `QPainter/QFontMetrics/QSize` in `table_viewer.py`, `ToggleSwitch` in `table_viewer.py`, `QSize` in `toggle_switch.py`, `from simpleeval import simple_eval` in `editing.py`, and `AddValueDialog/MultiplyDialog/SetValueDialog` in `operations.py` from inside function bodies to module-level imports. Left `from .settings import get_settings` lazy in `colormap.py` (test mock compatibility).

## Recent Completed Work (Feb 7, 2026) - Audit Fix #29
- **Eliminated redundant graph draw calls on cell/axis edits (#29)** — When editing a cell or axis, `_on_cell_changed`/`_on_bulk_changes`/`_on_axis_changed`/`_on_axis_bulk_changes` in `table_viewer_window.py` called `_refresh_graph()` directly AND the selection-change signal also fired a second debounced draw. Changed all four handlers to use `_schedule_graph_refresh()` (50ms debounce) instead of direct calls, and added `self._selection_timer.stop()` inside `_refresh_graph()` so that when the data-refresh fires, it cancels any pending selection-only timer. Result: one draw per user action instead of two.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #18
- **Fixed `clear_all()` not freeing undo stacks from QUndoGroup (#18)** — `remove_stack()` was already fixed in a prior session (composite keys + `deleteLater()`), but `clear_all()` had the same leak: it called `stack.clear()` without `stack.deleteLater()`, leaving QUndoStack objects registered in the QUndoGroup. Added `self._undo_group.setActiveStack(None)` and `stack.deleteLater()` to match `remove_stack()` cleanup behavior.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #32
- **Fixed test state leaking across tests (#32)** — `tests/test_colormap.py` mutated `ColorMap._builtin_gradient` and `colormap_module._current_colormap` without cleanup; `tests/test_settings.py` mutated `settings_module._settings` without cleanup. Added `@pytest.fixture(autouse=True)` fixtures in both files that save original values before each test and restore them in teardown, preventing order-dependent failures.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #26, #34
- **Fixed QAction memory leak in recent files menu (#26)** — `update_recent_files_menu()` in `main.py` called `removeAction()` but never deleted the old QAction objects, leaking them (and their lambda connections) on every menu rebuild. Added `action.deleteLater()` before clearing the list.
- **Fixed `sys.exit(1)` in MainWindow constructor bypassing Qt cleanup (#34)** — When the user cancels the setup wizard, `sys.exit(1)` was called directly inside `__init__`, bypassing Qt's cleanup sequence. Replaced with `QTimer.singleShot(0, lambda: sys.exit(1))` plus `return` to defer the exit to the event loop, allowing Qt to finish construction and clean up properly.
- **Audit finding #17 already fixed** — `modified_cells` and `original_table_values` are already cleaned in `close_tab()` (lines 461-463) via `.pop(rom_path, None)` calls added in an earlier fix.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #22, #27
- **Fixed `_display_3d` per-cell signal storm (#22)** — `_display_3d` in `display.py` calls `setItem()` cell-by-cell, each firing internal model signals causing the view to repaint per cell. Wrapped the entire cell-population block in `blockSignals(True)` / `setUpdatesEnabled(False)` with a single `viewport().update()` at the end for one batched repaint.
- **Added user warning when interpolation skips cells due to missing scaling (#27)** — All three interpolation methods (`interpolate_vertical`, `interpolate_horizontal`, `interpolate_2d`) in `interpolation.py` now check upfront whether the table's scaling is defined and resolvable. If not, a `QMessageBox.warning()` informs the user that interpolation was skipped and why. Previously, the operation silently did nothing.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #16
- **Vectorized scaling expressions (#16)** — `ScalingConverter` in `rom_reader.py` now pre-compiles scaling expressions (via `ast` validation + `compile()`) into numpy-compatible code objects at init time. Array evaluation uses a single `eval()` call on the whole numpy array instead of per-element `simple_eval` loops. Falls back to per-element `simple_eval` for expressions that fail AST safety checks or vectorized eval. Added `_is_safe_numpy_expr()` (AST whitelist: only arithmetic ops + `x` variable) and `_compile_numpy_expr()` helpers.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #31
- **Mitigated lxml XXE (XML External Entity) injection** — All `etree.parse()` calls across the codebase now use a secure parser with `resolve_entities=False` and `no_network=True`. Fixed in `definition_parser.py`, `rom_detector.py`, and `metadata_writer.py` (two call sites). Prevents crafted XML definition files from reading local files or making network requests via entity expansion.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #23
- **Fixed O(N) `get_commit()` calls per keystroke in history filter** — `_filter_commits` in `history_viewer.py` was calling `self.project_manager.get_commit(commit_id)` for every tree item on every keystroke. The commit object was already stored in the item at `Qt.UserRole + 1` (set in `_add_commit_item`). Changed to `item.data(0, Qt.UserRole + 1)` to use the stored data directly, eliminating the redundant lookups.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #33, #35, #37
- **Cached O(n) table lookups (#33)** — `get_tables_by_category()` and `get_table_by_name()` in `rom_definition.py` now build lazy lookup dicts on first access and return cached results on subsequent calls. Cache fields use `field(init=False, repr=False, compare=False)` to stay invisible to dataclass construction and equality.
- **Full UUID for commit IDs (#35)** — `Commit.create()` in `version_models.py` now uses `uuid.uuid4().hex` (32 hex chars) instead of `str(uuid.uuid4())[:12]` (12 chars). The full hex string provides proper collision resistance.
- **Cross-platform monospace font (#37)** — Renamed `LOG_CONSOLE_FONT_FAMILY` to `LOG_CONSOLE_FONT_FAMILIES` in `constants.py`, changed from a single `"Courier New"` string to a tuple of fallbacks `("Consolas", "Courier New", "DejaVu Sans Mono", "Monospace")`. Updated `log_console.py` to use `QFont.setFamilies()` for proper cross-platform font resolution.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes #36, #38
- **Fixed `setup_logging()` at import time (#36)** — Removed the module-level `setup_logging()` auto-call at the bottom of `logging_config.py`. The entry point in `main.py` already calls `setup_logging()` explicitly, so the import-time call was clobbering any pre-existing logging configuration.
- **Implemented backup file rotation (#38)** — `metadata_writer.py` now keeps the last 3 backups using `.bak.1`, `.bak.2`, `.bak.3` naming (`.bak.1` = most recent). Before creating a new backup, existing ones are rotated (delete `.bak.3`, rename `.bak.2`->`.bak.3`, `.bak.1`->`.bak.2`, current->`.bak.1`). Updated test to match new naming and added `test_backup_rotation` test.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #28
- **Fixed `closeEvent` not checking for unsaved changes across tabs** — When the user closed the main window (X button), it bypassed the per-tab unsaved-change prompts and silently discarded all work. Modified `closeEvent` in `main.py` to iterate through all open tabs, check `is_modified()` on each, and prompt Save/Discard/Cancel. If the user cancels on any tab, the close is aborted via `event.ignore()`. Session save (`_save_session`) still runs if the user proceeds with closing.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #21
- **Fixed `lstrip('0x')` bug in `table_browser.py`** — `lstrip('0x')` strips individual characters (`0` and `x`), not the literal prefix `"0x"`. For example, `"0x0080".lstrip('0x')` returns `"80"` instead of `"0080"`. Replaced both instances (lines 423 and 435 in `select_table_by_address`) with `removeprefix('0x')` which correctly removes only the exact prefix string.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #10
- **Fixed exception handling swallowing programming bugs** — Refactored all 12 `except Exception` blocks in `main.py` to separate expected errors (`RomEditorError` hierarchy) from unexpected exceptions. Expected errors (e.g., `RomWriteError`, `RomFileError`, `DetectionError`) get clean user-facing messages via `logger.error()`. Unexpected exceptions now use `logger.exception()` for full traceback logging plus a generic "Unexpected error" user message that includes the exception type. Added `RomEditorError`, `RomWriteError`, and `ProjectError` to imports.

## Recent Completed Work (Feb 7, 2026) - Multi-ROM Undo Isolation Fix
- **Fixed undo stacks shared across ROMs with same definition** — When two ROMs share the same definition (same ECU type), they have identical table addresses. The undo stacks, change tracker, and table highlighting were all keyed by bare `table_address`, causing: (1) both ROMs' edits going into the same undo stack, (2) closing one ROM destroying the other's undo stacks and pending changes, (3) table highlighting showing modifications from the wrong ROM. Fix: introduced composite keys (`rom_path|table_address`) throughout the undo and change tracking systems. Files modified: `version_models.py` (added `table_key` field to CellChange/AxisChange), `table_undo_manager.py` (composite key helpers, rom_path params), `undo_commands.py` (propagate table_key through undo/redo), `change_tracker.py` (composite keys, per-ROM filtering), `main.py` (pass rom_path to all handlers, per-ROM highlight filtering), `table_viewer_window.py` (emit composite key on focus).

## Recent Completed Work (Feb 7, 2026) - Undo Wrong-ROM Fix
- **Fixed Path vs str type mismatch throughout `main.py`** — `RomReader.rom_path` is `Path`, `RomDocument.rom_path` is `str`; on Windows, forward vs backslash normalization caused `str()` comparison to fail silently. Fixed `_find_document_by_rom_path()` to use `Path()` comparison. Fixed `close_tab()` to use `rom_reader.rom_path` (Path) instead of `document.rom_path` (str) for window matching and dict cleanup.
- **Fixed test runner operations not emitting signals** — `set_value`, `multiply_selection`, `add_to_selection` called `_apply_bulk_operation` directly which doesn't emit `bulk_changes`/`axis_bulk_changes` signals. Now properly emits signals so changes are written to ROM.
- **Fixed undo/edit writing to wrong ROM** when multiple ROMs are open — all 6 `get_current_document()` call sites in edit/undo handlers now resolve the correct ROM via `_find_document_by_rom_path()` instead of using the active tab
- **Clean ROM state on tab close** — closing a ROM now: closes all its table windows, removes undo stacks, clears pending changes, and purges modified_cells/original_table_values for that ROM. Reopening a ROM starts fresh.
- **Debounced graph selection updates** — arrow key navigation no longer triggers full 3D re-render per key press (100ms debounce timer)
- **Eliminated double-draw** in graph widget — `canvas.draw_idle()` + deferred redraw only on first plot

## Recent Completed Work (Feb 7, 2026) - 3D Graph Zoom Fix
- **Fixed 3D graph zoom-out on cell edits and selection changes** — `_refresh_graph()` was calling `set_data()` on every cell change, which did `figure.clear()` → full replot → `constrained_layout` recalculation → visible zoom-out. Changed to `update_selection()` which routes through `_update_3d_surface()` — replaces the surface collection on the existing axes without clearing the figure. Also added `_update_3d_surface` fast path for `update_data` and `update_selection` in GraphWidget. Axis limits saved/restored to prevent auto-rescale.

## Recent Completed Work (Feb 7, 2026) - Audit Fix #11
- **Deduplicated GraphWidget and GraphViewer** (~700 lines → ~350 lines) — extracted `_GraphPlotMixin` with 14 shared methods (plotting, colors, axis labels, keyboard rotation/zoom). Both classes now inherit from the mixin, keeping only their unique setup logic. Also fixed minor bug: GraphViewer was not resetting `ax_3d = None` on figure clear, and removed dead `tick_positions` variable in GraphWidget._plot_3d.

## Recent Completed Work (Feb 7, 2026) - Audit Fixes
- **Atomic file writes** for `save_rom`, `_save_project_file`, `_save_commits` — write-to-temp + `os.replace()` prevents corruption on crash
- **Fixed swapxy flatten bug** in `write_table_data` — was using C order instead of F order for swapxy tables, causing silent data corruption on bulk write
- **Fixed paste to use `bulk_changes` signal** — paste now creates a single undo entry instead of N individual entries (one per cell)
- **Memory leak fixes** — added `deleteLater()` in `close_tab`, `WA_DeleteOnClose` on `TableViewerWindow` and `GraphViewer`, matplotlib figure cleanup in `closeEvent`
- **Fixed `rom_document.save()` to clear modified flag** — `set_modified(False)` was missing after successful save
- **Rewrote 3 tautological test files** — `test_axis_editing.py`, `test_interpolation.py`, `test_table_viewer_helpers.py` now import and test actual production code (ScalingConverter, _convert_expr_to_python, swapxy round-trips, atomic writes)
- **Pinned dependency versions** in `requirements.txt` with upper bounds (e.g., `PySide6>=6.10.0,<7.0.0`)
- **Code audit report** saved to `docs/CODE_AUDIT_REPORT.md` (gitignored, personal reference)

## Recent Completed Work (Feb 7, 2026) - Earlier
- Fixed undo/redo performance: ROM data writes were O(N*ROM_size) per operation due to immutable `bytes` concatenation. Changed `rom_data` to `bytearray` for O(1) in-place writes.
- Fixed CTRL+Z not working in newly opened table viewer: `set_active_stack()` failed to create the undo stack on first window focus, so the stack was never activated until the window was closed and reopened.
- Fixed bulk undo/redo performance in main.py: `_update_project_ui()` was called N+1 times during bulk undo (once per cell via `_notify_change` callback + once direct). Added `_in_bulk_undo` guard to both `_on_changes_updated` callback and removed redundant direct call in `_update_pending_from_undo`. Now called exactly once at `_end_bulk_update`.
- Fixed undo stack staying active after closing table viewer window: `closeEvent` now deactivates the undo stack, preventing undo from executing on closed tables.
- Changed min/max coloring to use scaling definition min/max instead of current data values. Applies to table viewer (values + both axes) and graph viewer. Each of the 3 scalings in a 3D table (X axis, Y axis, values) uses its own scaling range.
- Fixed non-uniform graph cell sizes: graphs now use uniform indices for mesh coordinates (all cells same size) with actual axis values as tick labels. Previously, non-uniformly spaced axis values (e.g., RPM) caused edge cells to be thinner.

## Recent Completed Work (Feb 1, 2026)
- Fixed undo/redo performance for bulk operations (matching increment/decrement speed)
  - Root cause: Bulk undo/redo called per-cell updates without batching optimizations
  - Added `begin_bulk_update()` / `end_bulk_update()` methods to TableViewer and TableDisplayHelper
  - These methods disable widget updates, block signals, disable ResizeToContents headers, and cache min/max for color calculations
  - Updated `BulkCellEditCommand` and `BulkAxisEditCommand` to call bulk callbacks before/after applying changes
  - Files modified: `display.py`, `table_viewer.py`, `table_undo_manager.py`, `undo_commands.py`, `main.py`
- Implemented per-table undo/redo using Qt's QUndoGroup pattern
  - Each table now has its own undo stack (undo in Table A only affects Table A)
  - Created `src/core/undo_commands.py` - QUndoCommand subclasses (CellEditCommand, BulkCellEditCommand, AxisEditCommand, BulkAxisEditCommand)
  - Created `src/core/table_undo_manager.py` - Manages QUndoGroup and per-table QUndoStacks
  - Refactored `src/core/change_tracker.py` - Now only handles pending changes for commit tracking
  - Updated `main.py` - Integrated TableUndoManager, QUndoGroup-based menu actions
  - Updated `table_viewer_window.py` - Shortcuts route to main window's undo group
  - Added `focus_table` command to test_runner.py for switching between open tables
  - Added `tests/test_table_undo_manager.py` - 11 unit tests for per-table undo
  - Added `tests/gui/test_per_table_undo.txt` - GUI test script

## Recent Completed Work (Jan 31, 2026)
- Fixed major performance issue with bulk cell editing - operations that changed hundreds of cells were slow due to widget repainting on every cell update
  - Wrapped all bulk operations with `setUpdatesEnabled(False)` before processing and `setUpdatesEnabled(True)` with single `viewport().update()` after
  - Fixed in: `operations.py` (apply_bulk_operation, smooth_selection), `clipboard.py` (paste_selection), `interpolation.py` (all three interpolation methods)
  - Performance improvement: from hundreds of repaints to a single repaint at the end
- Fixed undo/redo to only apply to the focused table viewer window
  - Modified `_apply_cell_change()` and `_apply_axis_change()` in main.py to check `window.isActiveWindow()` before applying changes
  - Prevents undo/redo from affecting the wrong window when multiple table viewers are open

## Recent Completed Work (Jan 18, 2026)
- Fixed blank space under table cells: set QTableWidget size policy to prevent vertical expansion beyond content
- Fixed Windows-only blank space issue: added post-resize correction for high-DPI displays (detects/removes viewport blank space)

## Recent Completed Work (Jan 17, 2026)
- Added focus/highlight selected table feature: clicking a table viewer window now highlights and scrolls to that table in the tree view
- Standardized directory naming to plural convention: `colormap/` -> `colormaps/`, `metadata/` -> `definitions/` (kept `src/` as-is per Python convention)
- Fixed logging handler MRO conflict in `log_console.py` (QObject.emit vs logging.Handler.emit)
- Attempted PyQtGraph migration but reverted - OpenGL requires desktop environment, creating two implementations wasn't worth the maintenance burden
- Documented UI testing tools in `docs/UI_TESTING.md`
- Added UI Testing section to `CLAUDE.md` with rules for screenshot/testing scenarios
- Added `rotate_graph <elev> <azim>` command to test_runner.py

## Recent Completed Work (Jan 16, 2026)
- Added Copy Table to Clipboard (Ctrl+Shift+C) and Export to CSV (Ctrl+E)
- Added Smooth Selection (S) for light neighbor-based smoothing
- Removed graph widget and "Value" label for 1D tables
- Hidden View menu for 1D tables (when not in diff mode)
- Added graph auto-refresh on data changes
- Fixed undo/redo graph refresh with debouncing (50ms timer)
