"""
Definition Updates

Delivers new and corrected bundled ROM definitions (examples/metadata/*.xml) to
the workspace metadata folder after an app update, without losing the user's
own edits (#121).

When it runs: once per newer release build (never on a downgrade, never when
running from source unless NCFLASH_UPDATE_DEFINITIONS=1), before any ROM opens,
and only when the Metadata Directory is the workspace's own ``metadata`` folder.

Per bundled file:

* Not in the workspace: added. Unless the user deleted it after receiving it,
  or defines the same ROM (internal ID string + address) in a file of their own.
* Identifies a different ROM than the bundled file (romid edited): left alone.
* We know which bundled version the user last received (a copy kept in
  ``<workspace>/.ncflash/definition_base/``): three-way merge, entry by entry
  and attribute by attribute. A value the user changed is kept unless the
  bundled file changed the same value too; then the bundled value wins for
  fields that decide which bytes are written (address, size, type, axes,
  scaling formula, storage type, endianness) and the user's value wins for
  display fields (names, categories, min/max, units, format, step). Every
  such conflict is reported. Tables the bundled file removed or moved are
  removed from the user's copy too (reported by name when the user had edited
  them); tables the user added are kept.
* No copy (an install from before this feature, or the copy was lost): the
  file is replaced only if it is exactly a version NC Flash shipped in some
  release (examples/metadata_history.json lists each release's content hash
  per file). A file the user edited is left alone, and the user is told where
  the new version is.

Every file that changes is backed up first, to a new folder under
``<workspace>/metadata_backups/``.
"""

import copy
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from lxml import etree

logger = logging.getLogger(__name__)

# Attributes that decide which bytes are read and written: on a conflict the
# bundled value wins. Every other attribute is display-only.
TABLE_LAYOUT = ("address", "elements", "type", "swapxy", "scaling", "layout")
AXIS_LAYOUT = ("address", "elements", "type", "scaling")
SCALING_LAYOUT = ("name", "toexpr", "frexpr", "storagetype", "endian")
ROM_IDENTITY = ("internalidaddress", "internalidstring")

HISTORY_FILE = "metadata_history.json"
BASE_DIR = Path(".ncflash") / "definition_base"
LOCK_FILE = Path(".ncflash") / "definition_update.lock"
LOCK_STALE_SECONDS = 600
BACKUP_DIR = "metadata_backups"
VERSION_KEY = "definitions/updated_for_version"
FAILURES_KEY = "definitions/failed_attempts"
MAX_ATTEMPTS = 3  # starts in a row that hit a file error before giving up

# Detail prefixes (the summary counts them)
CONFLICT = "NC Flash's corrected value replaced your edit: "
REMOVED = "removed, no longer in NC Flash's definition: "
REPLACED = "NC Flash's new table replaced yours at the same address: "
LAYOUT_DIFF = "differs in a value that decides the bytes written: "
FORCE_ENV = "NCFLASH_UPDATE_DEFINITIONS"


class DefinitionUpdateError(Exception):
    """A definition file can't be merged safely; it is left as it is."""


# ------------------------------------------------------------------ parsing


def _parser():
    return etree.XMLParser(
        remove_blank_text=False, resolve_entities=False, no_network=True
    )


def _parse(data: bytes):
    try:
        return etree.fromstring(data, _parser())
    except etree.XMLSyntaxError as e:
        raise DefinitionUpdateError(f"not valid XML ({e})") from e


def _elements(node) -> list:
    """Child elements, without comments or processing instructions."""
    return [c for c in node if isinstance(c.tag, str)]


def _digest(elem, h):
    """Feed an element's content (tag, attributes, leaf text, children) to *h*."""
    children = _elements(elem)
    parts = [elem.tag, str(len(elem.attrib))]
    for k, v in sorted(elem.attrib.items()):
        parts += [k, v]
    parts.append("" if children else (elem.text or "").strip())
    parts.append(str(len(children)))
    h.update("".join(f"{len(x)}:{x}" for x in parts).encode("utf-8"))
    for c in children:
        _digest(c, h)


def entry_hash(elem) -> str:
    """Content of one entry, independent of layout and comments."""
    h = hashlib.sha256()
    _digest(elem, h)
    return h.hexdigest()[:16]


