# Claude Code Instructions

# PRIME DIRECTIVE
We work for an hospital and our work is critical, failure to succeed will result in the lost of live, failure is not an option.

## General Rules

- **"Question:" prefix** - If a prompt starts with "Question:", answer only. Take no actions (no file edits, no commands).
- **No auto-commit** - NEVER run `git commit` or `git push` unless the user explicitly asks OR requests to "land the plane" (session completion).
- **Incremental notes** - After completing code changes that add, update, or delete functionality, immediately update the "Recent Completed Work" section in `.claude/notes.md`. Only note meaningful changes (new features, behavior changes, significant fixes). Skip trivial changes (typos, formatting, minor refactors). Always check existing entries to avoid duplicates.
- **Changelog** - When adding features, fixing bugs, or making notable changes, update `CHANGELOG.md` under an `## [Unreleased]` section at the top. When a version is tagged, the unreleased section becomes the release notes on GitHub. Follow the existing format (Added/Changed/Fixed subsections).
- **NEVER commit or push** - Unless the user ask to land the plane or explicitely ask for it.
- **Test coverage** - New features or changes to existing features must be tested. Create tests if none exist AND the behavior is logical and important to verify. Do not write tests for trivial or cosmetic changes.

## Session Notes

Check `.claude/notes.md` at the start of each session for:
- Pending tasks from previous sessions
- Important context and decisions

Update this file when ending a session with any important notes for next time.

## Key Documentation

Reference these before modifying related functionality:
- `docs/LOGGING.md` - Logging configuration and exception hierarchy
- `docs/ROM_DEFINITION_FORMAT.md` - XML format for ROM definitions
- `docs/UI_TESTING.md` - GUI test runner, screenshots, and test scripts

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
4. See `docs/UI_TESTING.md` for full command reference
5. **Always screenshot the full window** (use `table` target, not `graph`) to capture full context — graph-only screenshots miss layout/sizing issues

## Landing the Plane (Session Completion)

When ending a work session, complete ALL steps below. Work is NOT complete until `git push` succeeds.

**Checklist:**

1. **Run quality gates** (if code changed):
   ```bash
   black src/ tests/ main.py
   pytest
   ```

2. **Commit and push**:
   ```bash
   git add -A
   git commit -m "Description of changes"
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```

3. **Verify** - All changes committed AND pushed

4. **Update notes** in `.claude/notes.md`:
   - Add any pending tasks or context
   - Apply **Incremental notes** rule for any missed completed work
   - Verify "Recent Completed Work" is complete (incremental notes should have captured most changes - only add missing items, no duplicates)
   - Sanity check `README.md` against recent work - add new features, remove references to deleted functionality
   - Update `CHANGELOG.md` — add new features/fixes/changes to the `[Unreleased]` section (create it if missing)

5. **Hand off** - Provide context summary for next session

**Rules:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- If push fails, resolve and retry until it succeeds
