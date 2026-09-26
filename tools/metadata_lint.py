#!/usr/bin/env python3
"""
Format and lint the ROM metadata XML files (examples/metadata/*.xml).

Two jobs:

* ``format``: rewrite each file in one readable layout (2-space indent, one
  element per line, childless elements self-closed, attribute order kept).
  Before writing, the new text is parsed and compared with the original tree
  (tags, attributes in order, leaf text, comments). Any difference aborts that
  file, so formatting can only ever change whitespace.

* ``check``: report definition errors that would make NC Flash read or write
  the wrong bytes, plus files that are not in the canonical layout. Exits 1 on
  any error. Run by tests/test_metadata_lint.py.

Usage:
    python tools/metadata_lint.py check [paths...]
    python tools/metadata_lint.py format [paths...]

With no paths, both commands work on examples/metadata/*.xml.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.storage_types import STORAGE_TYPE_BYTES  # noqa: E402

DEFAULT_DIR = _REPO_ROOT / "examples" / "metadata"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
INDENT = "  "
# Largest ROM any bundled definition targets (SH7058 ECU, 1 MB). TCM ROMs are
# smaller, so this is an upper bound, not an exact size check.
MAX_ROM_SIZE = 0x100000
REQUIRED_ROMID_FIELDS = ("xmlid", "internalidaddress", "internalidstring")
# Known issues that predate the lint and still need a decision. `check` does
# not fail on these, but any NEW issue fails. Delete a line once it's fixed.
BASELINE_FILE = Path(__file__).with_name("metadata_lint_baseline.txt")

_ATTR_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "\n": "&#10;",
    "\r": "&#13;",
    "\t": "&#9;",
}
_TEXT_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\r": "&#13;"}


class FormatError(Exception):
    """The file holds something the formatter can't lay out without loss."""


@dataclass
class Issue:
    path: Path
    message: str

    def __str__(self):
        return f"{self.path.name}: {self.message}"


# ---------------------------------------------------------------- parsing


def _parse(data: bytes) -> etree._Element:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    return etree.fromstring(data, parser)


def _read_text(path: Path) -> str:
    """File content as text, with any BOM dropped and line endings as LF."""
    return path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")


def _is_blank(text) -> bool:
    return text is None or text.strip() == ""


# ------------------------------------------------------------- formatting


def _escape(value: str, table: dict) -> str:
    return "".join(table.get(c, c) for c in value)


def _emit(node, depth: int, out: list):
    pad = INDENT * depth
    if not _is_blank(node.tail):
        raise FormatError(f"text {node.tail.strip()!r} after <{node.tag}>")
    if isinstance(node, etree._Comment):
        if "--" in (node.text or "") or (node.text or "").endswith("-"):
            raise FormatError("comment contains '--'")
        out.append(f"{pad}<!--{node.text or ''}-->")
        return
    if not isinstance(node.tag, str):
        raise FormatError(f"unsupported node {node!r} (only elements and comments)")
    if node.nsmap or node.prefix:
        raise FormatError(f"namespaced element <{node.tag}>")

    attrs = "".join(
        f' {k}="{_escape(v, _ATTR_ESCAPES)}"' for k, v in node.attrib.items()
    )
    children = list(node)
    if children:
        if not _is_blank(node.text):
            raise FormatError(f"<{node.tag}> mixes text and child elements")
        out.append(f"{pad}<{node.tag}{attrs}>")
        for child in children:
            _emit(child, depth + 1, out)
        out.append(f"{pad}</{node.tag}>")
    elif node.text is None or node.text == "":
        out.append(f"{pad}<{node.tag}{attrs}/>")
    elif node.text.strip() == "":
        # Whitespace-only leaf text carries no meaning for the parser
        # (it strips text); drop it rather than bake it into the layout.
        out.append(f"{pad}<{node.tag}{attrs}/>")
    else:
        text = _escape(node.text, _TEXT_ESCAPES)
        out.append(f"{pad}<{node.tag}{attrs}>{text}</{node.tag}>")


def format_xml(data: bytes) -> str:
    """Return *data* (an XML document) in the canonical layout."""
    root = _parse(data)
    out = [XML_DECLARATION]
    for sibling in root.itersiblings(preceding=True):
        raise FormatError(f"content before the root element: {sibling!r}")
    for sibling in root.itersiblings():
        raise FormatError(f"content after the root element: {sibling!r}")
    tail, root.tail = root.tail, None
    _emit(root, 0, out)
    root.tail = tail
    return "\n".join(out) + "\n"


def _signature(node) -> tuple:
    """Everything about a tree the parser can see; whitespace layout excluded."""
    if isinstance(node, etree._Comment):
        return ("#comment", node.text or "")
    children = list(node)
    if children:
        text = ""  # _emit refuses non-blank text next to children
    else:
        text = "" if _is_blank(node.text) else node.text
    return (
        node.tag,
        tuple(node.attrib.items()),
        text,
        tuple(_signature(c) for c in children),
    )