def _load(data: bytes):
    """(identity, content hash) of a definition file, from one parse."""
    root = _parse_stripped(data)
    ident = _identity(_rom(root))
    return ident, hashlib.sha256(etree.tostring(root, method="c14n")).hexdigest()[:16]


def file_hash(data: bytes) -> str:
    """
    Content of a whole definition file, independent of layout and comments
    (canonical XML of the tree without blank text). Raises
    DefinitionUpdateError if it is not valid XML.
    """
    root = _parse_stripped(data)
    return hashlib.sha256(etree.tostring(root, method="c14n")).hexdigest()[:16]


def _parse_stripped(data: bytes):
    parser = etree.XMLParser(
        remove_blank_text=True,
        remove_comments=True,
        remove_pis=True,
        resolve_entities=False,
        no_network=True,
    )
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise DefinitionUpdateError(f"not valid XML ({e})") from e


class _Hashes:
    """entry_hash, computed once per element during one merge."""

    def __init__(self):
        self._cache: Dict[int, Tuple[object, str]] = {}

    def __call__(self, elem) -> str:
        hit = self._cache.get(id(elem))
        if hit is None or hit[0] is not elem:
            hit = (elem, entry_hash(elem))  # keeps elem alive: ids stay unique
            self._cache[id(elem)] = hit
        return hit[1]


def _rom(root):
    roms = root.findall("rom")
    if root.tag != "roms" or len(roms) != 1:
        raise DefinitionUpdateError("expected exactly one <rom> under <roms>")
    return roms[0]


def _identity(rom) -> Tuple[str, ...]:
    """What ROM detection matches a definition on."""
    romid = rom.find("romid")
    if romid is None:
        return ("", "")
    return tuple((romid.findtext(f) or "").strip().lower() for f in ROM_IDENTITY)


def rom_identity(data: bytes) -> Optional[Tuple[str, ...]]:
    """(internalidaddress, internalidstring) of a definition file, or None."""
    try:
        ident = _identity(_rom(_parse(data)))
    except DefinitionUpdateError:
        return None
    return ident if all(ident) else None


def _group_key(e) -> tuple:
    if e.tag == "table":
        try:
            return ("table", int(e.get("address", ""), 16))
        except ValueError as err:
            raise DefinitionUpdateError(
                f"table {e.get('name')!r} has a bad address"
            ) from err
    if e.tag == "scaling":
        return ("scaling", e.get("name"))
    if e.tag == "romid":
        return ("romid",)
    return ("other", entry_hash(e))


def _groups(rom) -> Dict[tuple, list]:
    """Top-level entries grouped by key: tables by address, scalings by name."""
    out: Dict[tuple, list] = {}
    for e in _elements(rom):
        out.setdefault(_group_key(e), []).append(e)
    return out


def _match(xs: list, ys: list, eh) -> Tuple[list, list, list]:
    """
    Pair entries of one group (one address, or one scaling name) across two
    versions: identical content first, then same name, then, if exactly one
    is left on each side, those two. Returns (pairs, unmatched xs, unmatched ys).

    Raises:
        DefinitionUpdateError: several entries left on both sides; guessing
            which is which could give a correction to the wrong table
    """
    xs, ys = list(xs), list(ys)
    pairs = []
    for same in (
        lambda a, b: eh(a) == eh(b),
        lambda a, b: a.get("name") == b.get("name"),
    ):
        for x in list(xs):
            y = next((y for y in ys if same(x, y)), None)
            if y is not None:
                pairs.append((x, y))
                xs.remove(x)
                ys.remove(y)
    if len(xs) == 1 and len(ys) == 1:
        pairs.append((xs.pop(), ys.pop()))
    elif xs and ys:
        raise DefinitionUpdateError(
            f"can't tell which of the entries at {_label(xs[0])} are which"
        )
    return pairs, xs, ys


def _label(e) -> str:
    if e.tag == "table":
        return f"{e.get('name')} (0x{e.get('address', '').upper()})"
    if e.tag == "scaling":
        return f"scaling {e.get('name')}"
    return e.tag


# ------------------------------------------------------------------ merge


