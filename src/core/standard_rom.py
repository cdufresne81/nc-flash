"""
Standard ROMs: move a ROM (stock or tuned) onto the standard image of its line.

The MX-5 NC factory images come as 78 ROM IDs, but they reduce to 13
"standard" images: the latest revision Mazda issued for each software line.
Every other ID is either an older revision of a standard, or a variant of it
(gearbox, market, engine) that differs by a small byte preset.

This module does three things, none of which touches an ECU:

1. ``identify``: read the ROM ID from an image and say which standard it
   belongs to and which image the car would run.
2. ``build_base``: standard image + variant preset. The result must equal the
   variant's own release image outside the boot block (checked by hash).
3. ``convert``: move the user's edited tables onto that base, by table name,
   then run the release checks on the result.

It reads everything from a "standard pack" (one file, or a folder; see
``StandardPack``) built
by the nc-flash-re project. Rules of the conversion, all on the cautious side:

- An edit is a table whose bytes differ from the factory values of the ROM's
  own ID. It moves only if the table has one unambiguous name in both
  definitions, the same shape and scaling on both sides, and the SAME factory
  values in both programs. If Mazda changed that table between the two
  revisions, the edit is listed for the user to decide and is not moved.
- A table and its "(Data Integrity)" copy move together or not at all.
- Nothing outside defined tables is copied (patch code, feature blocks,
  unnamed bytes). Feature settings are never copied.
- An ID with no factory image of its variant on the standard is refused.
- The boot block is never changed.

Checksums are left as they are: the flash path recomputes them
(``src/ecu/flash_prep.py``).
"""

import hashlib
import json
import lzma
import re
import struct
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .definition_parser import load_definition
from .rom_definition import RomDefinition, Table, TableLayout

BOOT_BLOCK_END = 0x2000
MANIFEST_FORMAT = "ncflash-standard-manifest/1"
ROM_SIZE = 0x100000
PACK_FILE_SUFFIX = ".ncpack"
# A real pack unpacks to about 110 MB; refuse anything far beyond that.
PACK_FILE_MAX_BYTES = 512 * 1024 * 1024
PRESET_FORMAT = "ncflash-standard-preset/1"

_PAIR_SUFFIX = re.compile(r"\s*\(pair with [^()]*\)")
_INTEGRITY_TAG = " (Data Integrity)"
_RULE_SHA = re.compile(r"sha256 ([0-9a-f]{64})")
_RULE_FIRST = re.compile(r"first ([1-9][0-9]*) floats >= (-?[0-9]+(?:\.[0-9]+)?)")

WHY_FEATURE = "feature settings are not copied: set them again in the new ROM"
WHY_AMBIGUOUS = "the table name is used more than once in a definition"
WHY_MISSING = "the new ROM's definition has no table of this name"
WHY_SHAPE = "the table's size or scaling differs between the two definitions"
WHY_STOCK = (
    "Mazda changed this table between the two revisions: "
    "compare the two ROMs and decide by hand"
)


