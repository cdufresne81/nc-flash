#!/bin/bash
# check-changelog-staged.sh
# Claude Code PreToolUse hook: blocks git commit if CHANGELOG.md is not staged.
# Receives JSON on stdin from Claude Code with tool_input.command and cwd fields.

set -euo pipefail

input=$(cat)

# Decide whether the command is a `git commit` (also `git -C <dir> commit`) and
# which repo it commits to: the `git -C <dir>` or `cd <dir>` target if the
# command has one (worktrees), else the hook's cwd. Prints three lines: 1 or 0,
# the repo, then 1 if it is `--amend`. Python is guaranteed available; jq may
# not be on Windows.
parse='
import json, shlex, sys
data = json.loads(sys.argv[1])
command = data.get("tool_input", {}).get("command", "")
repo = data.get("cwd") or "."
try:
    words = shlex.split(command, posix=True)
except ValueError:
    words = command.split()
words = [w.rstrip(";|&") for w in words]
is_commit = False
amend = False
for i, word in enumerate(words):
    if word == "cd" and i + 1 < len(words):
        repo = words[i + 1]
    elif word == "git":
        j = i + 1
        while j + 1 < len(words) and words[j] in ("-C", "-c"):
            if words[j] == "-C":
                repo = words[j + 1]
            j += 2
        if j < len(words) and words[j] == "commit" and "--help" not in words[j:]:
            is_commit = True
            amend = "--amend" in words[j:]
            break
print(1 if is_commit else 0)
print(repo)
print(1 if amend else 0)
'
parsed=$(python -c "$parse" "$input" 2>/dev/null || python3 -c "$parse" "$input" 2>/dev/null || echo 0)
is_commit=$(echo "$parsed" | head -n 1)
repo=$(echo "$parsed" | sed -n 2p)
amend=$(echo "$parsed" | sed -n 3p)

# Only check git commit commands
if [[ "$is_commit" != "1" ]]; then
    exit 0
fi

# Check if CHANGELOG.md is in the staging area of the repo being committed to.
# An amend replaces HEAD, so compare against HEAD's parent: the amended commit
# passes if it changes CHANGELOG.md.
base=HEAD
if [[ "$amend" == "1" ]]; then
    base=HEAD~1
fi
if git -C "${repo:-.}" diff --cached --name-only "$base" 2>/dev/null | grep -q "^CHANGELOG.md$"; then
    exit 0
else
    echo "CHANGELOG.md is not staged in ${repo:-.}. Update the [Unreleased] section before committing, then: git add CHANGELOG.md" >&2
    exit 2
fi