@dataclass
class MergeResult:
    data: bytes  # the merged file
    kept: List[str] = field(default_factory=list)  # user edits kept
    conflicts: List[str] = field(default_factory=list)  # both changed a value
    removed: List[str] = field(default_factory=list)  # edited, no longer shipped
    replaced: List[str] = field(default_factory=list)  # bundled took the slot
    own_tables: int = 0  # tables only the user has, kept


def _merge_attrs(out, user, base, layout, where, result):
    """Three-way per attribute; *out* starts as the bundled entry."""
    for attr in sorted(set(user.attrib) | set(out.attrib) | set(base.attrib)):
        u, b, o = user.get(attr), out.get(attr), base.get(attr)
        if u == b or u == o:
            continue  # same, or only the bundled side changed: bundled value
        if b == o or attr not in layout:
            # Only the user changed it, or both changed a display field
            if u is None:
                del out.attrib[attr]
            else:
                out.set(attr, u)
            result.kept.append(f"{where}: {attr}")
        else:
            # Both changed a value that decides the bytes: the correction wins
            result.conflicts.append(f"{where}: {attr}")


def _merge_entry(b, u, base, result, eh):
    """Three-way merge of one entry the user and the bundled file both have."""
    if eh(u) == eh(base):
        return copy.deepcopy(b)  # the user didn't touch it
    where = _label(b)
    if eh(b) == eh(base):
        result.kept.append(where)  # only the user changed it
        return copy.deepcopy(u)
    if b.tag not in ("table", "scaling"):
        result.conflicts.append(where)
        return copy.deepcopy(b)
    out = copy.deepcopy(b)
    layout = TABLE_LAYOUT if b.tag == "table" else SCALING_LAYOUT
    _merge_attrs(out, u, base, layout, where, result)
    if b.tag == "table":
        b_ax, u_ax, o_ax = _elements(out), _elements(u), _elements(base)
        canon = lambda axes: [entry_hash(a) for a in axes]  # noqa: E731
        if canon(u_ax) == canon(o_ax):
            pass  # the user didn't touch the axes: bundled axes
        elif canon(b_ax) == canon(o_ax):
            for a in b_ax:
                out.remove(a)
            for a in u_ax:
                out.append(copy.deepcopy(a))
            result.kept.append(f"{where}: axes")
        elif len(b_ax) == len(u_ax) == len(o_ax) and all(
            x.tag == y.tag == z.tag for x, y, z in zip(b_ax, u_ax, o_ax)
        ):
            for i, (x, y, z) in enumerate(zip(b_ax, u_ax, o_ax)):
                _merge_attrs(x, y, z, AXIS_LAYOUT, f"{where} axis {i + 1}", result)
        else:
            result.conflicts.append(f"{where}: axes")
    return out


