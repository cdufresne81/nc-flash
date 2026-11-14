"""
The TCM definitions must describe the records the TCM code really reads.

The TCM calibration stores every lookup table as a self-describing record
(see docs/internal/TCM_TABLE_LAYOUTS.md):

* 1-D byte record:  [count u8][axis: count x u8][data: count x u8]
* 1-D word record:  [count u16][u16][axis: count x u16][data: count x u16]
* 2-D byte map:     the interleaved layout, checked by RomReader itself

A definition that declares the wrong cell size or count makes a table reach
into its neighbours, so saving an edit would overwrite them (#123). These
tests check each table against the count stored in the real dump.
"""

import pytest

from src.core.definition_parser import load_definition
from src.core.rom_reader import RomReader
from src.core.storage_types import STORAGE_TYPE_BYTES

TCM_DEFINITION = "LFG1TF000_v02.xml"
TCM_TWIN = "LFG1TG000_v02.xml"


@pytest.fixture
def tcm(definitions_dir, examples_dir):
    definition = load_definition(str(definitions_dir / TCM_DEFINITION))
    reader = RomReader(str(examples_dir / "LFG1TF000.bin"), definition)
    return definition, reader


def _size(definition, table):
    scaling = definition.get_scaling(table.scaling)
    return STORAGE_TYPE_BYTES[scaling.storagetype]


def _one_axis_tables(definition):
    return [t for t in definition.tables if len(t.children) == 1]


def test_every_table_reads(tcm):
    definition, reader = tcm
    for table in definition.tables:
        assert reader.read_table_data(table) is not None, table.name


def test_byte_records_match_their_count_byte(tcm):
    definition, reader = tcm
    rom = reader.rom_data
    checked = 0
    for table in _one_axis_tables(definition):
        axis = table.children[0]
        if _size(definition, axis) != 1:
            continue
        checked += 1
        assert rom[axis.address_int - 1] == axis.elements, table.name
        assert table.elements == axis.elements, table.name
        assert table.address_int == axis.address_int + axis.elements, table.name
        assert _size(definition, table) == 1, (
            f"'{table.name}': the byte lookup reads 1-byte cells, "
            f"but the definition declares {_size(definition, table)}-byte cells"
        )
    assert checked > 300


def test_word_records_match_their_count_word(tcm):
    definition, reader = tcm
    rom = reader.rom_data
    checked = 0
    for table in _one_axis_tables(definition):
        axis = table.children[0]
        if _size(definition, axis) != 2:
            continue
        checked += 1
        header = axis.address_int - 4
        count = int.from_bytes(rom[header : header + 2], "big")
        assert count == axis.elements, table.name
        assert table.elements == axis.elements, table.name
        assert table.address_int == axis.address_int + 2 * axis.elements, table.name
        assert _size(definition, table) == 2, table.name
    assert checked > 100


def test_both_tcm_definitions_stay_identical(definitions_dir):
    """LFG1TG000 has no dump to check against: it must mirror LFG1TF000."""
    first = (definitions_dir / TCM_DEFINITION).read_text(encoding="utf-8")
    twin = (definitions_dir / TCM_TWIN).read_text(encoding="utf-8")
    assert first.replace("LFG1TF000", "X") == twin.replace("LFG1TG000", "X")
