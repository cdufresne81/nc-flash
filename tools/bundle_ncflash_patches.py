"""Bundle the NC Flash feature packs of one nc-flash-re commit, in one step.

    python tools/bundle_ncflash_patches.py <nc-flash-re repo> <commit> [--vectors]

1. Reads roms/ncflash_patches/index.json and every pack file it lists AT THAT
   COMMIT (git show, never a working tree) and writes them byte for byte under
   patches/ (no JSON re-serialisation; patches/** is -text in .gitattributes).
   Files of patches/ that the index no longer lists are removed.
2. Recomputes every sha256 and rewrites the PINS block of
   src/ecu/bundled_patches.py (index.json included).
3. With --vectors: regenerates the LF9VEB cross-check vectors with that
   commit's tools/ncflash_apply.py (make_vectors.py --lean), into
   tests/fixtures/ncflash_patch/lf9veb_ncflash_features_<version>.vectors.json
   (older vectors files of real packs are removed). It is the slow part: the
   reference applier runs ~730 cases.

Then run: pytest tests/test_bundled_patches.py tests/test_bundled_pack.py
tests/test_ncflash_patch_vectors.py.
"""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
PINS_FILE = REPO / "src" / "ecu" / "bundled_patches.py"
FIX = REPO / "tests" / "fixtures" / "ncflash_patch"


def git_show(repo: str, commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", repo, "show", f"{commit}:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle(repo: str, commit: str) -> dict:
    full = subprocess.run(
        ["git", "-C", repo, "rev-parse", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    idx_raw = git_show(repo, full, "roms/ncflash_patches/index.json")
    idx = json.loads(idx_raw)
    files = {"index.json": idx_raw}
    for e in idx["patches"]:
        raw = git_show(repo, full, f"roms/ncflash_patches/{e['file']}")
        if sha(raw) != e["sha256"]:
            raise SystemExit(f"{e['file']}: sha256 differs from its index entry")
        files[e["file"]] = raw
    for old in sorted(PATCHES.rglob("*")):
        rel = old.relative_to(PATCHES).as_posix()
        if old.is_file() and rel not in files:
            old.unlink()
            print(f"removed patches/{rel}")
    for rel, raw in files.items():
        out = PATCHES / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        if out.read_bytes() != raw:
            raise SystemExit(f"patches/{rel} did not read back")
        print(f"patches/{rel}  {sha(raw)}")
    for d in sorted(PATCHES.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    versions = sorted(
        {f"{e['id']} {e['version']} ({e['channel']})" for e in idx["patches"]}
    )
    ids = ", ".join(sorted(e["rom_id"] for e in idx["patches"]))
    lines = [
        "# Path relative to patches/ (forward slashes) -> sha256 of the exact bytes.",
        f"# {'; '.join(versions)} for {ids}: nc-flash-re {full[:7]},",
        "# roms/ncflash_patches/ (written by tools/bundle_ncflash_patches.py).",
        "PINS = {",
    ]
    for rel in sorted(files, key=lambda r: (r != "index.json", r)):
        one = f'    "{rel}": "{sha(files[rel])}",'
        wrapped = f'    "{rel}": (\n        "{sha(files[rel])}"\n    ),'
        lines.append(one if len(one) <= 88 else wrapped)
    lines.append("}")
    src = PINS_FILE.read_text(encoding="utf-8")
    new = re.sub(
        r"# Path relative to patches/.*?\nPINS = \{.*?\n\}",
        lambda _: "\n".join(lines),
        src,
        count=1,
        flags=re.S,
    )
    if new == src and "PINS = {" not in src:
        raise SystemExit("PINS block not found")
    PINS_FILE.write_text(new, encoding="utf-8", newline="\n")
    subprocess.run([sys.executable, "-m", "black", "-q", str(PINS_FILE)], check=True)
    print(f"rewrote {PINS_FILE.relative_to(REPO).as_posix()} ({len(files)} pins)")
    return {"commit": full, "index": idx}


def vectors(repo: str, commit: str, idx: dict):
    entry = next(e for e in idx["patches"] if e["rom_id"] == "LF9VEB")
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ncflash_apply.py"
        ref.write_bytes(git_show(repo, commit, "tools/ncflash_apply.py"))
        for old in FIX.glob("lf9veb_ncflash_features_*.vectors.json"):
            if "_fixture" not in old.name:
                old.unlink()
        out = (
            FIX
            / f"lf9veb_ncflash_features_{entry['version'].replace('.', '_')}.vectors.json"
        )
        subprocess.run(
            [
                sys.executable,
                str(FIX / "make_vectors.py"),
                str(PATCHES / entry["file"]),
                str(ref),
                str(out),
                "--lean",
            ],
            check=True,
        )


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    repo, commit = argv[0], argv[1]
    r = bundle(repo, commit)
    if "--vectors" in argv[2:]:
        vectors(repo, r["commit"], r["index"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
