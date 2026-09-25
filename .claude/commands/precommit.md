---
allowed-tools: Bash, Read, Grep
description: Validate CHANGELOG.md and run quality gates before committing
---

# Pre-Commit Validation

You are running mandatory pre-commit checks. All steps must pass before committing.

## Current State

- Staged files: !`git diff --cached --name-only 2>/dev/null || echo "ERROR: git diff failed"`
- Python files staged: !`out=$(git diff --cached --name-only -- '*.py' 2>/dev/null) || out="ERROR: git diff failed"; echo "${out:-None}" | head -20`
- Current CHANGELOG Unreleased section: !`python -c "
import re, sys
try:
    with open('CHANGELOG.md', encoding='utf-8') as f:
        text = f.read()
    match = re.search(r'## \[Unreleased\].*?(?=\n## \[|\Z)', text, re.DOTALL)
    out = match.group(0).strip() if match else 'ERROR: No [Unreleased] section found!'
except Exception as e:
    out = 'ERROR: could not read CHANGELOG.md: %r' % (e,)
sys.stdout.buffer.write((out + '\n').encode('utf-8'))
sys.stdout.flush()
" 2>&1 || echo "ERROR: python failed to run the CHANGELOG snippet"`

## Step 1: Code Quality (if Python files are staged)

If there are Python files in the staged changes, run these checks. If no Python files are staged, skip to Step 2.

### 1a: Formatting

```bash
python -m black --check src/ tests/ main.py
```

If black fails, fix with `python -m black src/ tests/ main.py` and re-stage the affected files.

Always invoke the tools as `python -m <tool>` so they come from the project's interpreter. A bare `black` on PATH may be a different, older install that reports false reformat hits.

### 1b: Linting

```bash
python -m flake8 src/ tests/ --count --select=E9,F63,F7,F82 --show-source --statistics
```

This catches syntax errors and undefined names. Must pass with zero errors.

### 1c: Tests

```bash
python -m pytest
```

All tests must pass. If tests fail, investigate and fix before proceeding.

## Step 2: Changelog Structure Check

Verify `CHANGELOG.md` has:
- A `## [Unreleased]` section (REQUIRED)
- At least one subsection: `### Added`, `### Changed`, `### Fixed`, or `### Removed`
- At least one bullet entry under a subsection

If the `[Unreleased]` section is empty or missing, this is a **FAILURE**.

## Step 3: Changelog Content Relevance

Review the staged changes (`git diff --cached`) and verify the Unreleased entries describe the work being committed:

1. Read the staged diff to understand what changed
2. Read the Unreleased entries
3. Check that each significant change has a corresponding changelog entry

**Significant changes**: new features, behavior changes, bug fixes, removed functionality, file renames/moves.
**Skip**: formatting-only, comment-only, test-only changes (unless fixing a bug).

## Step 4: Changelog Format Check

Verify entries follow the project format:
- Each entry starts with `- **Bold summary** — Description`
- Entries are under the correct subsection (Added/Changed/Fixed/Removed)
- No duplicate entries

## Step 5: Staging Check

If CHANGELOG.md has been modified but is NOT staged, stage it:
```bash
git add CHANGELOG.md
```

If CHANGELOG.md needs updates, make them now, then stage.

## Output

Report a summary table of all checks:

| Check | Result |
|-------|--------|
| Black | PASS / SKIP / FAIL |
| Flake8 | PASS / SKIP / FAIL |
| Pytest | PASS / SKIP / FAIL |
| Changelog structure | PASS / FAIL |
| Changelog content | PASS / NEEDS UPDATE |
| Changelog format | PASS / FAIL |
| Changelog staged | PASS / FIXED |

Then a final verdict:
- **PASS**: All checks passed. Ready to commit.
- **NEEDS UPDATE**: List what is missing or failing.
- **FAIL**: Blocking issues that must be fixed.

## Rules

1. If you update CHANGELOG.md, re-read it to verify your changes
2. Never remove existing entries — only add or correct
3. Entries go under `## [Unreleased]`, never under a versioned section
4. After fixing issues, stage with `git add CHANGELOG.md`
5. If black reformats files, re-stage them before reporting