def same_content(a: bytes, b: bytes) -> bool:
    return _signature(_parse(a)) == _signature(_parse(b))


def format_file(path: Path) -> bool:
    """Rewrite *path* in the canonical layout. Returns True if it changed."""
    original = path.read_bytes()
    formatted = format_xml(original)
    if formatted == _read_text(path):
        return False
    if not same_content(original, formatted.encode("utf-8")):
        raise FormatError("formatted output differs in content (bug)")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(formatted)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


# ------------------------------------------------------------------ lint


def _rom_element(root):
    """The <rom> element, for both <roms><rom> and bare <rom> files."""
    return root if root.tag == "rom" else root.find("rom")


def _hex(value: str):
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        return None


def _check_table(path, table, scalings, where, issues):
    """Check one table or axis; return its byte extent (start, end) or None."""
    name = table.get("name") or table.get("type") or "?"
    label = f"{where}'{name}'"
    scaling_name = table.get("scaling")
    address = table.get("address")

    if scaling_name is None:
        # Axes with inline <data> breakpoints (no address, no scaling) are fine.
        if address is not None:
            issues.append(Issue(path, f"{label} has an address but no scaling"))
        return None
    scaling = scalings.get(scaling_name)
    if scaling is None:
        issues.append(Issue(path, f"{label} uses unknown scaling '{scaling_name}'"))
        return None
    storagetype = scaling.get("storagetype", "float").lower()
    width = STORAGE_TYPE_BYTES.get(storagetype)
    if width is None:
        return None  # reported once on the scaling itself
    if address is None:
        return None
    addr = _hex(address)
    if addr is None:
        issues.append(Issue(path, f"{label} has a bad address {address!r}"))
        return None
    if addr % width:
        issues.append(
            Issue(
                path,
                f"{label} at 0x{addr:X} is not {width}-byte aligned but its "
                f"scaling '{scaling_name}' is {storagetype}. The SH-2 CPU "
                "can't load a value that size there in one access, so the "
                "size is almost certainly wrong. Check the code that reads it",
            )
        )
    try:
        elements = int(table.get("elements", "1"))
    except ValueError:
        issues.append(Issue(path, f"{label} has a bad elements count"))
        return None
    if table.get("layout", "contiguous") != "contiguous":
        return None  # interleaved extents depend on the axes; not checked
    end = addr + max(elements, 1) * width
    if end > MAX_ROM_SIZE:
        issues.append(Issue(path, f"{label} ends past 1 MB (0x{end:X})"))
    return (addr, end, label)


def _check_dimensions(path, table, issues):
    """elements must equal the axis sizes (2D: the axis; 3D: x * y)."""
    kind = table.get("type")
    if kind not in ("2D", "3D") or table.get("elements") is None:
        return
    if table.get("layout", "contiguous") != "contiguous":
        return
    sizes = {}
    for axis in table.findall("table"):
        try:
            sizes[axis.get("type")] = int(axis.get("elements", ""))
        except ValueError:
            return  # axis without a count: nothing to compare against
    if kind == "3D":
        if "X Axis" not in sizes or "Y Axis" not in sizes:
            return
        expected = sizes["X Axis"] * sizes["Y Axis"]
    else:
        if len(sizes) != 1:
            return
        expected = next(iter(sizes.values()))
    try:
        elements = int(table.get("elements"))
    except ValueError:
        return  # reported by _check_table
    if elements != expected:
        issues.append(
            Issue(
                path,
                f"'{table.get('name')}' has elements=\"{elements}\" but its "
                f"axes make {expected} cells",
            )
        )


def _check_overlaps(path, extents, issues):
    """Two tables sharing bytes: writing one changes the other."""
    extents = sorted(set(extents))
    for i, (start, end, label) in enumerate(extents):
        for other_start, other_end, other in extents[i + 1 :]:
            if other_start >= end:
                break
            if (other_start, other_end) == (start, end):
                continue  # the same bytes under two names (an alias), not a clash
            issues.append(
                Issue(
                    path,
                    f"{label} (0x{start:X}-0x{end - 1:X}) overlaps {other} "
                    f"(0x{other_start:X}-0x{other_end - 1:X})",
                )
            )


