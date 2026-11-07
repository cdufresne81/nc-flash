"""
Transfer guard: never copy erased feature settings over real ones.

A ROM without the NC Flash feature release (AutoBlip, overboost cut, shift
light, cold rev limiter, speedometer correction) has its feature settings
areas erased (every byte 0xFF). When such a ROM is the source of a compare
copy onto a release image, a plain copy would write those 0xFF bytes over the
image's real settings: every Enable byte becomes 0xFF and every rpm or
pressure setting becomes NaN. The car stays safe (a feature is on only when
its Enable byte is exactly 1) but the settings become garbage.

Rule (nc-flash-re requirements R2): in a feature category, a table whose
source bytes are all 0xFF is never copied over a destination whose bytes are
not all 0xFF. An erased source is not a setting.
"""

from typing import Optional, Tuple

from .rom_definition import RomDefinition, Table, TableLayout

#: Categories of the feature release's settings tables (requirements R2).
FEATURE_CATEGORIES = frozenset(
    {
        "DBW - AutoBlip",
        "Engine Limiter - Boost Cut",
        "Instrument Cluster - Shift Light",
        "Engine Limiter - Cold Engine",
        "Instrument Cluster - Speedometer",
    }
)

_ERASED = 0xFF


def table_data_span(
    table: Table, definition: RomDefinition
) -> Optional[Tuple[int, int]]:
    """(start, length) of a table's data bytes, or None when unknown.

    Only contiguous tables have a span computable from the definition alone;
    interleaved (TCM-style) tables and tables without a known scaling return
    None.
    """
    if table.layout is not TableLayout.CONTIGUOUS or table.elements <= 0:
        return None
    scaling = definition.get_scaling(table.scaling)
    if scaling is None:
        return None
    try:
        start = table.address_int
    except ValueError:
        return None
    return start, table.elements * scaling.bytes_per_element


def _is_erased(rom_data, span: Tuple[int, int]) -> Optional[bool]:
    """True if every byte in span is 0xFF; None if the span is out of range."""
    start, length = span
    if start < 0 or length <= 0 or start + length > len(rom_data):
        return None
    return all(b == _ERASED for b in rom_data[start : start + length])


def is_feature_table(table: Optional[Table]) -> bool:
    """True if the table belongs to one of the feature release's categories."""
    return table is not None and table.category in FEATURE_CATEGORIES


def keeps_destination(
    src_table: Table,
    src_definition: RomDefinition,
    src_rom,
    dst_table: Table,
    dst_definition: RomDefinition,
    dst_rom,
) -> bool:
    """True when a copy must be refused because it would write an erased
    (all-0xFF) source over feature settings that the destination holds.

    Either side being in a feature category is enough: the destination's
    category says what would be overwritten, the source's what is copied.
    """
    if not (is_feature_table(src_table) or is_feature_table(dst_table)):
        return False
    src_span = table_data_span(src_table, src_definition)
    dst_span = table_data_span(dst_table, dst_definition)
    if src_span is None or dst_span is None:
        return False
    src_erased = _is_erased(src_rom, src_span)
    dst_erased = _is_erased(dst_rom, dst_span)
    return src_erased is True and dst_erased is False