class StandardPackError(Exception):
    """The standard pack is missing, incomplete or inconsistent."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_name(name: str) -> str:
    """Table name without its "(pair with ...)" suffix.

    The suffix holds the address of the table's integrity copy, which differs
    between revisions; it is not part of what the table is called.
    """
    return _PAIR_SUFFIX.sub("", name)


def pair_key(name: str) -> str:
    """Name shared by a table and its "(Data Integrity)" copy."""
    return normalized_name(name).replace(_INTEGRITY_TAG, "")


def _engine_text(engine_cc: str) -> str:
    return "1.8 L" if str(engine_cc).startswith("17") else "2.0 L"


WRAPPED_DATA_TAG = b"MEMD"


def unwrap_rom(data: bytes) -> Tuple[bytes, str]:
    """The ROM image inside a file, and a note if something was removed.

    Some reading tools save the 1 MB image behind a short header made of
    tagged blocks (4-letter tag, 4-byte big-endian length, data): "INFO",
    "DRMI", "MEML", then "MEMD" holding the image to the end of the file.
    Such a file gives the image alone; any other file is returned as it is.
    """
    if data[:4] != b"INFO":
        return data, ""
    pos = 0
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        size = struct.unpack_from(">I", data, pos + 4)[0]
        start = pos + 8
        if tag == WRAPPED_DATA_TAG:
            if size and start + size == len(data):
                return (
                    data[start:],
                    f"The file has a {start}-byte header from the tool that "
                    "saved it; the ROM inside it was read.",
                )
            break
        if not tag.isalpha() or not tag.isupper():
            break
        pos = start + size
    return data, ""


@dataclass(frozen=True)
class Identity:
    """What a ROM is and where it goes under the standard-ROM plan."""

    rom_id: str
    engine_cc: str
    gearbox: str
    market_code: str
    standard: str
    method: str  # "standard", "standard_image", "factory" or "carried"
    target: Optional[str]  # ID of the image the car would run; None if carried
    moves_revision: bool
    # How the target differs from the standard ("5MT", "market 10", "1.8 L");
    # empty when the target is the standard image itself.
    preset_traits: Tuple[str, ...] = ()

    @property
    def convertible(self) -> bool:
        return self.target is not None

    @property
    def needs_move(self) -> bool:
        """True when the ROM belongs on another image than its own."""
        return self.target is not None and self.target != self.rom_id

    def _image_text(self) -> str:
        """The image the car would run, said in terms of its standard."""
        if self.target == self.standard:
            return self.standard
        traits = ", ".join(self.preset_traits)
        preset = f"the {traits} preset" if traits else "its preset"
        return f"{self.standard} with {preset} " f"(the same as Mazda's {self.target})"

    def summary(self) -> str:
        lines = [
            f"ROM ID: {self.rom_id} ({_engine_text(self.engine_cc)}, "
            f"{self.gearbox}, market {self.market_code})",
            f"Standard ROM: {self.standard}",
        ]
        if self.target is None:
            lines.append(
                "This variant has no factory image on its standard yet, "
                "so it cannot be converted."
            )
        elif self.target == self.rom_id:
            lines.append("This ROM is already the image to use: nothing to move.")
            if self.target != self.standard:
                lines.append(f"It is {self._image_text()}.")
        else:
            lines.append(f"Image to use: {self._image_text()}")
        if self.moves_revision and self.needs_move:
            lines.append(
                "This is a newer software revision than the one this ROM runs."
            )
        return "\n".join(lines)


@dataclass
class TableOutcome:
    table: str
    edited_bytes: int
    why: Optional[str] = None  # None = moved


@dataclass
class ConversionReport:
    identity: Optional[Identity] = None
    source_sha256: str = ""
    ok: bool = False
    reason: str = ""
    moved: List[TableOutcome] = field(default_factory=list)
    not_moved: List[TableOutcome] = field(default_factory=list)
    bytes_not_copied: Dict[str, int] = field(default_factory=dict)
    checks_refused: List[str] = field(default_factory=list)
    checks_warned: List[str] = field(default_factory=list)
    source_already_fails: List[str] = field(default_factory=list)
    result_sha256: str = ""

    def to_dict(self) -> dict:
        ident = self.identity
        return {
            "format": "ncflash-standard-convert-report/1",
            "ok": self.ok,
            "reason": self.reason,
            "original_rom_id": ident.rom_id if ident else None,
            "standard": ident.standard if ident else None,
            "image": ident.target if ident else None,
            "moves_revision": ident.moves_revision if ident else None,
            "source_sha256": self.source_sha256,
            "result_sha256": self.result_sha256,
            "moved": [vars(t) for t in self.moved],
            "not_moved": [vars(t) for t in self.not_moved],
            "bytes_not_copied": self.bytes_not_copied,
            "checks_refused": self.checks_refused,
            "checks_warned": self.checks_warned,
            "source_already_fails": self.source_already_fails,
        }

    def summary(self) -> str:
        lines = []
        if self.identity:
            lines.append(self.identity.summary())
        if self.moved or self.not_moved:
            lines.append(
                f"Edited tables moved: {len(self.moved)}. "
                f"Not moved: {len(self.not_moved)}."
            )
        features = [t for t in self.not_moved if t.why == WHY_FEATURE]
        if features:
            lines.append(
                f"  {len(features)} feature settings tables were left at the "
                "new ROM's values."
            )
        for t in self.not_moved:
            if t.why != WHY_FEATURE:
                lines.append(f"  NOT MOVED: {t.table} ({t.why})")
        other = {k: v for k, v in self.bytes_not_copied.items() if v}
        if other:
            parts = ", ".join(f"{v} in {k}" for k, v in other.items())
            lines.append(f"Bytes outside defined tables, not copied: {parts}.")
        for msg in self.checks_refused:
            lines.append(f"  REFUSED: {msg}")
        for msg in self.checks_warned:
            lines.append(f"  Warning: {msg}")
        if self.source_already_fails:
            lines.append(
                "The source ROM already fails these checks on its own "
                "(usually a table edited without its Data Integrity copy). "
                "Fix them in the source ROM first:"
            )
            lines.extend(f"  {msg}" for msg in self.source_already_fails)
        lines.append("Result: " + ("ready to review." if self.ok else self.reason))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The pack
# --------------------------------------------------------------------------- #


def _read_pack_file(path: Path) -> Dict[str, bytes]:
    """Every file of a one-file pack, read into memory. Nothing is extracted."""
    files: Dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(path, "r:xz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                total += member.size
                if total > PACK_FILE_MAX_BYTES:
                    raise StandardPackError(f"{path.name} is too large to be a pack")
                files[member.name] = tar.extractfile(member).read()
    except (OSError, EOFError, tarfile.TarError, lzma.LZMAError) as exc:
        raise StandardPackError(f"{path.name} could not be read: {exc}") from None
    return files


class StandardPack:
    """Everything the conversion needs: one pack file, or the same as a folder.

    Layout (written by nc-flash-re ``tools/standard_manifest.py``, as one
    ``.ncpack`` file with ``--pack-file`` or as a folder with ``--pack``)::

        manifest.json               release manifest (hashes, checks, ...)
        standard_manifest.json      ROM ID -> standard / image / preset
        presets/<image>_on_<standard>.json
        <ROMID>/<romid>_ncf_<release>.bin
        <ROMID>/<romid>.xml

    The pack file is that folder as a tar, xz-compressed. It is read into
    memory; nothing is written to disk except a definition, for the moment it
    takes to parse it.
    """

    def __init__(self, source):
        self.source = Path(source)
        # None = a folder, read file by file; else the pack file's contents.
        self._files: Optional[Dict[str, bytes]] = None
        if self.source.is_file():
            self._files = _read_pack_file(self.source)
        elif not self.source.is_dir():
            raise StandardPackError(f"{self.source} is not a pack file or folder")
        self._release = self._load_json("manifest.json")
        self._standard = self._load_json("standard_manifest.json")
        if self._standard.get("format") != MANIFEST_FORMAT:
            raise StandardPackError(
                "standard_manifest.json is not a format this version understands"
            )
        if "ids" not in self._release or "ids" not in self._standard:
            raise StandardPackError("the pack's manifests list no ROM IDs")
        self._images: Dict[str, bytes] = {}
        self._definitions: Dict[str, RomDefinition] = {}

    def _names(self, folder: str) -> List[str]:
        """Names of the files directly inside one folder of the pack."""
        if self._files is not None:
            prefix = folder + "/"
            return sorted(
                name[len(prefix) :]
                for name in self._files
                if name.startswith(prefix) and "/" not in name[len(prefix) :]
            )
        path = self.source / folder
        if not path.is_dir():
            return []
        return sorted(p.name for p in path.iterdir() if p.is_file())

    def _read(self, name: str) -> Optional[bytes]:
        """One file of the pack by its relative name; None if it is not there."""
        if self._files is not None:
            return self._files.get(name)
        path = self.source / name
        return path.read_bytes() if path.is_file() else None

    def _load_json(self, name: str) -> dict:
        try:
            data = self._read(name)
            if data is None:
                raise StandardPackError(f"{name} is missing from {self.source}")
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise StandardPackError(f"{name} could not be read: {exc}") from None

    @property
    def rom_ids(self) -> List[str]:
        return sorted(self._standard["ids"])

    def release_entry(self, rom_id: str) -> dict:
        try:
            return self._release["ids"][rom_id]
        except KeyError:
            raise StandardPackError(
                f"{rom_id} is not in the release manifest"
            ) from None

    def image(self, rom_id: str) -> bytes:
        """The release image of an ID, verified against the manifest hash."""
        if rom_id not in self._images:
            entry = self.release_entry(rom_id)
            start = f"{rom_id.lower()}_ncf_"
            found = [
                name
                for name in self._names(rom_id)
                if name.startswith(start) and name.endswith(".bin")
            ]
            if len(found) != 1:
                raise StandardPackError(f"no single image for {rom_id} in the pack")
            try:
                data = self._read(f"{rom_id}/{found[0]}")
            except OSError as exc:
                raise StandardPackError(
                    f"the {rom_id} image could not be read: {exc}"
                ) from None
            if data is None or sha256(data) != entry.get("image_sha256"):
                raise StandardPackError(
                    f"the {rom_id} image does not match the pack's manifest"
                )
            self._images[rom_id] = data
        return self._images[rom_id]

    def definition(self, rom_id: str) -> RomDefinition:
        if rom_id not in self._definitions:
            name = f"{rom_id}/{rom_id.lower()}.xml"
            if self._files is None:
                path = self.source / name
                if not path.is_file():
                    raise StandardPackError(f"no definition for {rom_id} in the pack")
                self._definitions[rom_id] = load_definition(str(path))
            else:
                data = self._files.get(name)
                if data is None:
                    raise StandardPackError(f"no definition for {rom_id} in the pack")
                # The parser reads a file: give it one for as long as it parses.
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / f"{rom_id.lower()}.xml"
                    path.write_bytes(data)
                    self._definitions[rom_id] = load_definition(str(path))
        return self._definitions[rom_id]

    def no_transfer_ranges(self, rom_id: str) -> List[Tuple[int, int]]:
        out = []
        for item in self.release_entry(rom_id).get("no_transfer", []):
            out.append((int(item["addr"], 16), int(item["end"], 16)))
        return out

    def preset(self, entry: dict) -> Optional[dict]:
        rel = entry.get("preset")
        if not rel:
            return None
        preset = self._load_json(rel)
        if preset.get("format") != PRESET_FORMAT:
            raise StandardPackError(f"{rel} is not a preset this version understands")
        return preset

    def identify(self, rom: bytes) -> Optional[Identity]:
        """Which ID this image is, read from the ID field of its header."""
        hits = []
        for rom_id, entry in self._release["ids"].items():
            if rom_id not in self._standard["ids"]:
                continue
            try:
                addr = int(entry["rom_id_addr"], 16)
            except (KeyError, ValueError):
                continue
            raw = rom_id.encode("ascii")
            if rom[addr : addr + len(raw)] == raw:
                hits.append(rom_id)
        if not hits:
            return None
        rom_id = max(hits, key=len)
        e = self._standard["ids"][rom_id]
        traits: Tuple[str, ...] = ()
        target = e.get("target")
        std = self._standard["ids"].get(e["standard"])
        aim = self._standard["ids"].get(target) if target else None
        if std and aim and target != e["standard"]:
            found = []
            if aim.get("gearbox") != std.get("gearbox"):
                found.append(str(aim.get("gearbox", "")))
            if aim.get("market_code") != std.get("market_code"):
                found.append(f"market {aim.get('market_code', '')}")
            if _engine_text(aim.get("engine_cc", "")) != _engine_text(
                std.get("engine_cc", "")
            ):
                found.append(_engine_text(aim.get("engine_cc", "")))
            traits = tuple(found)
        return Identity(
            rom_id=rom_id,
            engine_cc=str(e.get("engine_cc", "")),
            gearbox=str(e.get("gearbox", "")),
            market_code=str(e.get("market_code", "")),
            standard=e["standard"],
            method=e["method"],
            target=e.get("target"),
            moves_revision=bool(e.get("moves_revision")),
            preset_traits=traits,
        )

    def build_base(self, identity: Identity) -> bytes:
        """Standard image + variant preset, proven equal to the variant image."""
        if identity.target is None:
            raise StandardPackError(
                f"{identity.rom_id} has no factory image on {identity.standard}"
            )
        base = bytearray(self.image(identity.standard))
        boot = bytes(base[:BOOT_BLOCK_END])
        preset = self.preset(self._standard["ids"][identity.rom_id])
        if preset is not None:
            if preset.get("standard_sha256") != sha256(bytes(base)):
                raise StandardPackError("a preset was made from another standard image")
            for addr_text, hex_bytes in preset["runs"]:
                addr = int(addr_text, 16)
                raw = bytes.fromhex(hex_bytes)
                if addr < BOOT_BLOCK_END or addr + len(raw) > len(base):
                    raise StandardPackError("a preset writes outside the allowed area")
                base[addr : addr + len(raw)] = raw
        want = self.image(identity.target)
        if bytes(base[BOOT_BLOCK_END:]) != want[BOOT_BLOCK_END:]:
            raise StandardPackError(
                f"standard + preset does not give the {identity.target} image"
            )
        if bytes(base[:BOOT_BLOCK_END]) != boot:
            raise StandardPackError("the boot block changed")
        return bytes(base)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

# One stored piece of a table: (address, byte length, signature). The
# signature must be equal on both sides for a table to move.
_Part = Tuple[int, int, tuple]


def _part(table: Table, definition: RomDefinition, role: str) -> Optional[_Part]:
    if table.layout is not TableLayout.CONTIGUOUS or table.elements <= 0:
        return None
    scaling = definition.get_scaling(table.scaling)
    if scaling is None:
        return None
    try:
        addr = table.address_int
    except (ValueError, TypeError):
        return None
    signature = (
        role,
        table.elements,
        scaling.storagetype,
        scaling.endian,
        scaling.toexpr,
    )
    return addr, table.elements * scaling.bytes_per_element, signature


def table_parts(definition: RomDefinition) -> Tuple[Dict[str, List[_Part]], set]:
    """({normalized table name: parts}, names used more than once)."""
    out: Dict[str, List[_Part]] = {}
    ambiguous = set()
    for table in definition.tables:
        name = normalized_name(table.name)
        parts = [_part(table, definition, "data")]
        for index, child in enumerate(table.children):
            role = f"axis{index}:{child.axis_type.value if child.axis_type else ''}"
            parts.append(_part(child, definition, role))
        parts = [p for p in parts if p is not None]
        if not parts:
            continue
        if name in out:
            ambiguous.add(name)
        out[name] = parts
    return out, ambiguous


def _touches(addr: int, length: int, ranges: List[Tuple[int, int]]) -> bool:
    return any(addr <= hi and lo <= addr + length - 1 for lo, hi in ranges)


def _same(rom_a: bytes, part_a: _Part, rom_b: bytes, part_b: _Part) -> bool:
    return (
        rom_a[part_a[0] : part_a[0] + part_a[1]]
        == rom_b[part_b[0] : part_b[0] + part_b[1]]
    )


# --------------------------------------------------------------------------- #
# Release checks (nc-flash-re requirements R3 / R4)
# --------------------------------------------------------------------------- #


def _inside(image: bytes, addr, length) -> bool:
    return (
        isinstance(addr, int)
        and isinstance(length, int)
        and addr >= 0
        and length > 0
        and addr + length <= len(image)
    )


def _latch_ok(image: bytes, check: dict) -> bool:
    if check.get("cells") != 8 or type(check.get("min")) is not int:
        return False
    pool, desc, axis, data = (
        int(check[k], 16) for k in ("pool", "desc", "axis", "addr")
    )
    if not (
        _inside(image, pool, 4)
        and _inside(image, desc, 12)
        and _inside(image, axis, 32)
        and _inside(image, data, 16)
    ):
        return False
    head, axis_ptr, data_ptr = struct.unpack_from(">III", image, desc)
    if (
        struct.unpack_from(">I", image, pool)[0] != desc
        or head != 0x00080000
        or axis_ptr != axis
        or data_ptr != data
    ):
        return False
    xs = struct.unpack_from(">8f", image, axis)
    if not all(x == x and abs(x) != float("inf") for x in xs):
        return False
    if any(b <= a for a, b in zip(xs, xs[1:])):
        return False
    return all(v >= check["min"] for v in struct.unpack_from(">8H", image, data))


def _check_ok(image: bytes, check: dict) -> Tuple[bool, bool]:
    """(passes, understood). A check this code does not understand refuses."""
    try:
        addr = int(check["addr"], 16)
        kind, rule = check.get("kind"), check.get("rule")
        if kind == "latch":
            return _latch_ok(image, check), True
        if kind is not None:
            return False, False
        if rule == "equal":
            other, length = int(check["pair"], 16), check["len"]
            if type(length) is not int or not (
                _inside(image, addr, length) and _inside(image, other, length)
            ):
                return False, False
            return (
                image[addr : addr + length] == image[other : other + length],
                True,
            )
        match = _RULE_SHA.fullmatch(rule or "")
        if match:
            end = int(check["end"], 16)
            if not (end >= addr and _inside(image, addr, end - addr + 1)):
                return False, False
            return sha256(image[addr : end + 1]) == match.group(1), True
        if rule == "float > 50":
            if not _inside(image, addr, 4):
                return False, False
            return struct.unpack_from(">f", image, addr)[0] > 50, True
        match = _RULE_FIRST.fullmatch(rule or "")
        if match:
            count, limit = int(match.group(1)), float(match.group(2))
            if not _inside(image, addr, 4 * count):
                return False, False
            values = struct.unpack_from(f">{count}f", image, addr)
            return all(v >= limit for v in values), True
        return False, False
    except (KeyError, ValueError, TypeError, struct.error):
        return False, False


def run_release_checks(
    image: bytes, entry: dict, parts: Optional[Dict[str, List[_Part]]] = None
) -> Tuple[List[str], List[str]]:
    """(refused, warned) messages of the release manifest's checks on an image.

    Covers the manifest's ``code_pin`` hashes and its ``checks`` list. A check
    with an unknown kind, rule or level counts as refused.
    """
    refused, warned = [], []

    def label(check: dict) -> str:
        text = f"{check.get('what')} {check.get('addr')}"
        try:
            addr = int(check["addr"], 16)
        except (KeyError, ValueError, TypeError):
            return text
        for name, table in (parts or {}).items():
            if any(p[0] <= addr < p[0] + p[1] for p in table):
                return f'{text} ("{name}")'
        return text

    for pin in entry.get("code_pin", []):
        try:
            lo, hi = int(pin["addr"], 16), int(pin["end"], 16)
            good = hi >= lo and _inside(image, lo, hi - lo + 1)
            good = good and sha256(image[lo : hi + 1]) == pin["sha256"]
        except (KeyError, ValueError, TypeError):
            good = False
        if not good:
            refused.append(f"feature code changed: {pin.get('what')} {pin.get('addr')}")

    checks = entry.get("checks")
    if not isinstance(checks, list):
        return refused + ["the release manifest has no check list"], warned
    for check in checks:
        if not isinstance(check, dict):
            refused.append("malformed check in the release manifest")
            continue
        passes, understood = _check_ok(image, check)
        level = check.get("level")
        if not understood or level not in ("REFUSE", "WARN"):
            refused.append(label(check) + " (check not understood)")
        elif not passes:
            (refused if level == "REFUSE" else warned).append(label(check))
    return refused, warned


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


def _region(addr: int, code_end: int, features_lo: int, checksum_lo: int) -> str:
    if addr < BOOT_BLOCK_END:
        return "the boot block"
    if addr < code_end:
        return "program code"
    if addr >= checksum_lo:
        return "the checksum table"
    if addr >= features_lo:
        return "the feature area"
    return "calibration no table covers"


def convert(
    pack: StandardPack, source: bytes
) -> Tuple[Optional[bytes], ConversionReport]:
    """Move a ROM onto the standard image of its line.

    Returns (new image or None, report). None means refused; the report says
    why. The source is never modified.
    """
    report = ConversionReport(source_sha256=sha256(source))
    identity = pack.identify(source)
    if identity is None:
        report.reason = "This ROM's ID is not one of the IDs the pack knows."
        return None, report
    report.identity = identity
    if not identity.convertible:
        report.reason = (
            "No factory image of this variant exists on its standard ROM. "
            "It cannot be converted until that variant has been reviewed."
        )
        return None, report

    stock = pack.image(identity.rom_id)
    if len(source) != len(stock):
        report.reason = "This file is not the size of a full ROM."
        return None, report
    base = pack.build_base(identity)
    out = bytearray(base)

    src_parts, src_ambiguous = table_parts(pack.definition(identity.rom_id))
    dst_parts, dst_ambiguous = table_parts(pack.definition(identity.target))
    src_locked = pack.no_transfer_ranges(identity.rom_id)
    dst_locked = pack.no_transfer_ranges(identity.target)

    covered = bytearray(len(source))
    plan = {}
    for name, parts in sorted(src_parts.items()):
        for addr, length, _sig in parts:
            covered[addr : addr + length] = b"\x01" * length
        edited = [p for p in parts if not _same(source, p, stock, p)]
        if not edited:
            continue
        count = sum(
            1
            for addr, length, _sig in edited
            for k in range(addr, addr + length)
            if source[k] != stock[k]
        )
        why, target_parts = None, None
        if any(_touches(a, n, src_locked) for a, n, _s in parts):
            why = WHY_FEATURE
        elif name in src_ambiguous or name in dst_ambiguous:
            why = WHY_AMBIGUOUS
        elif name not in dst_parts:
            why = WHY_MISSING
        else:
            target_parts = dst_parts[name]
            if [p[2] for p in parts] != [p[2] for p in target_parts]:
                why = WHY_SHAPE
            elif any(_touches(a, n, dst_locked) for a, n, _s in target_parts):
                why = WHY_FEATURE
            elif not all(_same(stock, p, base, q) for p, q in zip(parts, target_parts)):
                why = WHY_STOCK
        plan[name] = (parts, target_parts, edited, count, why)

    blocked = {pair_key(n): n for n, item in plan.items() if item[4]}
    for name, (parts, target_parts, edited, count, why) in sorted(plan.items()):
        if why is None and pair_key(name) in blocked:
            why = f'its integrity pair "{blocked[pair_key(name)]}" could not be moved'
        if why is not None:
            report.not_moved.append(TableOutcome(name, count, why))
            continue
        for part, target in zip(parts, target_parts):
            if part in edited:
                out[target[0] : target[0] + target[1]] = source[
                    part[0] : part[0] + part[1]
                ]
        report.moved.append(TableOutcome(name, count))

    entry = pack.release_entry(identity.rom_id)
    code_end = int(entry.get("rom_id_addr", "0"), 16) & ~0xFFF
    features_lo = min((lo for lo, _hi in src_locked), default=len(source)) & ~0xFFF
    checksum_lo = len(source) - 0x9B0  # the checksum table at 0xFF650
    for k in range(len(source)):
        if source[k] != stock[k] and not covered[k]:
            where = _region(k, code_end, features_lo, checksum_lo)
            report.bytes_not_copied[where] = report.bytes_not_copied.get(where, 0) + 1

    result = bytes(out)
    refused, warned = run_release_checks(
        result, pack.release_entry(identity.target), dst_parts
    )
    report.checks_refused, report.checks_warned = refused, warned
    # The same checks on the source, minus the feature-code hashes: a ROM from
    # before the feature release has no feature code and that is not a fault.
    report.source_already_fails = [
        msg
        for msg in run_release_checks(source, entry, src_parts)[0]
        if not msg.startswith("feature code changed")
    ]
    if result[:BOOT_BLOCK_END] != base[:BOOT_BLOCK_END]:
        refused.append("the boot block changed")
    if refused:
        report.reason = "The converted ROM fails the release checks."
        return None, report
    report.ok = True
    report.result_sha256 = sha256(result)
    return result, report
