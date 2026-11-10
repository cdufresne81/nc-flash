#!/usr/bin/env python3
"""Check a release tag request before the Tag Release workflow creates it.

Run by ``.github/workflows/tag-release.yml`` in a full clone. The version
picks the branch (docs/internal/RELEASE_CHANNELS.md):

  - ``vX.Y.Z-beta.N`` tags the head of ``dev``;
  - ``vX.Y.Z`` tags the head of ``master``.

Refuses, with nothing tagged, when:
  - the version is not one of those two forms (the updater ignores others);
  - the tag already exists;
  - a beta is asked for a version whose stable release is already tagged;
  - ``--commit`` is given and the branch head is a different commit;
  - a stable version has no ``## [vX.Y.Z]`` section in CHANGELOG.md;
  - CI has not passed on the commit.

Prints the commit to tag, and writes ``sha=<commit>`` to ``$GITHUB_OUTPUT``.

    python tools/release_tag_check.py v2.24.0-beta.16 [--commit <sha>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

_VERSION = re.compile(r"v(\d+\.\d+\.\d+)(-beta\.(\d+))?")


class TagRefused(Exception):
    """The request can't be tagged; the message says why."""


def target_branch(version: str) -> str:
    """The branch a version is tagged on: ``dev`` for a beta, else ``master``."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise TagRefused(
            f"'{version}' is not a release version. Use vX.Y.Z for a stable "
            "release or vX.Y.Z-beta.N for a beta (for example v2.24.0-beta.16)."
        )
    return "dev" if match.group(2) else "master"


def stable_version(version: str) -> str:
    """``v2.24.0-beta.3`` -> ``v2.24.0`` (a stable version is returned as-is)."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise TagRefused(f"'{version}' is not a release version.")
    return f"v{match.group(1)}"


def has_changelog_section(changelog: str, version: str) -> bool:
    """True when the changelog has a ``## [vX.Y.Z]`` section for the version."""
    return any(line.startswith(f"## [{version}]") for line in changelog.splitlines())


def ci_passed(runs: list[dict]) -> bool:
    """True when at least one CI run on the commit finished successfully."""
    return any(
        run.get("status") == "completed" and run.get("conclusion") == "success"
        for run in runs
    )


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8")


def _git(*args: str) -> str:
    result = _run("git", *args)
    if result.returncode != 0:
        raise TagRefused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _tag_exists(tag: str) -> bool:
    return (
        _run("git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}").returncode
        == 0
    )


def _ci_runs(sha: str) -> list[dict]:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    result = _run(
        "gh",
        "api",
        "-X",
        "GET",
        f"repos/{repo}/actions/workflows/ci.yml/runs",
        "-f",
        f"head_sha={sha}",
    )
    if result.returncode != 0:
        raise TagRefused(f"Could not read the CI result: {result.stderr.strip()}")
    return json.loads(result.stdout).get("workflow_runs", [])


def check(version: str, commit: str = "") -> str:
    """Validate the request and return the commit to tag."""
    branch = target_branch(version)
    if _tag_exists(version):
        raise TagRefused(f"The tag {version} already exists.")
    stable = stable_version(version)
    if branch == "dev" and _tag_exists(stable):
        raise TagRefused(
            f"{stable} is already released, so the updater would never offer "
            f"{version}. The next beta belongs to the next version."
        )

    sha = _git("rev-parse", f"origin/{branch}")
    commit = commit.strip().lower()
    if commit and not (len(commit) >= 7 and sha.startswith(commit)):
        raise TagRefused(
            f"{branch} is at {sha}, not at the commit you asked for ({commit}). "
            "The branch moved; check the new commits and run this again."
        )

    if branch == "master":
        changelog = _git("show", f"{sha}:CHANGELOG.md")
        if not has_changelog_section(changelog, version):
            raise TagRefused(
                f"CHANGELOG.md on master has no '## [{version}]' section. Move "
                "the Unreleased entries under it first, or the release notes "
                "would be wrong."
            )

    if not ci_passed(_ci_runs(sha)):
        raise TagRefused(
            f"CI has not passed on {sha} ({branch}). Wait for it to finish, or "
            "fix or re-run it, then run this again."
        )
    return sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", default=os.environ.get("VERSION", ""))
    parser.add_argument("--commit", default=os.environ.get("COMMIT", ""))
    args = parser.parse_args()
    try:
        sha = check(args.version.strip(), args.commit)
    except TagRefused as refusal:
        print(f"::error::{refusal}")
        return 1
    print(f"{args.version} will tag {sha}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"sha={sha}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
