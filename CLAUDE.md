# Claude Code Instructions

NC Flash edits and flashes Mazda MX-5 NC ECU ROMs. What we write ends up in a car ECU: a bad write can brick the ECU or damage an engine.

## General Rules

- **"Question:" prefix** - If a prompt starts with "Question:", answer only. Take no actions (no file edits, no commands).
- **Commit/push** - Commit only when the user asks. NEVER push or merge without the user's explicit OK. `master` is protected: work ships via branch + PR, admin-merged only after CI is green and the user says so.
- **Before committing** - Run `/precommit` (black, flake8, pytest, CHANGELOG check). A PreToolUse hook (`.claude/hooks/check-changelog-staged.sh`) blocks `git commit` if CHANGELOG.md isn't staged. If the change adds or removes a user-facing feature, update `README.md` too.
- **Changelog** - `CHANGELOG.md` MUST be updated before every commit. Add entries to the `## [Unreleased]` section using Added/Changed/Fixed/Removed subsections. When a version is tagged, the unreleased section becomes the GitHub release notes. Ensure version sections match actual git tags — never leave released work under Unreleased.
- **PR descriptions, CHANGELOG entries and release notes** - Keep them concise and human-friendly. Write for a user or reviewer, not a developer mid-session: say what changed and why it matters in plain words. Leave out internal names, file paths, test counts and investigation history unless a reader needs them.
- **Session notes** - `.claude/notes.md` holds ONLY open work and live decisions not already tracked in a GitHub issue. Read it at session start. When you leave something unfinished, add it; when an item is done, delete it (completed work goes in CHANGELOG, not notes). Pre-Sep-24-2026 history: `git show f4709f1:.claude/notes.md` (search it, don't read it end to end).
- **Test coverage** - New features or changes to existing features must be tested. Create tests if none exist AND the behavior is logical and important to verify. Do not write tests for trivial or cosmetic changes.

## Architecture Rules (enforced; rationale in `docs/internal/ARCHITECTURE.md`)

Layering: utils ← core ← {ecu, ui, api, mcp}; main.py composes ui.
- NEVER import `src.ui` from `src.core` / `src.ecu` / `src.utils` / `src.api` / `src.mcp`.
- NEVER import `src.ecu` from `src.core` / `src.utils`.
- State has ONE owner. NEVER share a mutable dict/list between objects for mutation; the owner exposes methods + Qt signals.
- NEVER add a mixin. New window-scale behavior = an owned collaborator object.
- ONE pipeline copy. Table rendering, flash-image prep, and HTTP/socket read loops each live in exactly one module. Extend it; never copy it.
- Signals carry their own context (rom_path, …). NEVER recover context via `sender()`/`parent()` walks. Max 2 signal hops.
- Styling: colors/fonts/QSS come from the theme module (`src/ui/theme.py`). No new inline hex literals in widgets.
- `src/ecu/` is brick-critical: behavior-preserving changes only, unless a hardware test per `docs/internal/WICAN_MANUAL_TEST.md` is run.
- `tests/test_architecture.py` enforces the import rules — keep it green.

## Key Documentation

Reference these before modifying related functionality:
- `docs/internal/ARCHITECTURE.md` - Layer map + the architecture rules with the incidents that motivated each; read before adding a cross-layer import, a mixin, a shared-state dict, or a duplicated pipeline. Enforced by `tests/test_architecture.py`
- `docs/internal/LOGGING.md` - Logging configuration and exception hierarchy
- `docs/internal/ROM_DEFINITION_FORMAT.md` - XML format for ROM definitions
- `docs/internal/ROM_COMPARISON_TOOL.md` - Spec for the side-by-side ROM comparison tool; reference before touching compare/diff code
- `docs/internal/WINDOWS_SETUP.md` - Windows dev environment setup
- `docs/internal/UI_TESTING.md` - GUI test runner, screenshots, and test scripts
- `docs/internal/CODE_AUDIT.md` - Codebase audit snapshot from 2026-04-03 (bugs, dead code, duplication, test gaps); predates the Jul 2026 architecture hardening, so verify a finding still holds before acting on it
- `docs/internal/WICAN_TRANSPORT.md` - Design & build plan for WiCAN PRO wireless (WiFi/SLCAN) ECU transport; reference before touching the ECU transport/session/flash-connect layer
- `docs/internal/WICAN_MANUAL_TEST.md` - Hardware-in-the-loop checklist for the WiCAN ROM read path (firmware version ping, bench-tool read + byte-compare, UI flow); run after touching the transport, firmware, or adapter-selector UI
- `docs/internal/WICAN_PART_C_FINDINGS.md` - Investigation findings (CAN-wedge reboot root cause + clean-teardown fix, no-reboot protocol switch, unified read+write SD architecture); reference before implementing the firmware reboot fix or deciding the WiCAN WRITE-over-SD architecture
- `docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md` - Sequencing plan to replace the protocol-switch reboot with an always-on dedicated SLCAN port that coexists with the datalogger (FLASH_ACTIVE_BIT single-CAN interlock, FWD→FWB merge order, RPM-gated datalog/flash); reference before merging the datalogger firmware branch or building the no-reboot SLCAN port
- `docs/internal/WICAN_DEADMAN_AUTORESUME.md` - Validated design for brick-safe datalog auto-resume when NC-Flash vanishes (lid close / crash / Wi-Fi drop): the HOST_BUS_CLAIM_BIT auth-window fence + firmware dead-man reaper, plus the missing #36 RX-forward fix. Reference before touching datalog pause/resume, the `/datalog` endpoint, the FLASH_ACTIVE_BIT/DATALOG_PARK_BIT interlock, or the host flash auth window
- `docs/internal/WICAN_SLCAN_STRAND_INVESTIGATION.md` - Historical (path since removed): why the adapter got stranded in Bench SLCAN mode (#92), with bench evidence; reference only for the reasoning behind the single-mode trim

**Rule:** When creating new documentation in `docs/`, add it to this list with a brief description of when to reference it.

## UI Testing & Screenshots

**Tool:** `tools/test_runner.py` - Automated GUI testing with screenshot capabilities

Use it whenever you test, debug, or screenshot the UI. Full command reference: `docs/internal/UI_TESTING.md`.

```bash
python tools/test_runner.py --rom examples/lf9veb.bin --table "Table Name" --screenshot name
python tools/test_runner.py --script tests/gui/test_name.txt
```

- Do NOT automate Qt by hand; use `test_runner.py`. Screenshots land in `docs/screenshots/`.
- Put reproducible cases in `tests/gui/*.txt`.
- **Always screenshot the full window** (`table` target, not `graph`): graph-only screenshots miss layout/sizing issues.
