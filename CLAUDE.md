# Claude Code Instructions

# PRIME DIRECTIVE
We work for an hospital and our work is critical, failure to succeed will result in the lost of live, failure is not an option.

## General Rules

- **"Question:" prefix** - If a prompt starts with "Question:", answer only. Take no actions (no file edits, no commands).
- **No auto-commit** - NEVER run `git commit` or `git push` unless the user explicitly asks OR requests to "land the plane" (session completion).
- **Incremental notes** - After completing code changes that add, update, or delete functionality, immediately update the "Recent Completed Work" section in `.claude/notes.md`. Only note meaningful changes (new features, behavior changes, significant fixes). Skip trivial changes (typos, formatting, minor refactors). Always check existing entries to avoid duplicates.
- **Changelog** - `CHANGELOG.md` MUST be updated before every commit. Add entries to the `## [Unreleased]` section using Added/Changed/Fixed/Removed subsections. When a version is tagged, the unreleased section becomes the GitHub release notes. Ensure version sections match actual git tags — never leave released work under Unreleased.
- **Concise, human-friendly release notes** - Because the changelog IS the GitHub release notes, every entry must read as release notes a human can scan, not an engineering log (v2.9.0 is the reference style). Each entry is a bullet with a **plain-language bold headline** stating the user-facing change, followed by a short description of what changed and why it matters. One logical change = one bullet; never a single wall-of-text bullet. Do NOT include: internal audit/task codes (`D1`, `C1/C2`, `H10`, `B8`), `tests/…` file enumerations, agent-workflow meta ("5 review agents", "adversarially verified"), or `.claude/plans/…` pointers. Keep genuinely useful specifics (issue numbers, new setting names/paths, hardware-validation results, key module names). Prefer a few tight sentences over a paragraph; split dev-facing detail into its own bullet marked "(dev-facing)".
- **NEVER commit or push** - Unless the user ask to land the plane or explicitely ask for it.
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

## Session Notes

Check `.claude/notes.md` at the start of each session for:
- Pending tasks from previous sessions
- Important context and decisions

Update this file when ending a session with any important notes for next time.

## Key Documentation

Reference these before modifying related functionality:
- `docs/internal/ARCHITECTURE.md` - Layer map + the architecture rules with the incidents that motivated each; read before adding a cross-layer import, a mixin, a shared-state dict, or a duplicated pipeline. Enforced by `tests/test_architecture.py`
- `docs/internal/LOGGING.md` - Logging configuration and exception hierarchy
- `docs/internal/ROM_DEFINITION_FORMAT.md` - XML format for ROM definitions
- `docs/internal/UI_TESTING.md` - GUI test runner, screenshots, and test scripts
- `docs/internal/CODE_AUDIT.md` - Full codebase audit findings (bugs, dead code, duplication, test gaps)
- `docs/internal/WICAN_TRANSPORT.md` - Design & build plan for WiCAN PRO wireless (WiFi/SLCAN) ECU transport; reference before touching the ECU transport/session/flash-connect layer
- `docs/internal/WICAN_MANUAL_TEST.md` - Hardware-in-the-loop checklist for the WiCAN ROM read path (firmware version ping, bench-tool read + byte-compare, UI flow); run after touching the transport, firmware, or adapter-selector UI
- `docs/internal/WICAN_PART_C_FINDINGS.md` - Investigation findings (CAN-wedge reboot root cause + clean-teardown fix, no-reboot protocol switch, unified read+write SD architecture); reference before implementing the firmware reboot fix or deciding the WiCAN WRITE-over-SD architecture
- `docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md` - Sequencing plan to replace the protocol-switch reboot with an always-on dedicated SLCAN port that coexists with the datalogger (FLASH_ACTIVE_BIT single-CAN interlock, FWD→FWB merge order, RPM-gated datalog/flash); reference before merging the datalogger firmware branch or building the no-reboot SLCAN port
- `docs/internal/WICAN_DEADMAN_AUTORESUME.md` - Validated design for brick-safe datalog auto-resume when NC-Flash vanishes (lid close / crash / Wi-Fi drop): the HOST_BUS_CLAIM_BIT auth-window fence + firmware dead-man reaper, plus the missing #36 RX-forward fix. Reference before touching datalog pause/resume, the `/datalog` endpoint, the FLASH_ACTIVE_BIT/DATALOG_PARK_BIT interlock, or the host flash auth window
- `docs/internal/WICAN_LIVE_STREAM.md` - Design for the live datalog stream (fw issue #3): device TCP port 35002 `NCDLv1` line protocol, firmware csv_logger hook, host `wican_stream.py`/`WiCANLiveDatalog` receiver. Reference before touching the live-tail client, the stream port, or the firmware stream hook

**Rule:** When creating new documentation in `docs/`, add it to this list with a brief description of when to reference it.

## UI Testing & Screenshots

**Tool:** `tools/test_runner.py` - Automated GUI testing with screenshot capabilities

**When to use:**
- User asks to take a screenshot or view the UI
- Debugging or verifying a visual/UI issue
- Testing UI behavior after code changes
- Creating documentation images

**Quick Commands:**
```bash
# Take screenshot of a specific table
python tools/test_runner.py --rom examples/lf9veb.bin --table "Table Name" --screenshot name

# Run a GUI test script
python tools/test_runner.py --script tests/gui/test_name.txt

# Interactive mode for exploration
python tools/test_runner.py --interactive
```

**Screenshot output:** `docs/screenshots/`

**Rules:**
1. When asked to test or screenshot the UI, use `test_runner.py` - do NOT manually automate Qt
2. For visual bug investigation, take screenshots to capture the problematic state
3. Test scripts live in `tests/gui/*.txt` - create new ones for reproducible test cases
4. See `docs/internal/UI_TESTING.md` for full command reference
5. **Always screenshot the full window** (use `table` target, not `graph`) to capture full context — graph-only screenshots miss layout/sizing issues

## Landing the Plane (Session Completion)

When ending a work session, complete ALL steps below. Work is NOT complete until `git push` succeeds.

**Checklist:**

1. **Run quality gates** (if code changed):
   ```bash
   black src/ tests/ main.py
   pytest
   ```

2. **Validate changelog** (if code changed):
   Run `/precommit` to validate CHANGELOG.md is updated and staged, and run quality gates (black, flake8, pytest). A PreToolUse hook will also block `git commit` if CHANGELOG.md is not staged.

3. **Commit and push**:
   ```bash
   git add -A
   git commit -m "Description of changes"
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```

4. **Verify** - All changes committed AND pushed

5. **Update notes** in `.claude/notes.md`:
   - Add any pending tasks or context
   - Apply **Incremental notes** rule for any missed completed work
   - Verify "Recent Completed Work" is complete (incremental notes should have captured most changes - only add missing items, no duplicates)
   - Sanity check `README.md` against recent work - add new features, remove references to deleted functionality

6. **Hand off** - Provide context summary for next session

**Rules:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- If push fails, resolve and retry until it succeeds