def merge(bundled: bytes, user: bytes, base: bytes) -> MergeResult:
    """
    Three-way merge of the user's copy of a definition with a newer bundled
    one, *base* being the bundled version the user last received.

    Raises:
        DefinitionUpdateError: the files can't be merged safely
    """
    b_root = _parse(bundled)
    b_rom = _rom(b_root)
    u_rom = _rom(_parse(user))
    o_rom = _rom(_parse(base))
    B, U, O = _groups(b_rom), _groups(u_rom), _groups(o_rom)
    eh = _Hashes()
    result = MergeResult(data=b"")
    replace = []  # (bundled element, new element or None to remove)
    own = []

    keys = list(B) + [k for k in U if k not in B]
    keys += [k for k in O if k not in B and k not in U]
    for key in keys:
        b_list, u_list, o_list = B.get(key, []), U.get(key, []), O.get(key, [])
        ob, _, b_new = _match(o_list, b_list, eh)
        ou, _, u_new = _match(o_list, u_list, eh)
        to_b = {id(o): b for o, b in ob}
        to_u = {id(o): u for o, u in ou}
        for o in o_list:
            b, u = to_b.get(id(o)), to_u.get(id(o))
            if b is not None and u is not None:
                if eh(u) != eh(b):
                    replace.append((b, _merge_entry(b, u, o, result, eh)))
            elif b is not None:
                replace.append((b, None))  # the user deleted it: stays deleted
            elif u is not None and eh(u) != eh(o):
                # The bundled file removed or moved it; the user had edited it
                result.removed.append(_label(u))
        # New on both sides (not in base): the bundled entry takes the slot
        pairs, _, u_only = _match(b_new, u_new, eh)
        for b, u in pairs:
            if eh(b) != eh(u):
                result.replaced.append(_label(u))
        own.extend(u_only)

    for b, new in replace:
        if new is None:
            b_rom.remove(b)
        else:
            new.tail = b.tail
            b_rom.replace(b, new)
    result.own_tables = sum(1 for e in own if e.tag == "table")
    _append(b_rom, own)

    # Never leave a table pointing at a scaling that's gone
    have = {e.get("name") for e in _elements(b_rom) if e.tag == "scaling"}
    missing = _referenced_scalings(b_rom) - have
    if missing:
        rescued = [
            e
            for e in _elements(u_rom)
            if e.tag == "scaling" and e.get("name") in missing
        ]
        _append(b_rom, rescued)
        missing -= {e.get("name") for e in rescued}
        if missing:
            raise DefinitionUpdateError(
                f"merged file would reference missing scaling(s): {sorted(missing)}"
            )

    etree.indent(b_root, space="  ")
    result.data = (
        etree.tostring(b_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        + b"\n"
    )
    return result


def _referenced_scalings(rom) -> Set[str]:
    return {
        e.get("scaling")
        for t in _elements(rom)
        if t.tag == "table"
        for e in t.iter()
        if isinstance(e.tag, str) and e.get("scaling")
    }


def _append(rom, elems):
    """Add copies of *elems*: scalings before the first table, the rest at the end."""
    first_table = next((k for k in _elements(rom) if k.tag == "table"), None)
    for e in elems:
        new = copy.deepcopy(e)
        if e.tag == "scaling" and first_table is not None:
            first_table.addprevious(new)
        else:
            rom.append(new)


# ------------------------------------------------------------------ workspace


@dataclass
class FileOutcome:
    name: str
    # added | replaced | merged | unchanged | kept-deleted | left-alone | skipped
    action: str
    details: List[str] = field(default_factory=list)
    failed_io: bool = False


@dataclass
class UpdateReport:
    files: List[FileOutcome] = field(default_factory=list)
    backup_dir: Optional[Path] = None
    bundled_dir: Optional[Path] = None
    skipped_reason: Optional[str] = None  # the whole update did not run

    def by_action(self, action: str) -> List[FileOutcome]:
        return [f for f in self.files if f.action == action]

    @property
    def failed_io(self) -> bool:
        return any(f.failed_io for f in self.files)

    @property
    def worth_telling(self) -> bool:
        return bool(
            self.skipped_reason
            or any(
                self.by_action(a)
                for a in ("added", "replaced", "merged", "left-alone", "skipped")
            )
        )

    def summary_text(self) -> str:
        """Short plain-English summary for the user."""
        if self.skipped_reason:
            return self.skipped_reason
        lines = []
        updated = self.by_action("replaced") + self.by_action("merged")
        added = self.by_action("added")
        alone = self.by_action("left-alone")
        skipped = self.by_action("skipped")
        if updated:
            lines.append(
                f"Updated {len(updated)} ROM definition(s) with new and "
                "corrected tables."
            )
        if added:
            lines.append(f"Added {len(added)} new ROM definition(s).")
        merged = self.by_action("merged")
        if merged:
            text = "Your edits and the tables you added were kept"
            conflicts = self._count(merged, CONFLICT)
            removed = self._count(merged, REMOVED)
            replaced = self._count(merged, REPLACED)
            if conflicts or removed or replaced:
                text += ", except:"
            else:
                text += "."
            if conflicts:
                text += (
                    f"\n- {conflicts} value(s) you had changed were replaced by NC "
                    "Flash's correction (address, size, data type or formula)."
                )
            if removed:
                text += (
                    f"\n- {removed} table(s) you had edited were removed: NC Flash's "
                    "definition no longer has them (they moved or were wrong)."
                )
            if replaced:
                text += (
                    f"\n- {replaced} table(s) you added were replaced by NC Flash's "
                    "new table at the same address."
                )
            lines.append(text)
        if alone:
            text = (
                f"{len(alone)} definition(s) you edited were not updated. The new "
                f"versions are in {self.bundled_dir}: compare and copy what you need."
            )
            risky = [f.name for f in alone if self._count([f], LAYOUT_DIFF)]
            if risky:
                text += (
                    " Check these first, they differ from NC Flash's in values that "
                    "decide which bytes are written (see Show Details): "
                    f"{', '.join(risky)}."
                )
            lines.append(text)
        if skipped:
            lines.append(
                f"{len(skipped)} definition(s) could not be updated "
                "(see Show Details)."
            )
        if self.backup_dir is not None:
            lines.append(f"Your previous files were saved in {self.backup_dir}.")
        return "\n\n".join(lines)

    @staticmethod
    def _count(files, prefix) -> int:
        return sum(1 for f in files for d in f.details if d.startswith(prefix))

    def details_text(self) -> str:
        """Per-file details, for the dialog's details pane."""
        lines = []
        for f in self.files:
            if f.action in ("merged", "left-alone", "skipped"):
                lines.append(f"{f.name}:")
                lines.extend(f"  - {d}" for d in f.details)
        return "\n".join(lines)


def _write_atomic(path: Path, data: bytes, durable: bool = True):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_base(data: bytes):
    """The remembered bundled copy, parsed; None if unreadable (e.g. truncated)."""
    try:
        root = _parse(data)
        _rom(root)
        return root
    except DefinitionUpdateError:
        return None


def _validate(data: bytes, scratch: Path):
    """The merged file must load in NC Flash's own definition parser."""
    from .definition_parser import load_definition

    scratch.write_bytes(data)
    try:
        definition = load_definition(str(scratch))
    except Exception as e:  # any parse failure means: don't write it
        raise DefinitionUpdateError(f"merged file doesn't load ({e})") from e
    finally:
        scratch.unlink(missing_ok=True)
    missing = [
        t.name
        for t in definition.tables
        if t.scaling and t.scaling not in definition.scalings
    ]
    if missing:
        raise DefinitionUpdateError(f"tables without a scaling: {missing[:3]}")


class _Backups:
    """A new folder per run, created on first use; never overwrites a file."""

    def __init__(self, root: Path, stamp: str):
        self.root, self.stamp, self.dir = root, stamp, None

    def save(self, path: Path):
        if self.dir is None:
            n = 1
            while True:
                d = self.root / (self.stamp if n == 1 else f"{self.stamp}_{n}")
                try:
                    d.mkdir(parents=True, exist_ok=False)
                    break
                except FileExistsError:
                    n += 1
            self.dir = d
        target = self.dir / path.name
        if target.exists():
            raise DefinitionUpdateError(f"backup {target} already exists")
        shutil.copy2(path, target)


def update_definitions(
    bundled_dir: Path,
    metadata_dir: Path,
    workspace: Path,
    history: Optional[Dict[str, Set[str]]] = None,
    now: Optional[datetime] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> UpdateReport:
    """
    Bring the workspace definitions up to date with the bundled ones.

    Args:
        history: file name (lower case) -> content hashes of every version
            NC Flash shipped in a release
        progress: called with (files done, total) after each file

    Never raises for a single file: a file that can't be updated safely is
    left untouched and reported.
    """
    report = UpdateReport(bundled_dir=bundled_dir)
    history = history or {}
    base_dir = workspace / BASE_DIR
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    backups = _Backups(workspace / BACKUP_DIR, stamp)

    existing = {p.name.lower(): p for p in metadata_dir.glob("*.xml")}
    bundled_names = {p.name.lower() for p in bundled_dir.glob("*.xml")}
    own_ids: Dict[Tuple[str, ...], str] = {}  # ROM identity -> user file name
    for name, p in existing.items():
        if name not in bundled_names:
            ident = rom_identity(p.read_bytes())
            if ident:
                own_ids.setdefault(ident, p.name)

    sources = sorted(bundled_dir.glob("*.xml"))
    for done, src in enumerate(sources):
        if progress is not None:
            progress(done, len(sources))
        name = src.name
        out = FileOutcome(name, "unchanged")
        try:
            _update_one(
                src,
                existing.get(name.lower()),
                metadata_dir,
                base_dir,
                history.get(name.lower(), set()),
                own_ids,
                backups,
                out,
            )
        except (DefinitionUpdateError, OSError) as e:
            logger.warning(f"Definition update skipped {name}: {e}")
            out.action = "skipped"
            out.details = [f"{e}"]
            out.failed_io = isinstance(e, OSError)
        report.files.append(out)
        for d in out.details:
            logger.info(f"Definition update {name} ({out.action}): {d}")

    report.backup_dir = backups.dir
    logger.info(
        "Definition update: "
        + ", ".join(
            f"{a} {len(report.by_action(a))}"
            for a in (
                "added",
                "replaced",
                "merged",
                "unchanged",
                "kept-deleted",
                "left-alone",
                "skipped",
            )
        )
    )
    return report


def _update_one(src, target, metadata_dir, base_dir, shipped, own_ids, backups, out):
    """Decide and apply the update of one bundled file (fills *out*)."""
    name = src.name
    bundled = src.read_bytes()
    base_path = base_dir / name

    if target is None:
        if base_path.exists():
            out.action = "kept-deleted"  # the user deleted it after receiving it
            return
        owner = own_ids.get(_identity(_rom(_parse(bundled))))
        if owner:
            out.action = "skipped"
            out.details = [f"you define this ROM in your own file {owner}"]
            return
        _write_atomic(metadata_dir / name, bundled)
        _write_atomic(base_path, bundled, durable=False)
        out.action = "added"
        return

    user = target.read_bytes()
    try:
        base = base_path.read_bytes()
    except FileNotFoundError:
        base = None

    def remember():
        # Our own copy: a torn write only makes it unreadable (= no base)
        if base != bundled:
            _write_atomic(base_path, bundled, durable=False)

    if user == bundled:
        remember()
        return  # unchanged (the common case: nothing to parse)
    u_ident, u_hash = _load(user)
    b_ident, b_hash = _load(bundled)
    if u_ident != b_ident and not _same_identity_as_base(u_ident, base):
        out.action = "skipped"
        out.details = ["your copy identifies a different ROM, so it was left as it is"]
        return
    if base is not None and user == base:
        # Exactly what NC Flash gave the user last time: never edited
        backups.save(target)
        _write_atomic(target, bundled)
        remember()
        out.action = "replaced"
        return
    if u_hash == b_hash:
        remember()
        return  # unchanged

    base_root = _read_base(base) if base is not None else None
    if base_root is None:
        if u_hash not in shipped:
            out.action = "left-alone"
            out.details = [f"you edited it; the new version is {src}"]
            out.details += [LAYOUT_DIFF + d for d in layout_differences(user, bundled)]
            return
        backups.save(target)
        _write_atomic(target, bundled)
        remember()
        out.action = "replaced"
        return
    if u_hash == file_hash(base):
        backups.save(target)  # same content as last time, another layout
        _write_atomic(target, bundled)
        remember()
        out.action = "replaced"
        return

    result = merge(bundled, user, base)
    merged_hash = file_hash(result.data)
    if merged_hash == u_hash:
        remember()
        return  # the user's copy already holds everything
    data = bundled if merged_hash == b_hash else result.data
    if data is not bundled:
        _validate(data, metadata_dir / f"{name}.{os.getpid()}.check")
    backups.save(target)
    _write_atomic(target, data)
    remember()
    out.action = "replaced"
    out.details = [f"kept your edit: {k}" for k in result.kept]
    out.details += [CONFLICT + c for c in result.conflicts]
    out.details += [REMOVED + r for r in result.removed]
    out.details += [REPLACED + r for r in result.replaced]
    if result.own_tables:
        out.details.append(f"kept {result.own_tables} table(s) you added")
    if out.details:
        out.action = "merged"


def _same_identity_as_base(ident, base: Optional[bytes]) -> bool:
    """The user's copy identifies the ROM the base did (the bundle may fix the ID)."""
    if base is None:
        return False
    try:
        return _load(base)[0] == ident
    except DefinitionUpdateError:
        return False


def layout_differences(user: bytes, bundled: bytes) -> List[str]:
    """
    Values that decide which bytes are written and differ between the user's
    copy and the bundled one, for entries both have (scalings by name, tables
    by address). Used to warn about files that are left alone.
    """
    try:
        U = _groups(_rom(_parse(user)))
        B = _groups(_rom(_parse(bundled)))
    except DefinitionUpdateError:
        return []
    out = []
    for key, b_list in B.items():
        u_list = U.get(key, [])
        if key[0] not in ("table", "scaling") or len(b_list) != 1 or len(u_list) != 1:
            continue
        b, u = b_list[0], u_list[0]
        layout = TABLE_LAYOUT if key[0] == "table" else SCALING_LAYOUT
        for attr in layout:
            if u.get(attr) != b.get(attr):
                out.append(
                    f"{_label(b)} {attr}: yours {u.get(attr)}, "
                    f"NC Flash's {b.get(attr)}"
                )
        if key[0] == "table":
            b_ax, u_ax = _elements(b), _elements(u)
            if len(b_ax) != len(u_ax) or any(
                x.get(a) != y.get(a) for x, y in zip(b_ax, u_ax) for a in AXIS_LAYOUT
            ):
                out.append(f"{_label(b)} axes differ")
    return out


def history_hashes(data: bytes) -> Dict[str, Set[str]]:
    """File name (lower case) -> content hashes of every released version."""
    doc = json.loads(data.decode("utf-8"))
    return {name.lower(): set(h) for name, h in doc["files"].items()}


# ------------------------------------------------------------------ startup


class _Lock:
    """One update at a time per workspace (two launches in the same second)."""

    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - self.path.stat().st_mtime > LOCK_STALE_SECONDS:
                self.path.unlink()
        except OSError:
            pass
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        return True

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)


def _should_run(stored: str) -> bool:
    """A newer release build than the last update, or forced by the environment."""
    from ..utils.constants import APP_VERSION
    from ..utils.update_check import parse_version

    if os.environ.get(FORCE_ENV) == "1":
        return True
    current = parse_version(APP_VERSION)
    if current is None:
        return False  # running from source: never touch the workspace
    last = parse_version(stored or "")
    return last is None or current > last


def update_workspace_definitions(
    settings, progress: Optional[Callable[[int, int], None]] = None
) -> Optional[UpdateReport]:
    """
    Startup entry point. Returns None when there was nothing to do.

    Args:
        progress: called with (files done, total) while files are processed
    """
    from ..utils.constants import APP_VERSION
    from ..utils.paths import get_app_root

    store = settings.settings
    if not _should_run(store.value(VERSION_KEY, "")):
        return None
    examples = get_app_root() / "examples"
    bundled_dir = examples / "metadata"
    if not bundled_dir.is_dir():
        return None

    workspace = Path(settings.get_workspace_directory())
    metadata_dir = Path(settings.get_metadata_directory())
    default_dir = workspace / "metadata"
    if os.path.normcase(os.path.abspath(metadata_dir)) != os.path.normcase(
        os.path.abspath(default_dir)
    ):
        # A folder the user chose (maybe their own repository): never write there
        store.setValue(VERSION_KEY, APP_VERSION)
        return UpdateReport(
            skipped_reason=(
                "This version of NC Flash comes with new and corrected ROM "
                f"definitions. Your definitions folder ({metadata_dir}) is not "
                "the workspace's own, so it was not changed. The new files are "
                f"in {bundled_dir}."
            )
        )
    metadata_dir.mkdir(parents=True, exist_ok=True)

    history: Dict[str, Set[str]] = {}
    history_path = examples / HISTORY_FILE
    if history_path.exists():
        try:
            history = history_hashes(history_path.read_bytes())
        except (ValueError, KeyError) as e:
            logger.warning(f"Ignoring unreadable {history_path.name}: {e}")

    with _Lock(workspace / LOCK_FILE) as locked:
        if not locked:
            logger.info("Definition update already running in another instance")
            return None
        report = update_definitions(
            bundled_dir, metadata_dir, workspace, history, progress=progress
        )
    failures = int(store.value(FAILURES_KEY, 0) or 0) + 1 if report.failed_io else 0
    if failures == 0 or failures >= MAX_ATTEMPTS:
        # Done, or a file that keeps failing (read-only?): stop retrying
        store.setValue(VERSION_KEY, APP_VERSION)
        failures = 0
    store.setValue(FAILURES_KEY, failures)
    return report
