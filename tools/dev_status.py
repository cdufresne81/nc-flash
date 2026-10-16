#!/usr/bin/env python3
"""List what is on ``dev`` but not yet on ``master``.

Every change goes to ``dev`` first and reaches ``master`` later through a
second PR from its own branch (docs/internal/RELEASE_CHANNELS.md). This asks
GitHub for the PRs merged into ``dev`` and checks whether each one's head
commit is in ``origin/master`` yet, so there is no hand-kept list to drift.

Prints two lists:
  - awaiting master: merged into dev, not yet in master (open the PR into
    master from that branch once it's tested);
  - reached master: in master, branch still on GitHub (delete it).

Needs ``gh`` (logged in) and a fetchable ``origin``. Read-only.

    python tools/dev_status.py
"""

from __future__ import annotations

import json
import subprocess
import sys


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8")


def _git_ok(*args: str) -> bool:
    return _run("git", *args).returncode == 0


def main() -> int:
    fetch = _run("git", "fetch", "--quiet", "--prune", "origin")
    if fetch.returncode != 0:
        print(f"git fetch failed: {fetch.stderr.strip()}", file=sys.stderr)
        return 1
    listing = _run(
        "gh",
        "pr",
        "list",
        "--base",
        "dev",
        "--state",
        "merged",
        "--limit",
        "500",
        "--json",
        "number,title,headRefName,headRefOid,mergedAt",
    )
    if listing.returncode != 0:
        print(f"gh pr list failed: {listing.stderr.strip()}", file=sys.stderr)
        return 1

    awaiting, graduated = [], []
    for pr in sorted(json.loads(listing.stdout), key=lambda p: p["mergedAt"]):
        if pr["headRefName"] == "master":
            continue  # a master -> dev sync PR
        line = f"#{pr['number']} {pr['headRefName']}: {pr['title']}"
        oid = pr["headRefOid"]
        if not _git_ok("cat-file", "-e", f"{oid}^{{commit}}"):
            awaiting.append(line + "  (head commit not fetched; check by hand)")
        elif _git_ok("merge-base", "--is-ancestor", oid, "origin/master"):
            if _git_ok(
                "rev-parse", "--verify", "--quiet", f"origin/{pr['headRefName']}"
            ):
                graduated.append(line)
        else:
            awaiting.append(line)

    # A branch with a newer PR still awaiting master isn't done yet.
    pending = {line.split()[1].rstrip(":") for line in awaiting}
    graduated = [g for g in graduated if g.split()[1].rstrip(":") not in pending]

    print("On dev, awaiting master:")
    print("\n".join(f"  {a}" for a in awaiting) or "  (none)")
    print("Reached master, branch can be deleted:")
    print("\n".join(f"  {g}" for g in dict.fromkeys(graduated)) or "  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