def lint_file(path: Path) -> list:
    """Definition errors in one file (excluding layout)."""
    issues = []
    try:
        root = _parse(path.read_bytes())
    except etree.XMLSyntaxError as e:
        return [Issue(path, f"not well-formed XML: {e}")]
    rom = _rom_element(root)
    if rom is None:
        return [Issue(path, "no <rom> element")]
    romid = rom.find("romid")
    for field in REQUIRED_ROMID_FIELDS:
        el = romid.find(field) if romid is not None else None
        if el is None or _is_blank(el.text):
            issues.append(Issue(path, f"romid is missing <{field}>"))

    scalings = {}
    for sc in rom.iter("scaling"):
        name = sc.get("name")
        if not name:
            issues.append(Issue(path, "a <scaling> has no name"))
            continue
        # speeps' files repeat many scalings word for word; only a repeat
        # that differs matters (the parser keeps the last one).
        if name in scalings and scalings[name].attrib != sc.attrib:
            issues.append(
                Issue(path, f"scaling '{name}' is defined twice, differently")
            )
        scalings[name] = sc
        storagetype = sc.get("storagetype", "float")
        if storagetype.lower() not in STORAGE_TYPE_BYTES:
            issues.append(
                Issue(path, f"scaling '{name}' has unknown storagetype {storagetype!r}")
            )

    first_at = {}
    extents = []
    for table in rom.findall("table"):
        name = table.get("name")
        if not name:
            issues.append(Issue(path, "a top-level <table> has no name"))
        elif name in first_at:
            # Lookups by name (MCP read/write, compare) reach only one copy.
            issues.append(
                Issue(
                    path,
                    f"table '{name}' at 0x{table.get('address')} has the same "
                    f"name as the one at 0x{first_at[name]}",
                )
            )
        else:
            first_at[name] = table.get("address")
        extents.append(_check_table(path, table, scalings, "", issues))
        _check_dimensions(path, table, issues)
        for axis in table.findall("table"):
            extents.append(_check_table(path, axis, scalings, f"'{name}' ", issues))
    _check_overlaps(path, [e for e in extents if e], issues)
    return issues


def _rom_id(path: Path):
    try:
        romid = _rom_element(_parse(path.read_bytes())).find("romid")
        return (
            (romid.findtext("internalidaddress") or "").strip().lower(),
            (romid.findtext("internalidstring") or "").strip(),
        )
    except (etree.XMLSyntaxError, AttributeError):
        return None


def load_baseline(path: Path = BASELINE_FILE) -> Counter:
    """Accepted issue lines, counted: a repeated line is only covered as
    many times as the baseline lists it."""
    if not path.exists():
        return Counter()
    lines = path.read_text(encoding="utf-8").splitlines()
    return Counter(ln for ln in lines if ln.strip() and not ln.startswith("#"))


def new_issues(issues: list, baseline: Counter) -> list:
    """The issues the baseline doesn't cover."""
    left = Counter(baseline)
    new = []
    for issue in issues:
        if left[str(issue)] > 0:
            left[str(issue)] -= 1
        else:
            new.append(issue)
    return new


def _layout_ok(path: Path) -> bool:
    """True if the file is in the canonical layout. The XML declaration's
    quoting is ignored: the app's scaling editor (lxml) writes single quotes."""
    expected = format_xml(path.read_bytes()).split("\n", 1)[1]
    actual = _read_text(path)
    if actual.startswith("<?xml"):
        actual = actual.split("\n", 1)[1] if "\n" in actual else ""
    return actual == expected


def check(paths: list) -> list:
    issues = []
    ids = {}
    for path in paths:
        issues.extend(lint_file(path))
        try:
            if not _layout_ok(path):
                issues.append(
                    Issue(
                        path, "not in canonical layout (run: metadata_lint.py format)"
                    )
                )
        except (FormatError, etree.XMLSyntaxError) as e:
            issues.append(Issue(path, f"can't be formatted: {e}"))
        rid = _rom_id(path)
        if rid:
            ids.setdefault(rid, []).append(path.name)
    for (address, string), names in ids.items():
        if len(names) > 1:
            issues.append(
                Issue(
                    Path(names[0]),
                    f"ROM ID {string!r} at 0x{address} is also claimed by "
                    f"{', '.join(names[1:])} (detection would be ambiguous)",
                )
            )
    return issues


# ------------------------------------------------------------------- CLI


def _collect(args_paths: list) -> list:
    if not args_paths:
        return sorted(DEFAULT_DIR.glob("*.xml"))
    out = []
    for p in map(Path, args_paths):
        out.extend(sorted(p.glob("*.xml")) if p.is_dir() else [p])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("check", "format"))
    ap.add_argument("paths", nargs="*")
    ap.add_argument(
        "--no-baseline", action="store_true", help="also report baselined issues"
    )
    args = ap.parse_args(argv)
    paths = _collect(args.paths)

    if args.command == "format":
        failed = 0
        for path in paths:
            try:
                if format_file(path):
                    print(f"formatted {path.name}")
            except (FormatError, etree.XMLSyntaxError) as e:
                failed += 1
                print(f"SKIPPED {path.name}: {e}", file=sys.stderr)
        return 1 if failed else 0

    baseline = Counter() if args.no_baseline else load_baseline()
    issues = check(paths)
    new = new_issues(issues, baseline)
    for issue in new:
        print(issue)
    known = len(issues) - len(new)
    print(f"{len(paths)} files, {len(new)} issue(s), {known} known (baseline)")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
