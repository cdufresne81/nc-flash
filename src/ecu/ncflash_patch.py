"""NC Flash patch format v2 — the generic applier.

A v2 patch file (``ncflash-patch/2``, built and verified by the nc-flash-re
repo: spec ``docs/modifications/ncflash_patch_format_v2.md``, reference applier
``tools/ncflash_apply.py``) carries everything needed to apply a patch safely:
the bytes to write, the bytes that must already be there, a pin over the ROM's
code, and every tune check as a typed rule. This module knows nothing about any
ROM; adding a ROM ID means shipping one more file. Its semantics follow the
reference applier line for line, so both give the same verdict and output.

Pure bytes in, bytes out, except :func:`apply_file` and :func:`write_new_file`,
which read the input and write a NEW output file. Nothing here talks to an ECU.

Steps (nothing is written unless the verdict is OK or WARN):

 1. patch file: sha256 == the pin, format known, structure valid
 2. input: 1,048,576 bytes, the ROM ID at ``rom_id_addr``
 3. ranges: input == old -> write; == new -> INFO; romdrop_data mismatch ->
    WARN (input kept); else REFUSE. The code ranges hold
    ``installed_signature`` -> REFUSE "already installed"; a ``previous``
    signature -> REFUSE (upgrade not supported)
 4. consent: a tyre_restore write needs the user's consent id (else REFUSE)
 5. code pin (REFUSE, names the differing 4 KB blocks), data pin (its level)
 6. checksum table well-formed (else REFUSE); stale input records -> WARN
 7. checks on "input" (the input with tyre_restore applied and every checksum
    record recomputed) or "output" (the image as it will be written); every
    failure is reported
 8. write the ranges into a copy, recompute every checksum record
 9. re-check the output: every written range == new, every other byte ==
    input except sum words, post checks pass, no stale record
10. save to a NEW file, read it back; the input re-read unchanged

Every WARN carries a class (nc-flash-re review 2026-10-07 finding 2): routine
(an accepted consent, a check the file marks ``routine: true``, stale input
checksums) or non-routine (everything else: a Romdrop data byte that differs,
the data pin, a gear-margin or preflight WARN ...). The Patches window shows
the non-routine WARNs apart and makes the user acknowledge each one.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .constants import CHECKSUM_MAGIC

FORMATS = ("ncflash-patch/2",)
ROM_SIZE = 0x100000
WRITE_KINDS = ("code", "cal", "settings", "erase", "hook")
ALL_KINDS = WRITE_KINDS + ("tyre_restore", "romdrop", "romdrop_data")
LEVELS = ("refuse", "warn", "info")

REFUSE, WARN, INFO = "REFUSE", "WARN", "INFO"
ROW = {"refuse": REFUSE, "warn": WARN, "info": INFO}

CK_ALGORITHM = "ncflash-sum32-v1"
CK_MAGIC = CHECKSUM_MAGIC


class PatchFormatError(ValueError):
    """The patch file is malformed, of an unknown format, or not the pinned one."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _h(a: int) -> str:
    return f"0x{a:06X}"


def num(x) -> int:
    """An address / integer field: int, or a '0x..' (hex) / decimal string."""
    if isinstance(x, bool):
        raise PatchFormatError(f"not a number: {x!r}")
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x, 16) if x.lower().startswith("0x") else int(x)
        except ValueError:
            raise PatchFormatError(f"not a number: {x!r}")
    raise PatchFormatError(f"not a number: {x!r}")


def _f32(d: bytes, a: int) -> float:
    return struct.unpack_from(">f", d, a)[0]


def _floats(d: bytes, a: int, n: int) -> tuple:
    return struct.unpack_from(f">{n}f", d, a)


# ---------------------------------------------------------------- report


ROUTINE, NON_ROUTINE = "routine", "non-routine"


@dataclass
class Finding:
    level: str  # REFUSE | WARN | INFO
    message: str
    cls: str | None = None  # routine | non-routine for a WARN, else None


@dataclass
class Report:
    rows: list = field(default_factory=list)

    def add(self, level: str, message: str, routine: bool = False):
        cls = (ROUTINE if routine else NON_ROUTINE) if level == WARN else None
        self.rows.append(Finding(level, message, cls))

    def findings(self) -> list:
        """[(level, message, class)]: the machine-readable form."""
        return [(r.level, r.message, r.cls) for r in self.rows]

    def non_routine(self) -> list:
        """The non-routine WARN messages: each one needs the user's acknowledgement."""
        return [r.message for r in self.rows if r.cls == NON_ROUTINE]

    @property
    def refused(self) -> bool:
        return any(r.level == REFUSE for r in self.rows)

    @property
    def verdict(self) -> str:
        if self.refused:
            return "REFUSED"
        return "WARN" if any(r.level == WARN for r in self.rows) else "OK"


# ---------------------------------------------------------------- checksums


class ChecksumLayoutError(Exception):
    """The checksum table is not the expected structure: nothing is fixed."""


@dataclass(frozen=True)
class ChecksumTable:
    """ncflash-sum32-v1: ``records`` records of (start, end incl., sum) BE32
    plus a sentinel record; valid when sum(words) + sum == 0x5AA5A55A."""

    table: int
    records: int
    magic: int = CK_MAGIC

    @property
    def table_last(self) -> int:
        return self.table + 12 * (self.records + 1) - 1


def ck_records(d: bytes, ck: ChecksumTable) -> list:
    """[(index, start, end_incl, stored)] after the structural validation of
    nc-flash-re tools/fix_checksums.records()."""
    t, n = ck.table, ck.records
    out = []
    for i in range(n):
        a, b, c = struct.unpack_from(">III", d, t + i * 12)
        if a >= ROM_SIZE:
            raise ChecksumLayoutError(
                f"record {i}: start {a:#x} is a sentinel; expected {n} records"
            )
        if a % 4 or b % 4 != 3 or b < a or b >= ROM_SIZE:
            raise ChecksumLayoutError(f"record {i}: bad range {a:#07x}-{b:#07x}")
        if a <= ck.table_last and b >= t:
            raise ChecksumLayoutError(
                f"record {i}: range {a:#07x}-{b:#07x} covers the table itself "
                "(cannot converge)"
            )
        out.append((i, a, b, c))
    if struct.unpack_from(">I", d, t + n * 12)[0] < ROM_SIZE:
        raise ChecksumLayoutError(
            f"no sentinel at {t + n * 12:#x}; expected exactly {n} records"
        )
    spans = sorted((a, b, i) for i, a, b, _ in out)
    for (_a1, b1, i1), (a2, _b2, i2) in zip(spans, spans[1:]):
        if a2 <= b1:
            raise ChecksumLayoutError(f"records {i1} and {i2} overlap")
    return out


