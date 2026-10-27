"""
Table identity helpers: duplicate names, "Name @ 0xADDR" labels, matching.

Every NC definition gives some tables the same name at different addresses.
A table's name is therefore NOT a unique key; its address is. Anything that
looks tables up by name, or pairs tables across two ROMs, goes through here so
no copy is silently shadowed by another one with the same name.
"""

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Tuple, Union

if TYPE_CHECKING:  # pragma: no cover
    from .rom_definition import RomDefinition, Table

# "Some Table @ 0xC8FFC". The 0x prefix is required so names that already
# contain " @ " (e.g. "... | RPM @ 900") are never misread as qualified.
_QUALIFIED_RE = re.compile(r"^(?P<name>.*\S)\s+@\s+(?P<addr>0[xX][0-9A-Fa-f]+)\s*$")


def parse_address(value: Union[int, str]) -> int:
    """Parse a table address given as an int or a hex string.

    Accepts "0xC8FFC", "C8FFC" and "c8ffc" (definitions store addresses as
    bare hex). Raises ValueError on anything else.
    """
    if isinstance(value, bool):
        raise ValueError(f"Invalid table address: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"Invalid table address: {value!r}")
        return value
    text = str(value).strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text or not re.fullmatch(r"[0-9A-Fa-f]+", text):
        raise ValueError(
            f"Invalid table address: {value!r} (expected hex like 0xC8FFC)"
        )
    return int(text, 16)


def format_address(table: "Table") -> str:
    """Address of a table as "0xC8FFC"."""
    try:
        return f"0x{table.address_int:X}"
    except (TypeError, ValueError):
        return f"0x{table.address}"


def qualified_name(table: "Table") -> str:
    """The "Name @ 0xADDR" form that addresses one specific table copy."""
    return f"{table.name.strip()} @ {format_address(table)}"


def split_qualified_name(name: str) -> Tuple[str, Optional[int]]:
    """Split "Name @ 0xADDR" into (name, address); plain names give (name, None)."""
    m = _QUALIFIED_RE.match(name or "")
    if not m:
        return name, None
    return m.group("name"), int(m.group("addr"), 16)


def _address_matches(table: Optional["Table"], address: int) -> bool:
    if table is None:
        return False
    try:
        return table.address_int == address
    except (TypeError, ValueError):
        return False


def _blank_to_none(address):
    """An empty or whitespace-only address from a request means "none given"."""
    if isinstance(address, str) and not address.strip():
        return None
    return address


def lookup_table(
    definition: "RomDefinition",
    name: str,
    address: Optional[Union[int, str]] = None,
) -> Tuple[Optional["Table"], Optional[str]]:
    """Resolve a table for an API/MCP request.

    Returns (table, None) on success, or (None, error_message) when the name
    is unknown, ambiguous, or the address is invalid. Never guesses between
    copies that share a name.
    """
    from .exceptions import AmbiguousTableNameError

    address = _blank_to_none(address)
    try:
        table = definition.get_table_by_name(name, address=address)
    except (AmbiguousTableNameError, ValueError) as e:
        return None, str(e)
    if table is None:
        if address is not None:
            return None, f"Table not found: {name} at address {address}"
        return None, f"Table not found: {name}"
    return table, None


# ---------------------------------------------------------------------------
# Cross-ROM matching (compare window, MCP compare)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableMatch:
    """One row of a two-ROM comparison: the same table on each side.

    ``label`` is unique within one match list: the plain name when the name
    is unique, else "Name @ 0xADDR" (or "Name @ 0xA / 0xB" when a cross-
    definition pair sits at different addresses).

    ``uncertain`` is True when the two definitions list a different number
    of tables under this name: the pairing is then only a guess, so the pair
    is still compared and shown but must never be used to copy data.
    """

    name: str
    label: str
    category: str
    table_a: Optional["Table"]
    table_b: Optional["Table"]
    uncertain: bool = False

    @property
    def copy_safe(self) -> bool:
        """True when both sides exist and are known to be the same table."""
        return (
            self.table_a is not None and self.table_b is not None and not self.uncertain
        )


def _group_by_name(tables: Iterable["Table"]) -> Dict[str, List["Table"]]:
    groups: Dict[str, List["Table"]] = {}
    for table in tables:
        groups.setdefault(table.name, []).append(table)
    return groups


def _pair_label(name: str, ta: Optional["Table"], tb: Optional["Table"]) -> str:
    addr_a = format_address(ta) if ta is not None else None
    addr_b = format_address(tb) if tb is not None else None
    if addr_a and addr_b and addr_a != addr_b:
        return f"{name} @ {addr_a} / {addr_b}"
    return f"{name} @ {addr_a or addr_b}"


def match_tables(
    tables_a: Iterable["Table"], tables_b: Iterable["Table"]
) -> List[TableMatch]:
    """Pair the tables of two definitions, one TableMatch per table copy.

    Tables are matched by name. When several tables share a name, copies at
    the same address on both sides are paired first; the remaining copies
    pair by their order in each definition (1st with 1st, ...). A side with
    more copies yields one-sided matches for the extras. If the two sides
    list a different number of copies, every match for that name is marked
    ``uncertain`` (still compared, never copied). No table is ever dropped.
    """
    groups_a = _group_by_name(tables_a)
    groups_b = _group_by_name(tables_b)
    names = list(groups_a)
    names.extend(n for n in groups_b if n not in groups_a)

    matches: List[TableMatch] = []
    for name in names:
        copies_a = groups_a.get(name, [])
        copies_b = groups_b.get(name, [])
        duplicated = len(copies_a) > 1 or len(copies_b) > 1
        uncertain = duplicated and len(copies_a) != len(copies_b)
        for ta, tb in _pair_copies(copies_a, copies_b):
            label = _pair_label(name, ta, tb) if duplicated else name
            category = (ta or tb).category or ""
            matches.append(TableMatch(name, label, category, ta, tb, uncertain))

    # Labels must stay unique (they key UI rows and API answers). Two copies
    # at the same address under one name would collide; number them.
    seen: Dict[str, int] = {}
    for i, m in enumerate(matches):
        count = seen.get(m.label, 0) + 1
        seen[m.label] = count
        if count > 1:
            matches[i] = replace(m, label=f"{m.label} #{count}")
    return matches


def _pair_copies(
    copies_a: List["Table"], copies_b: List["Table"]
) -> List[Tuple[Optional["Table"], Optional["Table"]]]:
    """Pair same-named copies: same address first, then by order.

    Output follows A's order (each A copy with its partner or None), then
    any B copies left over.
    """
    partner: Dict[int, "Table"] = {}  # index in copies_a -> B copy
    used_b = set()
    for i, ta in enumerate(copies_a):
        for j, tb in enumerate(copies_b):
            if j not in used_b and _same_address(ta, tb):
                partner[i] = tb
                used_b.add(j)
                break
    rest_a = [i for i in range(len(copies_a)) if i not in partner]
    rest_b = [j for j in range(len(copies_b)) if j not in used_b]
    for i, j in zip(rest_a, rest_b):
        partner[i] = copies_b[j]
        used_b.add(j)

    pairs = [(ta, partner.get(i)) for i, ta in enumerate(copies_a)]
    pairs.extend((None, tb) for j, tb in enumerate(copies_b) if j not in used_b)
    return pairs


def _same_address(ta: "Table", tb: "Table") -> bool:
    try:
        return ta.address_int == tb.address_int
    except (TypeError, ValueError):
        return False


def find_match(
    matches: List[TableMatch],
    name: str,
    address: Optional[Union[int, str]] = None,
) -> Tuple[Optional[TableMatch], Optional[str]]:
    """Pick one TableMatch by label, name, or name + address.

    Returns (match, None) or (None, error_message). An ambiguous name is
    refused with the list of copies rather than resolved to one of them.
    """
    address = _blank_to_none(address)
    if address is None:
        by_label = [m for m in matches if m.label == name]
        if len(by_label) == 1:
            return by_label[0], None

    base, suffix_addr = name, None
    candidates = [m for m in matches if m.name == name]
    if not candidates:
        candidates = [m for m in matches if m.name.strip() == name.strip()]
    if not candidates:
        base, suffix_addr = split_qualified_name(name)
        if suffix_addr is not None:
            candidates = [m for m in matches if m.name.strip() == base.strip()]

    wanted: Optional[int] = None
    try:
        if address is not None:
            wanted = parse_address(address)
    except ValueError as e:
        return None, str(e)
    if suffix_addr is not None:
        if wanted is not None and wanted != suffix_addr:
            return None, (
                f"Address {address} does not match the address in "
                f"'{name}'; give only one of them"
            )
        wanted = suffix_addr

    if wanted is not None:
        candidates = [
            m
            for m in candidates
            if _address_matches(m.table_a, wanted)
            or _address_matches(m.table_b, wanted)
        ]

    if not candidates:
        where = f" at address 0x{wanted:X}" if wanted is not None else ""
        return None, f"Table not found in either ROM: {base}{where}"
    if len(candidates) > 1:
        labels = ", ".join(f"'{m.label}'" for m in candidates)
        return None, (
            f"Table name '{base}' is ambiguous: {len(candidates)} tables share "
            f"it ({labels}). Pass an address, or use one of those labels."
        )
    return candidates[0], None
