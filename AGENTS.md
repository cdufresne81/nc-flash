# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Session Start Checklist

**EVERY NEW SESSION, you MUST:**

1. **Refresh Repomix** - Get latest codebase snapshot:
   ```bash
   repomix mix
   ```
   This creates/updates `repomix-output.xml` with the entire codebase in AI-friendly format

2. **Check available work:**
   ```bash
   bd ready
   ```

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
repomix mix           # Refresh codebase snapshot for AI consumption
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Refresh Repomix** - Update codebase snapshot for next session:
   ```bash
   repomix mix
   git add repomix-output.xml
   git commit -m "Update repomix snapshot"
   ```
5. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
6. **Clean up** - Clear stashes, prune remote branches
7. **Verify** - All changes committed AND pushed, repomix refreshed
8. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