def ck_calc(d: bytes, a: int, b: int, magic: int = CK_MAGIC) -> int:
    """The sum word for [a, b] inclusive: magic - sum of the BE32 words.

    The same arithmetic as the flash path's ``checksum.mazda_checksum`` (a test
    pins them equal on every record), summed with numpy: the applier recomputes
    the table six times per check, which the pure-Python sum made take seconds.
    """
    n = (b + 1 - a) // 4
    total = int(np.frombuffer(d, dtype=">u4", count=n, offset=a).sum(dtype=np.uint64))
    return (magic - total) & 0xFFFFFFFF


def ck_stale(d: bytes, ck: ChecksumTable) -> list:
    """[(index, start, end, stored, computed)] for every invalid record."""
    return [
        (i, a, b, c, v)
        for i, a, b, c in ck_records(d, ck)
        if (v := ck_calc(d, a, b, ck.magic)) != c
    ]


def ck_sum_words(ck: ChecksumTable) -> set:
    return {ck.table + i * 12 + 8 + k for i in range(ck.records) for k in range(4)}


def diff_addrs(x: bytes, y: bytes) -> list:
    """Addresses where the equal-length byte strings x and y differ, ascending."""
    a = np.frombuffer(x, dtype=np.uint8)
    b = np.frombuffer(y, dtype=np.uint8)
    return np.flatnonzero(a != b).tolist()


def ck_fix(d: bytes, ck: ChecksumTable) -> bytes:
    """Bytes with every stale sum word recomputed (only the sum words change)."""
    out = bytearray(d)
    for i, _a, _b, _c, v in ck_stale(d, ck):
        struct.pack_into(">I", out, ck.table + i * 12 + 8, v)
    allowed = ck_sum_words(ck)
    if any(k not in allowed for k in diff_addrs(bytes(out), d)):
        raise ChecksumLayoutError("internal error: bytes outside the sum words changed")
    if ck_stale(bytes(out), ck):
        raise ChecksumLayoutError("internal error: table still stale after one pass")
    return bytes(out)


# ---------------------------------------------------------------- pins


@dataclass(frozen=True)
class Pin:
    regions: tuple
    exclude: tuple
    sha256: str | None
    block_size: int
    blocks: dict
    level: str


def pin_segments(regions, exclude) -> list:
    """[(lo, hi)] inclusive: the region bytes minus the excluded ones."""
    ex = sorted(exclude)
    out = []
    for lo, hi in regions:  # regions in listed order, ascending within each
        a = lo
        for elo, ehi in ex:
            if ehi < a or elo > hi:
                continue
            if elo > a:
                out.append((a, elo - 1))
            a = max(a, ehi + 1)
            if a > hi:
                break
        if a <= hi:
            out.append((a, hi))
    return out


