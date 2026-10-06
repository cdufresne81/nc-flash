"""Definition overlay: table definitions that travel with an installed patch.

A v2 patch file embeds the definitions of the tables it adds (``definitions``:
``<scaling>`` and ``<table>`` elements, as in a definition file). When NC Flash
detects the patch installed in a ROM, those tables are added to that ROM's
loaded definition, in memory only: the user's metadata files are never written.

- Overlay scalings are renamed ``<tag>:<name>``, so the scaling editor (which
  writes by name into the definition file) can never find or change them there.
- An overlay table whose name or address the base definition already has is
  skipped: the user's own definition wins.
"""

import logging
from dataclasses import replace
from pathlib import Path

from lxml import etree

from .definition_parser import DefinitionParser
from .exceptions import DefinitionParseError
from .rom_definition import RomDefinition, Table

logger = logging.getLogger(__name__)


def parse_fragment(xml: str, source: str) -> tuple[dict, list]:
    """(scalings by name, top-level tables) of a ``<scaling>``/``<table>`` fragment."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        rom = etree.fromstring(f"<rom>{xml}</rom>".encode("utf-8"), parser)
    except etree.XMLSyntaxError as e:
        raise DefinitionParseError(
            f"{source}: embedded definitions are not valid XML: {e}"
        )
    # Reuse the definition parser's element handling without a file.
    p = DefinitionParser.__new__(DefinitionParser)
    p.xml_path = Path(f"<{source}>")
    return p._parse_scalings(rom), p._parse_tables(rom)


def _addresses(table: Table) -> set:
    out = {table.address_int}
    for child in table.children:
        out.add(child.address_int)
    return out


def _rescale(table: Table, rename: dict) -> Table:
    return replace(
        table,
        scaling=rename.get(table.scaling, table.scaling),
        children=[_rescale(c, rename) for c in table.children],
    )


def overlay(
    base: RomDefinition, xml: str, tag: str
) -> tuple[RomDefinition, list, list]:
    """(new definition, added table names, skipped "name: why" lines).

    ``tag`` names the patch (e.g. ``autoblip 3.4``); it prefixes the overlay
    scalings and labels the log lines. The base definition is not modified.
    """
    scalings, tables = parse_fragment(xml, tag)
    rename = {name: f"{tag}:{name}" for name in scalings}
    names = {t.name.strip() for t in base.tables}
    taken = set()
    for t in base.tables:
        taken |= _addresses(t)
    added, skipped, new_tables = [], [], []
    for t in tables:
        if t.name.strip() in names:
            skipped.append(
                f"{t.name}: the definition file already has a table of that name"
            )
            continue
        clash = _addresses(t) & taken
        if clash:
            skipped.append(
                f"{t.name}: the definition file already has a table at "
                + ", ".join(f"0x{a:X}" for a in sorted(clash))
            )
            continue
        missing = [
            s
            for s in {t.scaling, *(c.scaling for c in t.children)}
            if s not in scalings
        ]
        if missing:
            skipped.append(f"{t.name}: scaling {', '.join(missing)} not embedded")
            continue
        new_tables.append(_rescale(t, rename))
        taken |= _addresses(t)
        names.add(t.name.strip())
        added.append(t.name)
    merged = RomDefinition(
        romid=base.romid,
        scalings={
            **base.scalings,
            **{rename[k]: replace(v, name=rename[k]) for k, v in scalings.items()},
        },
        tables=list(base.tables) + new_tables,
        xml_path=base.xml_path,
    )
    for line in skipped:
        logger.info(f"{tag} definitions: skipped {line}")
    return merged, added, skipped


def _extent(table: Table, definition: RomDefinition) -> tuple:
    scaling = definition.get_scaling(table.scaling)
    width = scaling.bytes_per_element if scaling else 1
    start = table.address_int
    return start, start + max(table.elements, 1) * width - 1


def stale_tables(definition: RomDefinition, spans, xml: str | None, tag: str) -> list:
    """Tables of ``definition`` inside the patch area that the patch doesn't define.

    ``spans``: the patch footprint, [(lo, hi)] inclusive. A table (or axis)
    whose bytes fall in it and that is not one of the patch's own tables (same
    name, address and scaling) is probably a dead or stale control, such as the
    retired v1 Auto-Blip block in old installs. Returns "name at 0x…" lines.
    """
    own = set()
    if xml:
        _, tables = parse_fragment(xml, tag)
        for t in tables:
            for x in [t, *t.children]:
                own.add((x.name.strip(), x.address_int, x.scaling))
    prefix = f"{tag}:"
    out = []
    for t in definition.tables:
        for x in [t, *t.children]:
            lo, hi = _extent(x, definition)
            if not any(lo <= b and a <= hi for a, b in spans):
                continue
            scaling = (
                x.scaling[len(prefix) :] if x.scaling.startswith(prefix) else x.scaling
            )
            if (x.name.strip(), x.address_int, scaling) in own:
                continue
            label = t.name if x is t else f"{t.name} ({x.name})"
            out.append(f"{label} at 0x{lo:X}")
    return out
