#!/bin/bash
# check-changelog-staged.sh
# Claude Code PreToolUse hook: blocks git commit if CHANGELOG.md is not staged.
# Receives JSON on stdin from Claude Code with tool_input.command field.

set -euo pipefail

input=$(cat)

# Extract the command using Python (guaranteed available, jq may not be on Windows)
command=$(python -c "
import sys, json
data = json.loads(sys.argv[1])
print(data.get('tool_input', {}).get('command', ''))
" "$input" 2>/dev/null || python3 -c "
import sys, json
data = json.loads(sys.argv[1])
print(data.get('tool_input', {}).get('command', ''))
" "$input" 2>/dev/null || echo "")

# Only check git commit commands
if [[ "$command" != *"git commit"* ]] || [[ "$command" == *"--help"* ]]; then
    exit 0
fi

# Check if CHANGELOG.md is in the staging area
if git diff --cached --name-only 2>/dev/null | grep -q "^CHANGELOG.md$"; then
    exit 0
else
    echo "CHANGELOG.md is not staged. Update the [Unreleased] section before committing, then: git add CHANGELOG.md" >&2
    exit 2
fi