def pin_hashes(d: bytes, regions, exclude, block_size: int) -> tuple:
    """(sha256 hex over every pinned byte, {block address: first 16 hex of the
    block's sha256}). Excluded bytes are skipped, not zeroed."""
    whole = hashlib.sha256()
    blocks: dict = {}
    for lo, hi in pin_segments(regions, exclude):
        whole.update(d[lo : hi + 1])
        a = lo
        while a <= hi:
            b = min(hi, (a // block_size + 1) * block_size - 1)
            blocks.setdefault(a - a % block_size, hashlib.sha256()).update(d[a : b + 1])
            a = b + 1
    return whole.hexdigest(), {k: v.hexdigest()[:16] for k, v in blocks.items()}


def blocks_shared(regions, block_size: int) -> bool:
    """True if two regions of one pin touch the same block (ambiguous keys)."""
    seen: set = set()
    for lo, hi in regions:
        mine = set(range(lo - lo % block_size, hi + 1, block_size))
        if mine & seen:
            return True
        seen |= mine
    return False


def pin_mismatch(d: bytes, pin: Pin) -> list:
    """[] when the pin holds, else the differing block addresses (or a note)."""
    got, blocks = pin_hashes(d, pin.regions, pin.exclude, pin.block_size)
    if pin.sha256 and got == pin.sha256 and blocks == pin.blocks:
        return []
    bad = sorted(
        k for k in set(blocks) | set(pin.blocks) if blocks.get(k) != pin.blocks.get(k)
    )
    return bad or ["(whole-region hash)"]


def _report_pin(d, pin: Pin, what: str, rep: Report, level: str) -> bool:
    bad = pin_mismatch(d, pin)
    if not bad:
        return True
    if isinstance(bad[0], int):
        first = f"{_h(bad[0])}-{_h(bad[0] + pin.block_size - 1)}"
    else:
        first = str(bad[0])
    more = ""
    if len(bad) > 1:
        listed = ", ".join(_h(x) for x in bad[1:6] if isinstance(x, int))
        more = (
            f" (+{len(bad) - 1} more blocks: {listed}{' ...' if len(bad) > 6 else ''})"
        )
    why = (
        ": code a tune never edits (another Romdrop version, another patch, a "
        "damaged file or a code edit)"
        if what == "code pin"
        else ": a byte outside every table definition changed (a tune made with "
        "other definitions, or another patch); kept as it is"
    )
    rep.add(
        ROW[level],
        f"{what}: the {pin.block_size // 1024} KB block {first} differs from the "
        f"reference base{more}{why}",
    )
    return False


# ---------------------------------------------------------------- checks


def _fin(x) -> bool:
    return math.isfinite(x)


def c_bytes_eq(d, c):
    a, b, n = num(c["a"]), num(c["b"]), num(c["len"])
    ok = d[a : a + n] == d[b : b + n]
    return ok, f"{_h(a)} = {d[a:a + n].hex()[:32]} vs {_h(b)} = {d[b:b + n].hex()[:32]}"


def c_bytes_is(d, c):
    a, want = num(c["addr"]), bytes.fromhex(c["hex"])
    got = d[a : a + len(want)]
    if got == want:
        return True, f"{_h(a)} as expected"
    i = next(j for j in range(len(want)) if got[j] != want[j])
    return (
        False,
        f"at {_h(a + i)} expected {want[i:i + 8].hex()} found {got[i:i + 8].hex()}",
    )


def c_fsub(d, c):
    a, b, cc = num(c["a"]), num(c["b"]), num(c["c"])
    try:
        want = struct.pack(">f", _f32(d, b) - _f32(d, cc))
    except OverflowError:
        return False, f"f({_h(b)}) - f({_h(cc)}) overflows a float"
    return d[a : a + 4] == want, (
        f"{_h(a)} = {d[a:a + 4].hex()} vs f({_h(b)}) - f({_h(cc)}) = {want.hex()}"
    )


def c_f_gt(d, c):
    v = _f32(d, num(c["addr"]))
    return _fin(v) and v > c["x"], (
        f"{_h(num(c['addr']))} = {v:g} (must be finite and > {c['x']:g})"
    )


def c_f_le(d, c):
    v = _f32(d, num(c["addr"]))
    return _fin(v) and v <= c["x"], (
        f"{_h(num(c['addr']))} = {v:g} (must be finite and <= {c['x']:g})"
    )


def c_f_finite(d, c):
    v = _f32(d, num(c["addr"]))
    return _fin(v), f"{_h(num(c['addr']))} = {v:g}"


def c_f_desc(d, c):
    a, n = num(c["addr"]), num(c["n"])
    v = _floats(d, a, n)
    ok = all(_fin(x) and x > 0 for x in v) and all(
        v[i] > v[i + 1] for i in range(n - 1)
    )
    return ok, f"{n} floats at {_h(a)}: " + " ".join(f"{x:.4g}" for x in v)


def c_f_range(d, c):
    a, n = num(c["addr"]), num(c.get("n", 1))
    v = _floats(d, a, n)
    if c.get("min_exclusive"):

        def lo_ok(x):
            return x > c["min"]

    else:

        def lo_ok(x):
            return x >= c["min"]

    ok = all(_fin(x) and lo_ok(x) and x <= c["max"] for x in v)
    sign = "> " if c.get("min_exclusive") else ">= "
    return ok, (
        f"{n} float(s) at {_h(a)}: "
        + " ".join(f"{x:.4g}" for x in v)
        + f" (each must be finite, {sign}{c['min']:g} and <= {c['max']:g})"
    )


def c_f_close(d, c):
    a = num(c["a"])
    if c.get("skip_if_blank") and d[a : a + 4] == b"\xff" * 4:
        return True, f"{_h(a)} blank (not used)"
    v = _f32(d, a)
    ref = _f32(d, num(c["b"])) if "b" in c else c["value"]
    ok = _fin(v) and abs(v - ref) <= c["tol"]
    return ok, f"{_h(a)} = {v:g} vs {ref:g} (tolerance {c['tol']:g})"


def c_f_any_le(d, c):
    a, n = num(c["addr"]), num(c["n"])
    v = _floats(d, a, n)
    hits = sum(1 for x in v if x <= c["x"])
    return any(x <= c["x"] for x in v), (
        f"{n} floats at {_h(a)}, at or below {c['x']:g}: {hits}"
    )


def c_table_finite(d, c):
    a, n = num(c["addr"]), num(c["nx"]) * num(c["ny"])
    bad = [i for i, x in enumerate(_floats(d, a, n)) if not _fin(x)]
    if not bad:
        return True, f"{n} cells finite"
    nx = num(c["nx"])
    return False, (
        f"{len(bad)} non-finite cell(s), first {_h(a + 4 * bad[0])} "
        f"(x point {bad[0] % nx + 1}, y point {bad[0] // nx + 1})"
    )


def c_table_all_eq(d, c):
    a, n = num(c["addr"]), num(c["nx"]) * num(c["ny"])
    v = _floats(d, a, n)
    ok = all(x == c["value"] for x in v)
    return ok, f"{n} cells {min(v):.2f} to {max(v):.2f}" + (
        "" if ok else f", not all {c['value']:g}"
    )


_FMT = {
    "float": ">f",
    "uint8": ">B",
    "int8": ">b",
    "uint16": ">H",
    "int16": ">h",
    "uint32": ">I",
    "int32": ">i",
}


def _read(d, addr, typ):
    """A value of a definition storagetype, big-endian."""
    return struct.unpack_from(_FMT[typ], d, num(addr))[0]


def c_setting_le_cal(d, c):
    s = _read(d, c["setting"]["addr"], c["setting"]["type"])
    k = _read(d, c["cal"]["addr"], c["cal"]["type"])
    return s <= k - c["margin"], (
        f"{c['setting']['addr']} = {s:g} must be <= {c['cal']['addr']} = {k:g} "
        f"- {c['margin']:g}"
    )


def c_u8_eq(d, c):
    a = num(c["addr"])
    return d[a] == c["value"], f"{_h(a)} = {d[a]} (expected {c['value']})"


def _words(d: bytes) -> np.ndarray:
    return np.frombuffer(d[: len(d) // 4 * 4], dtype=">u4")


def c_no_word_in(d, c):
    """No 4-aligned 32-bit word holds a value in [lo, hi], except the words at
    except_words, inside except_ranges and inside [lo, hi] itself."""
    lo, hi = num(c["lo"]), num(c["hi"])
    exw = {num(x) for x in c.get("except_words", [])}
    exr = [(num(a), num(b)) for a, b in c.get("except_ranges", [])] + [(lo, hi)]
    w = _words(d)
    cand = (np.flatnonzero((w >= lo) & (w <= hi)) * 4).tolist()
    hits = [x for x in cand if x not in exw and not any(a <= x <= b for a, b in exr)]
    if not hits:
        return True, "no word points into " + f"{lo:#x}-{hi:#x}"
    return False, f"words holding a value in {lo:#x}-{hi:#x} at " + ", ".join(
        _h(x) for x in hits[:8]
    )


def c_word_only_at(d, c):
    """The 4-aligned 32-bit value occurs in the image exactly at addrs."""
    v, want = num(c["value"]), sorted(num(x) for x in c["addrs"])
    at = (np.flatnonzero(_words(d) == v) * 4).tolist()
    return at == want, f"word {v:#010x} at " + (
        ", ".join(_h(x) for x in at) or "nowhere"
    )


def c_pcrel_loader_only(d, c):
    """Exactly one ``mov.l @(disp,PC),Rn`` loads the word at lit, at site. A
    mov.l is any even address a with d[a] >> 4 == 0xD; it loads
    (a & ~3) + 4 + 4 * d[a + 1], so only a in [lit - 1027, lit - 4] can."""
    lit, site = num(c["lit"]), num(c["site"])
    lds = [
        a
        for a in range(max(0, lit - 1027) & ~1, max(0, lit - 3), 2)
        if d[a] >> 4 == 0xD and (a & ~3) + 4 + d[a + 1] * 4 == lit
    ]
    return lds == [site], f"literal {_h(lit)} loaded at " + (
        ", ".join(_h(x) for x in lds) or "nowhere"
    )


def c_blank_blocks(d, c):
    size = num(c["block"])
    lost = [
        num(a)
        for a in c["expect"]
        if d[num(a) : num(a) + size] in (b"\xff" * size, b"\x00" * size)
    ]
    ok = len(lost) < num(c["min"])
    return (
        ok,
        f"{len(lost)} x {size} B blocks blank where the factory image has data"
        + (f": {', '.join(_h(x) for x in lost[:8])}" if lost else ""),
    )


def detect_gear(r, bands, x) -> int:
    """The gear (1..6) the ECU's detector reports for a measured ratio x, or 0."""
    for i in range(6):
        if r[i] * bands[i][0] < x < r[i] * bands[i][1]:
            return i + 1
    return 0


def gear_margins(r, bands, gears) -> dict:
    """{gear: (at_nominal, (e_up, gear_up), (e_down, gear_down))}: the gear
    reported at the nominal ratio, and the smallest relative error up / down
    (0.05 % steps, to 30 %) at which the real gear reads as another gear."""
    out = {}
    for g in gears:
        sides = []
        for sgn in (1, -1):
            hit = (None, None)
            for k in range(1, 601):
                e = k * 0.0005
                dg = detect_gear(r, bands, r[g - 1] * (1 + sgn * e))
                if dg != g:
                    hit = (e, dg)
                    break
            sides.append(hit)
        out[g] = (detect_gear(r, bands, r[g - 1]), sides[0], sides[1])
    return out


def c_gear(d, c) -> list:
    """[(level, message)] per the spec's Gear rule (empty = pass)."""
    r = _floats(d, num(c["ratios"]), 6)
    f = _floats(d, num(c["bands"]), 12)
    bands = [(f[2 * i], f[2 * i + 1]) for i in range(6)]
    grs = " ".join(f"{x:.3f}" for x in r)
    if not all(_fin(x) for x in r + f) or r[5] > r[4] or r[5] <= 0:
        return [
            (
                c.get("level", "refuse"),
                f"gear ratios {grs}: 6th is above 5th, 6th <= 0, or a ratio / "
                "band factor is not a number",
            )
        ]
    out = []
    five = r[4] / r[5] < c["five_speed_ratio"]
    # a factory 5MT calibration has 6th = 5th AND an empty 6th band (lo_6 >= hi_6):
    # gear 6 can never be reported, so the 5-speed WARN fires only while band 6 is
    # live (port census 2026-10-06); the checked gears still stop at 5th
    if five and bands[5][0] < bands[5][1]:
        out.append(
            (
                "warn",
                f"gear ratios {grs}: 6th within {(c['five_speed_ratio'] - 1) * 100:.0f} % "
                "of 5th (a 5-speed gearbox). In 5th the ECU can report gear 6, and a "
                "5th to 4th downshift then blips to about the current rpm (no rise)",
            )
        )
    thin = []
    gears = range(num(c["from_gear"]), 6 if five else 7)
    for g, (nom, up, dn) in gear_margins(r, bands, gears).items():
        if nom != g:
            thin.append(f"gear {g} reads as gear {nom} at its own ratio")
            continue
        for sgn, (e, dg) in (("+", up), ("-", dn)):
            if e is not None and e < c["margin_warn"]:
                thin.append(
                    f"gear {g} reads as gear {dg or 'none'} at {sgn}{e * 100:.1f} %"
                )
    if thin:
        out.append(
            (
                "warn",
                f"gear detection margins (ratios {grs} with the tune's band factors): "
                f"{'; '.join(thin)}. A tyre size that differs from the ECU's value "
                "shifts the measured ratio, and a misread gear makes the blip target "
                "the wrong gear",
            )
        )
    return out


CHECKS = {
    "bytes_eq": c_bytes_eq,
    "bytes_is": c_bytes_is,
    "fsub": c_fsub,
    "f_gt": c_f_gt,
    "f_le": c_f_le,
    "f_finite": c_f_finite,
    "f_desc": c_f_desc,
    "f_range": c_f_range,
    "f_close": c_f_close,
    "f_any_le": c_f_any_le,
    "table_finite": c_table_finite,
    "table_all_eq": c_table_all_eq,
    "setting_le_cal": c_setting_le_cal,
    "u8_eq": c_u8_eq,
    "no_word_in": c_no_word_in,
    "word_only_at": c_word_only_at,
    "pcrel_loader_only": c_pcrel_loader_only,
    "blank_blocks": c_blank_blocks,
    "gear": c_gear,
}

_CHECK_ERRORS = (
    struct.error,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
    ZeroDivisionError,
    OverflowError,
    StopIteration,
)


def run_check(d: bytes, c: dict) -> list:
    """[(level, message)] for a failed check (empty = pass); an error refuses."""
    try:
        if c["type"] == "gear":
            return c_gear(d, c)
        ok, det = CHECKS[c["type"]](d, c)
    except _CHECK_ERRORS as ex:
        return [
            (
                "refuse",
                f"{c.get('name')}: the check could not be evaluated "
                f"({type(ex).__name__} {ex})",
            )
        ]
    if ok:
        return []
    advice = f". {c['advice']}" if c.get("advice") else ""
    return [(c.get("level", "refuse"), f"{c['name']}: {det}{advice}")]


# ---------------------------------------------------------------- patch file


@dataclass(frozen=True)
class Range:
    addr: int
    kind: str
    old: bytes
    new: bytes

    @property
    def end(self) -> int:
        """Last byte (inclusive)."""
        return self.addr + len(self.old) - 1

    def label(self) -> str:
        return f"{self.kind} {_h(self.addr)}-{_h(self.end)}"


@dataclass
class Patch:
    """A parsed, validated v2 patch file."""

    data: dict
    sha256: str
    rom_id: str
    rom_id_addr: int
    ranges: list
    code_pin: Pin
    data_pin: Pin | None
    ck: ChecksumTable
    signature: str
    previous: list  # [(version, signature)]
    footprint: list  # [(lo, hi)] inclusive, ROM
    notes: dict = field(default_factory=dict)  # range start -> note shown on a REFUSE

    @property
    def id(self) -> str:
        return str(self.data["patch"].get("id"))

    @property
    def name(self) -> str:
        return str(self.data["patch"].get("name") or self.id)

    @property
    def version(self) -> str:
        return str(self.data["patch"].get("version"))

    @property
    def channel(self) -> str:
        return str(self.data["patch"].get("channel") or "release")

    @property
    def title(self) -> str:
        return f"{self.name} {self.version}"

    @property
    def romdrop_rev(self) -> str | None:
        rev = (self.data.get("requires") or {}).get("romdrop_rev")
        return str(rev) if rev else None

    @property
    def definitions(self) -> str | None:
        defs = self.data.get("definitions")
        return defs if isinstance(defs, str) else None

    @property
    def next_steps(self) -> list:
        return [str(s) for s in self.data.get("next_steps", [])]

    @property
    def consents(self) -> list:
        return list(self.data.get("consent", []))

    @property
    def features(self) -> list:
        """The pack's features: [{id, name, version, enable: {addr, type}, ...}]."""
        feats = self.data["patch"].get("features")
        return (
            [f for f in feats if isinstance(f, dict)] if isinstance(feats, list) else []
        )

    @property
    def summary(self) -> str:
        """'NC Flash features 1.0.0 (AutoBlip 3.4)' — the panel line."""
        feats = ", ".join(
            f"{f.get('name') or f.get('id')} {f.get('version', '')}".strip()
            for f in self.features
        )
        return f"{self.title} ({feats})" if feats else self.title

    def ranges_of(self, *kinds) -> list:
        return [r for r in self.ranges if r.kind in kinds]


@dataclass
class FeatureState:
    id: str
    name: str
    enabled: bool | None  # None: no enable flag, or unreadable
    missing: list  # feature ids it requires that are not enabled


def feature_states(d: bytes, patch: Patch) -> list:
    """Each feature's Enable as the ROM holds it, and unmet feature requirements."""
    states = []
    for f in patch.features:
        en = f.get("enable")
        enabled = None
        if isinstance(en, dict):
            try:
                enabled = _read(d, en["addr"], en["type"]) != 0
            except (KeyError, TypeError, ValueError, struct.error, PatchFormatError):
                enabled = None
        states.append(
            FeatureState(
                str(f.get("id")), str(f.get("name") or f.get("id")), enabled, []
            )
        )
    on = {s.id for s in states if s.enabled}
    for s, f in zip(states, patch.features):
        if s.enabled:
            s.missing = [str(r) for r in (f.get("requires") or []) if str(r) not in on]
    return states


_SIZE = {
    "float": 4,
    "uint8": 1,
    "int8": 1,
    "uint16": 2,
    "int16": 2,
    "uint32": 4,
    "int32": 4,
}


def _span(what: str, addr, n) -> int:
    """Address field addr must name n > 0 bytes inside the ROM: an unevaluable
    check REFUSES at parse time instead of passing or raising later."""
    a = num(addr)
    if (
        not isinstance(n, int)
        or isinstance(n, bool)
        or n <= 0
        or a < 0
        or a + n > ROM_SIZE
    ):
        raise PatchFormatError(
            f"{what}: address {addr!r} / length {n!r} is not a non-empty span "
            "inside the ROM"
        )
    return a


def _count(what: str, x, lo: int = 1) -> int:
    v = num(x)
    if v < lo:
        raise PatchFormatError(f"{what}: count {x!r} below {lo}")
    return v


def _number(what: str, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise PatchFormatError(f"{what}: {x!r} is not a finite number")
    return x


def _validate_check(c: dict):
    """Every address and length a check reads lies inside the ROM; every number
    field is a number (tools/ncflash_apply.py _validate_check)."""
    t, w = c["type"], f"check {c.get('name')!r}"
    if not isinstance(c.get("name"), str) or not c["name"]:
        raise PatchFormatError(f"{w}: name missing")
    if t == "bytes_eq":
        n = _count(w, c["len"])
        _span(w, c["a"], n)
        _span(w, c["b"], n)
    elif t == "bytes_is":
        _span(w, c["addr"], len(bytes.fromhex(c["hex"])))
    elif t == "fsub":
        for k in ("a", "b", "c"):
            _span(w, c[k], 4)
    elif t in ("f_gt", "f_le"):
        _span(w, c["addr"], 4)
        _number(w, c["x"])
    elif t == "f_finite":
        _span(w, c["addr"], 4)
    elif t == "f_desc":
        _span(w, c["addr"], 4 * _count(w, c["n"]))
    elif t == "f_any_le":
        _span(w, c["addr"], 4 * _count(w, c["n"]))
        _number(w, c["x"])
    elif t == "f_range":
        _span(w, c["addr"], 4 * _count(w, c.get("n", 1)))
        _number(w, c["min"])
        _number(w, c["max"])
        if not isinstance(c.get("min_exclusive", False), bool):
            raise PatchFormatError(f"{w}: min_exclusive must be true / false")
    elif t == "f_close":
        _span(w, c["a"], 4)
        if "b" in c:
            _span(w, c["b"], 4)
        else:
            _number(w, c["value"])
        _number(w, c["tol"])
    elif t in ("table_finite", "table_all_eq"):
        _span(w, c["addr"], 4 * _count(w, c["nx"]) * _count(w, c["ny"]))
        if t == "table_all_eq":
            _number(w, c["value"])
    elif t == "setting_le_cal":
        for k in ("setting", "cal"):
            if c[k]["type"] not in _SIZE:
                raise PatchFormatError(f"{w}: {k} type {c[k]['type']!r} unknown")
            _span(w, c[k]["addr"], _SIZE[c[k]["type"]])
        _number(w, c["margin"])
    elif t == "u8_eq":
        _span(w, c["addr"], 1)
        v = c["value"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
            raise PatchFormatError(f"{w}: value {v!r} is not a byte")
    elif t == "no_word_in":
        lo, hi = num(c["lo"]), num(c["hi"])
        if not 0 <= lo <= hi <= 0xFFFFFFFF:
            raise PatchFormatError(f"{w}: word range {c['lo']!r}-{c['hi']!r} invalid")
        for x in c.get("except_words", []):
            _span(w, x, 4)
        for a, b in c.get("except_ranges", []):
            if _span(w, a, 1) > _span(w, b, 1):
                raise PatchFormatError(
                    f"{w}: except range {a}-{b} ends before it starts"
                )
    elif t == "word_only_at":
        if not 0 <= num(c["value"]) <= 0xFFFFFFFF:
            raise PatchFormatError(f"{w}: value {c['value']!r} is not a 32-bit word")
        for x in c["addrs"]:
            if _span(w, x, 4) % 4:
                raise PatchFormatError(f"{w}: address {x} is not 4-aligned")
    elif t == "pcrel_loader_only":
        _span(w, c["lit"], 4)
        _span(w, c["site"], 2)
    elif t == "blank_blocks":
        size = _count(w, c["block"])
        for x in c["expect"]:
            _span(w, x, size)
        _count(w, c["min"])
    elif t == "gear":
        _span(w, c["ratios"], 24)
        _span(w, c["bands"], 48)
        _number(w, c["five_speed_ratio"])
        _number(w, c["margin_warn"])
        if not 1 <= _count(w, c["from_gear"]) <= 6:
            raise PatchFormatError(f"{w}: from_gear {c['from_gear']!r} outside 1-6")
    if "routine" in c and not isinstance(c["routine"], bool):
        raise PatchFormatError(f"{w}: routine must be true / false")


def _regions(lst) -> list:
    out = []
    for lo, hi in lst:
        lo, hi = num(lo), num(hi)
        if not 0 <= lo <= hi < ROM_SIZE:
            raise PatchFormatError(f"region {_h(lo)}-{_h(hi)} outside the ROM")
        out.append((lo, hi))
    return out


def _pin(p: dict) -> Pin:
    block_size = num(p["block_size"])
    if (
        block_size <= 0
        or not isinstance(p.get("sha256"), str)
        or len(p["sha256"]) != 64
    ):
        raise PatchFormatError("a pin needs a positive block_size and a sha256")
    level = p.get("level", "refuse")
    if level not in LEVELS:
        raise PatchFormatError(f"pin level {level!r} unknown")
    regions = _regions(p["regions"])
    if blocks_shared(regions, block_size):
        raise PatchFormatError("two regions of a pin share a block")
    return Pin(
        tuple(regions),
        tuple(_regions(p["exclude"])),
        p["sha256"],
        block_size,
        {num(k): v for k, v in p["blocks"].items()},
        level,
    )


def _parse(pt: dict, raw_sha: str) -> Patch:
    rom_id = pt["rom_id"]
    if not isinstance(rom_id, str) or not rom_id:
        raise PatchFormatError("rom_id missing")
    rom_id_addr = _span("rom_id_addr", pt["rom_id_addr"], len(rom_id))
    rngs, notes = [], {}
    for r in pt["ranges"]:
        a, old, new = num(r["addr"]), bytes.fromhex(r["old"]), bytes.fromhex(r["new"])
        if r["kind"] not in ALL_KINDS:
            raise PatchFormatError(f"range {_h(a)}: unknown kind {r['kind']!r}")
        if len(old) != len(new) or not old or not 0 <= a <= ROM_SIZE - len(old):
            raise PatchFormatError(
                f"range {_h(a)}: old / new lengths differ, are empty or run past the ROM"
            )
        if r["kind"] in ("romdrop", "romdrop_data") and old != new:
            raise PatchFormatError(
                f"range {_h(a)}: kind {r['kind']} must have old == new"
            )
        if "note" in r:
            if not isinstance(r["note"], str):
                raise PatchFormatError(f"range {_h(a)}: note must be text")
            notes[a] = r["note"]
        rngs.append(Range(a, r["kind"], old, new))
    rngs.sort(key=lambda x: x.addr)
    for r1, r2 in zip(rngs, rngs[1:]):
        if r2.addr <= r1.end:
            raise PatchFormatError(f"ranges overlap at {_h(r2.addr)}")
    if not any(r.kind == "code" for r in rngs):
        raise PatchFormatError("no code range")
    ck = pt["checksums"]
    if ck.get("algorithm") != CK_ALGORITHM:
        raise PatchFormatError(f"checksum algorithm {ck.get('algorithm')!r} unknown")
    table = ChecksumTable(num(ck["table"]), num(ck["records"]))
    if table.records <= 0:
        raise PatchFormatError("checksums: records must be positive")
    _span("checksums.table", ck["table"], 12 * (table.records + 1))
    for c in pt["checks"] + pt["post"]:
        if c["type"] not in CHECKS:
            raise PatchFormatError(
                f"check {c.get('name')!r}: unknown type {c['type']!r}"
            )
        if c.get("level", "refuse") not in LEVELS:
            raise PatchFormatError(
                f"check {c.get('name')!r}: unknown level {c.get('level')!r}"
            )
        if c.get("on", "input") not in ("input", "output"):
            raise PatchFormatError(
                f"check {c.get('name')!r}: unknown image {c.get('on')!r}"
            )
        _validate_check(c)
    listed = set()
    for c in pt.get("consent", []):
        if c.get("level", "warn") not in LEVELS or not isinstance(c.get("text"), str):
            raise PatchFormatError(f"consent {c.get('id')}: level / text missing")
        for a in c["ranges"]:
            if not any(x.addr == num(a) and x.kind == c["id"] for x in rngs):
                raise PatchFormatError(
                    f"consent {c['id']}: {a} is not a range of that kind"
                )
            listed.add(num(a))
    if any(x.kind == "tyre_restore" and x.addr not in listed for x in rngs):
        raise PatchFormatError("a tyre_restore range is not listed in a consent entry")
    req = pt.get("requires") or {}
    if req.get("patches") or pt.get("conflicts") or pt.get("upgrade_from"):
        raise PatchFormatError(
            "requires.patches / conflicts / upgrade_from are not supported by this "
            "applier version"
        )
    sig = pt["installed_signature"]
    if sig.get("kind") != "code" or len(sig.get("sha256", "")) != 64:
        raise PatchFormatError("installed_signature must be {kind: code, sha256}")
    footprint = []
    for lo, hi in (pt.get("footprint") or {}).get("rom", []):
        footprint.append((num(lo), num(hi)))
    if not footprint:
        footprint = [(r.addr, r.end) for r in rngs if r.kind in WRITE_KINDS]
    meta = pt.get("patch")
    if not isinstance(meta, dict):
        raise PatchFormatError("patch: missing")
    return Patch(
        data=pt,
        sha256=raw_sha,
        rom_id=rom_id,
        rom_id_addr=rom_id_addr,
        ranges=rngs,
        code_pin=_pin(pt["code_pin"]),
        data_pin=_pin(pt["data_pin"]) if pt.get("data_pin") else None,
        ck=table,
        signature=sig["sha256"],
        previous=[(str(p["version"]), p["signature"]) for p in pt.get("previous", [])],
        footprint=sorted(footprint),
        notes=notes,
    )


def load_patch(raw: bytes, pin: str) -> Patch:
    """Step 1: parse a v2 patch file whose sha256 must equal ``pin``.

    Raises :class:`PatchFormatError` on anything it can't use.
    """
    if not isinstance(pin, str) or len(pin) != 64:
        raise PatchFormatError("no sha256 pin given for the patch file")
    got = sha256_hex(raw)
    if got != pin.lower():
        raise PatchFormatError(
            f"patch file sha256 {got[:16]} is not the pinned {pin[:16]} (altered, "
            "truncated or another version)"
        )
    try:
        pt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise PatchFormatError(f"patch file is not JSON: {ex}")
    if not isinstance(pt, dict) or pt.get("format") not in FORMATS:
        fmt = pt.get("format") if isinstance(pt, dict) else None
        raise PatchFormatError(f"patch format {fmt!r} is not one of {FORMATS}")
    try:
        return _parse(pt, got)
    except PatchFormatError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as ex:
        raise PatchFormatError(f"patch file structure: {type(ex).__name__} {ex}")


# ---------------------------------------------------------------- detection


@dataclass
class PatchStatus:
    """What a ROM holds with respect to one patch (a "Patches" panel row)."""

    state: str  # installed | previous | available | unavailable
    detail: str = ""
    version: str | None = None


def held_signature(d: bytes, patch: Patch) -> str:
    """sha256 of the bytes the patch's code ranges hold, ascending address."""
    return sha256_hex(
        b"".join(d[r.addr : r.addr + len(r.old)] for r in patch.ranges_of("code"))
    )


def romdrop_match(d: bytes, patch: Patch) -> tuple:
    """(romdrop ranges matching, romdrop ranges): the Romdrop code fingerprint."""
    rs = patch.ranges_of("romdrop")
    return sum(1 for r in rs if d[r.addr : r.addr + len(r.old)] == r.old), len(rs)


def detect(d: bytes, patch: Patch) -> PatchStatus:
    """installed / previous / available / unavailable (+ why); read-only."""
    if len(d) != ROM_SIZE:
        return PatchStatus("unavailable", "not a 1 MB ECU ROM")
    ra = patch.rom_id_addr
    if d[ra : ra + len(patch.rom_id)] != patch.rom_id.encode():
        return PatchStatus("unavailable", f"for {patch.rom_id} only")
    held = held_signature(d, patch)
    if held == patch.signature:
        return PatchStatus("installed", f"{patch.title} installed", patch.version)
    for ver, sig in patch.previous:
        if held == sig:
            return PatchStatus("previous", f"{patch.name} {ver} installed", ver)
    hit, total = romdrop_match(d, patch)
    rev = patch.romdrop_rev or "?"
    if total and hit == 0:
        return PatchStatus(
            "unavailable",
            "Romdrop is not installed: install Romdrop first (Speeps' Romdrop "
            "tool), then reopen the file",
        )
    if hit < total:
        return PatchStatus(
            "unavailable",
            f"Romdrop differs from revision {rev} ({total - hit} of {total} code "
            "spots): another Romdrop version, a damaged file or another patch",
        )
    return PatchStatus("available", f"Romdrop {rev} found")


def consents_needed(d: bytes, patch: Patch) -> list:
    """The consent entries whose ranges this ROM would have written (step 4)."""
    if len(d) != ROM_SIZE:
        return []
    tyre = [
        r.addr
        for r in patch.ranges_of("tyre_restore")
        if d[r.addr : r.addr + len(r.old)] == r.old and r.old != r.new
    ]
    return [
        c
        for c in patch.consents
        if c["id"] == "tyre_restore" and any(num(x) in tyre for x in c["ranges"])
    ]


# ---------------------------------------------------------------- apply


@dataclass
class ApplyResult:
    report: Report
    output: bytes | None = None
    input_sha256: str | None = None

    @property
    def verdict(self) -> str:
        return self.report.verdict


def apply_patch(d: bytes, patch: Patch, consent=(), installed=()) -> ApplyResult:
    """Steps 2-9 on bytes in memory; nothing is written.

    ``consent``: the consent ids the user accepted. ``installed``: the bundled
    packs detected installed on this ROM; any other one refuses (a ROM holds one
    pack). The output is set only on an OK or WARN verdict.
    """
    rep = Report()
    res = ApplyResult(rep, input_sha256=sha256_hex(d))
    pt = patch.data
    pv = pt.get("patch", {})
    rep.add(
        INFO,
        f"patch {pv.get('name')} {pv.get('version')} ({pv.get('channel')}) for "
        f"{patch.rom_id}: {len(patch.ranges)} ranges, {len(pt['checks'])} checks, "
        f"reference result {str((pt.get('reference') or {}).get('result_sha256'))[:16]}",
    )
    if pv.get("channel") == "test":
        rep.add(INFO, "TEST patch (not a reviewed release)")
    # 2 input
    rep.add(INFO, f"input SHA-256 {res.input_sha256}")
    if len(d) != ROM_SIZE:
        rep.add(REFUSE, f"input is {len(d)} bytes, not a 1 MB ROM")
        return res
    rid, ra = patch.rom_id, patch.rom_id_addr
    if d[ra : ra + len(rid)] != rid.encode():
        rep.add(
            REFUSE,
            f"ROM ID: the patch is for {rid} (at {_h(ra)}); the input has "
            f"{d[ra:ra + len(rid)].decode('latin-1')!r} there",
        )
        return res
    # 3 ranges
    held = held_signature(d, patch)
    if held == patch.signature:
        rep.add(
            REFUSE,
            f"{pv.get('name')} {pv.get('version')} is already installed (its code "
            "signature matches)",
        )
        return res
    for ver, sig in patch.previous:
        if held == sig:
            rep.add(
                REFUSE,
                f"{pv.get('name')} {ver} is installed: upgrade from {ver} not "
                "supported, re-apply on a ROM without it",
            )
            return res
    write, tyre = [], []
    for r in patch.ranges:
        cur = d[r.addr : r.addr + len(r.old)]
        if cur == r.old:
            if r.old != r.new:
                write.append(r)
                if r.kind == "tyre_restore":
                    tyre.append(r.addr)
        elif cur == r.new:
            rep.add(INFO, f"{r.label()}: already the patch bytes")
        else:
            i = next(j for j in range(len(r.old)) if cur[j] != r.old[j])
            span = f"{_h(r.addr)}-{_h(r.end)}"
            if r.kind == "romdrop_data":
                rep.add(
                    WARN,
                    f"Romdrop data {_h(r.addr + i)} (range {span}) differs from a fresh "
                    "Romdrop file: a tune edit outside our definitions, or another "
                    "Romdrop version. Kept",
                )
            else:
                why = {
                    "romdrop": "Romdrop code differs from the required revision "
                    "(another Romdrop version, a damaged file or another patch)",
                    "code": "the patch area holds something else (another version "
                    "or another patch)",
                }.get(r.kind, "not the expected base bytes there")
                note = patch.notes.get(r.addr)
                rep.add(
                    REFUSE,
                    f"{r.kind} {span}: at {_h(r.addr + i)} expected "
                    f"{r.old[i:i + 16].hex()} found {cur[i:i + 16].hex()} ({why})"
                    + (f". {note}" if note else ""),
                )
    # NC Flash: a ROM holds one pack (release or test); switching is an upgrade
    for other in installed:
        if other.sha256 != patch.sha256:
            rep.add(
                REFUSE,
                f"this ROM already holds {other.summary} ({other.channel}): a ROM holds "
                "one NC Flash pack, and switching packs is an upgrade, not supported yet",
            )
    # 4 consent
    for c in patch.consents:
        need = (
            [a for a in tyre if any(num(x) == a for x in c["ranges"])]
            if c["id"] == "tyre_restore"
            else []
        )
        if not need:
            continue
        where = ", ".join(_h(a) for a in need)
        if c["id"] in consent:
            # the user accepted this explicitly: a routine WARN
            rep.add(
                ROW[c.get("level", "warn")],
                f"{c['id']} accepted ({where}): {c['text']}",
                routine=True,
            )
        else:
            rep.add(REFUSE, f"{c['id']} needs consent ({where}): {c['text']}")
    # 5 pins
    _report_pin(d, patch.code_pin, "code pin", rep, "refuse")
    if patch.data_pin:
        _report_pin(d, patch.data_pin, "data pin", rep, patch.data_pin.level)
    # 6 checksums
    ck = patch.ck
    try:
        st_in = ck_stale(d, ck)
    except ChecksumLayoutError as ex:
        rep.add(REFUSE, f"checksum table: {ex}")
        return res
    if st_in:
        rep.add(
            WARN,
            f"input checksum records stale {[x[0] for x in st_in]} (editors leave sums "
            "for the flasher): all recomputed in the output",
            routine=True,
        )
    # 7 checks
    virt = bytearray(d)
    for r in write:
        if r.kind == "tyre_restore":
            virt[r.addr : r.addr + len(r.new)] = r.new
    virt = ck_fix(bytes(virt), ck)
    outp = bytearray(virt)
    for r in write:
        outp[r.addr : r.addr + len(r.new)] = r.new
    outp = ck_fix(bytes(outp), ck)  # the image as it will be written
    for c in pt["checks"]:
        for lv, msg in run_check(virt if c.get("on", "input") == "input" else outp, c):
            rep.add(ROW[lv], msg, routine=c.get("routine", False))
    if rep.refused:
        return res
    # 8-9 output
    out = outp
    for c in pt["post"]:
        for _lv, msg in run_check(out, c):
            rep.add(REFUSE, f"output: {msg}")  # any post failure refuses
    if ck_stale(out, ck):
        rep.add(REFUSE, "output checksum records stale")
    if any(
        out[r.addr : r.addr + len(r.new)] != r.new
        for r in patch.ranges
        if r.kind != "romdrop_data"
    ):
        rep.add(REFUSE, "output: a patch range does not hold the patch bytes")
    covered = np.zeros(ROM_SIZE, dtype=bool)
    for r in patch.ranges:
        covered[r.addr : r.end + 1] = True
    for k in ck_sum_words(ck):
        covered[k] = True
    stray = [x for x in diff_addrs(out, d) if not covered[x]]
    if stray:
        rep.add(
            REFUSE,
            f"output differs from the input outside the ranges and sum words at "
            f"{_h(stray[0])}",
        )
    if rep.refused:
        return res
    rep.add(
        INFO,
        f"output: {len(write)} ranges written, {ck.records} checksum records valid, "
        f"SHA-256 {sha256_hex(out)}",
    )
    for s in patch.next_steps:
        rep.add(INFO, f"next: {s}")
    res.output = out
    return res


# ---------------------------------------------------------------- files


def write_new_file(path, data: bytes) -> None:
    """Write ``data`` to a NEW file (refused if it exists), then read it back.

    Written to a temp file in the same folder, fsynced, then linked into place;
    an existing file is never replaced.
    """
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} exists: choose a new file name")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"the folder of {path} does not exist")
    tmp = path.with_name(f".{path.name}.ncpatch.tmp")
    with open(tmp, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        if tmp.read_bytes() != data:
            raise OSError("the temporary output did not read back identical")
        try:
            os.link(tmp, path)  # fails if path exists: never overwrites
        except FileExistsError:
            raise
        except OSError:
            if sys.platform != "win32":
                raise
            os.rename(tmp, path)  # Windows rename refuses an existing target
    finally:
        if tmp.exists():
            tmp.unlink()
    if path.read_bytes() != data:
        raise OSError(f"{path} did not read back identical")


def apply_file(in_path, patch: Patch, consent=(), installed=()) -> ApplyResult:
    """Steps 2-9 on a file; the input is re-read and must be unchanged."""
    d = Path(in_path).read_bytes()
    res = apply_patch(d, patch, consent, installed)
    if res.output is not None and Path(in_path).read_bytes() != d:
        res.report.add(REFUSE, "the input file changed while it was being checked")
        res.output = None
    return res


# ---------------------------------------------------------------- catalogue


@dataclass
class Catalog:
    patches: list
    problems: list


def load_catalog(root, pins: dict) -> Catalog:
    """The bundled patches under ``root`` (``index.json``), every file pinned.

    ``pins`` maps a path relative to ``root`` (forward slashes) to its sha256;
    ``index.json`` itself is pinned too. An unpinned or altered file is left
    out and reported in ``problems``, never trusted.
    """
    root = Path(root)
    idx_path = root / "index.json"
    try:
        raw = idx_path.read_bytes()
    except OSError:
        return Catalog([], [f"{idx_path} missing"])
    if pins.get("index.json") != sha256_hex(raw):
        return Catalog([], ["patches/index.json is not the file this release pins"])
    try:
        entries = json.loads(raw.decode("utf-8"))["patches"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
        return Catalog([], [f"patches/index.json unreadable: {e}"])
    patches, problems = [], []
    for e in entries:
        rel = str(e.get("file", ""))
        pin = pins.get(rel)
        if not rel or pin is None or e.get("sha256") != pin:
            problems.append(f"{rel or '?'}: not pinned by this release")
            continue
        try:
            patches.append(load_patch((root / rel).read_bytes(), pin))
        except (OSError, PatchFormatError) as ex:
            problems.append(f"{rel}: {ex}")
    return Catalog(patches, problems)


def looks_like_romdrop_patch(data: bytes) -> bool:
    """A Speeps Romdrop XOR file: 1 MB, starts with a printable cal ID 'L…'."""
    return (
        len(data) == ROM_SIZE
        and data[:1] == b"L"
        and all(0x30 <= c < 0x7F for c in data[:6])
    )
