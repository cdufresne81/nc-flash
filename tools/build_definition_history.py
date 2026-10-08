#!/usr/bin/env python3
"""
Build examples/metadata_history.json: for each bundled definition file, the
content hash of every version NC Flash shipped in a release (v* tags).

The startup definition update (src/core/definition_update.py) uses it for a
workspace file it has no remembered copy for (installs from before that
feature): a file whose content matches a released version was never edited,
so the new bundled version can replace it. Any other file is left alone.

Run it before each release, after tagging the previous one, so the file
covers every released version:
    python tools/build_definition_history.py
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.core.definition_update import (  # noqa: E402
    HISTORY_FILE,
    DefinitionUpdateError,
    file_hash,
)


def git(*args) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True
    ).stdout


def main() -> int:
    tags = [t for t in git("tag", "--list", "v*").decode().split() if t]
    files = {}
    for tag in tags:
        listing = git("ls-tree", "--name-only", tag, "examples/metadata/").decode()
        for path in listing.split():
            if not path.lower().endswith(".xml"):
                continue
            try:
                h = file_hash(git("show", f"{tag}:{path}"))
            except DefinitionUpdateError as e:
                print(f"skip {tag}:{path}: {e}")
                continue
            files.setdefault(Path(path).name.lower(), set()).add(h)
    out = {
        "format": 1,
        "releases": sorted(tags),
        "files": {name: sorted(h) for name, h in sorted(files.items())},
    }
    target = REPO / "examples" / HISTORY_FILE
    target.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    versions = sum(len(h) for h in files.values())
    print(f"{len(tags)} releases, {len(files)} files, {versions} versions -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
