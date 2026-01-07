"""
Unit tests for ROM definition parser module
"""

import pytest
from pathlib import Path
from src.core.definition_parser import DefinitionParser, load_definition
from src.core.rom_definition import RomDefinition, RomID, Scaling, Table, TableType
from src.core.exceptions import (
    DefinitionNotFoundError,
    DefinitionParseError,
    InvalidDefinitionError
)


class TestDefinitionParserInitialization:
    """Test DefinitionParser initialization"""

    def test_init_with_valid_xml(self, sample_xml_path):
        """Test initialization with valid XML file"""
        parser = DefinitionParser(str(sample_xml_path))
        assert parser is not None
        assert parser.xml_path == sample_xml_path

    def test_init_with_nonexistent_file(self):
        """Test initialization with non-existent file raises error"""
        with pytest.raises(DefinitionNotFoundError):
            DefinitionParser("nonexistent.xml")


class TestRomDefinitionParsing:
    """Test parsing complete ROM definition"""

    def test_parse_returns_rom_definition(self, sample_xml_path):
        """Test that parse() returns a RomDefinition object"""
        parser = DefinitionParser(str(sample_xml_path))
        definition = parser.parse()

        assert isinstance(definition, RomDefinition)
        assert definition.romid is not None
        assert len(definition.scalings) > 0
        assert len(definition.tables) > 0

    def test_load_definition_function(self, sample_xml_path):
        """Test the convenience load_definition() function"""
        definition = load_definition(str(sample_xml_path))

        assert isinstance(definition, RomDefinition)
        assert definition.romid.xmlid == "LF9VEB"


class TestRomIdParsing:
    """Test ROM ID section parsing"""

    def test_parse_romid_fields(self, sample_xml_path):
        """Test that all ROM ID fields are parsed correctly"""
        definition = load_definition(str(sample_xml_path))
        romid = definition.romid

        assert isinstance(romid, RomID)
        assert romid.xmlid == "LF9VEB"
        assert romid.internalidstring == "LF9VEB"
        assert romid.internalidaddress == "b8046"
        assert romid.ecuid == "LF9VEB"
        assert romid.make == "Mazda"
        assert romid.model == "MX5"
        assert romid.flashmethod == "Romdrop"
        assert romid.memmodel == "SH7058"
        assert romid.checksummodule == "21053000"

    def test_romid_address_conversion(self, sample_xml_path):
        """Test that ROM ID address is converted to int correctly"""
        definition = load_definition(str(sample_xml_path))

        # b8046 hex = 753734 decimal
        assert definition.romid.internal_id_address_int == 0xb8046
        assert definition.romid.internal_id_address_int == 753734


class TestScalingParsing:
    """Test scaling definition parsing"""

    def test_parse_scalings_count(self, sample_xml_path):
        """Test that scalings are parsed"""
        definition = load_definition(str(sample_xml_path))

        assert len(definition.scalings) > 100  # Should have many scalings

    def test_scaling_attributes(self, sample_xml_path):
        """Test that scaling attributes are parsed correctly"""
        definition = load_definition(str(sample_xml_path))

        # Get a known scaling (should exist in lf9veb.xml)
        scalings = list(definition.scalings.values())
        assert len(scalings) > 0

        scaling = scalings[0]
        assert isinstance(scaling, Scaling)
        assert scaling.name is not None
        assert scaling.toexpr is not None
        assert scaling.frexpr is not None
        assert scaling.format is not None
        assert scaling.storagetype is not None
        assert scaling.endian is not None

    def test_scaling_numeric_fields(self, sample_xml_path):
        """Test that numeric scaling fields are parsed as floats"""
        definition = load_definition(str(sample_xml_path))
        scaling = list(definition.scalings.values())[0]

        assert isinstance(scaling.min, float)
        assert isinstance(scaling.max, float)
        assert isinstance(scaling.inc, float)

    def test_scaling_bytes_per_element(self, sample_xml_path):
        """Test bytes_per_element property"""
        definition = load_definition(str(sample_xml_path))

        # Find a float scaling
        float_scaling = None
        for scaling in definition.scalings.values():
            if scaling.storagetype.lower() == 'float':
                float_scaling = scaling
                break

        assert float_scaling is not None
        assert float_scaling.bytes_per_element == 4


class TestTableParsing:
    """Test table definition parsing"""

    def test_parse_tables_count(self, sample_xml_path):
        """Test that tables are parsed"""
        definition = load_definition(str(sample_xml_path))

        # Should have 511 tables for lf9veb
        assert len(definition.tables) > 500

    def test_table_basic_attributes(self, sample_xml_path):
        """Test basic table attributes"""
        definition = load_definition(str(sample_xml_path))

        # Get first non-axis table
        table = None
        for t in definition.tables:
            if not t.is_axis:
                table = t
                break

        assert table is not None
        assert table.name is not None
        assert table.address is not None
        assert table.elements > 0
        assert table.scaling is not None
        assert table.type in [TableType.ONE_D, TableType.TWO_D, TableType.THREE_D]

    def test_table_address_conversion(self, sample_xml_path):
        """Test that table addresses are converted correctly"""
        definition = load_definition(str(sample_xml_path))
        table = definition.tables[0]

        # Should convert hex string to integer
        assert isinstance(table.address_int, int)
        assert table.address_int > 0

    def test_table_types_distribution(self, sample_xml_path):
        """Test that various table types exist"""
        definition = load_definition(str(sample_xml_path))

        # Count table types (excluding axis tables)
        types = {
            TableType.ONE_D: 0,
            TableType.TWO_D: 0,
            TableType.THREE_D: 0
        }

        for table in definition.tables:
            if not table.is_axis:
                types[table.type] += 1

        # Should have all three types
        assert types[TableType.ONE_D] > 0
        assert types[TableType.TWO_D] > 0
        assert types[TableType.THREE_D] > 0

    def test_table_with_axes(self, sample_xml_path):
        """Test that 3D tables have axes"""
        definition = load_definition(str(sample_xml_path))

        # Find a 3D table
        table_3d = None
        for table in definition.tables:
            if table.type == TableType.THREE_D and not table.is_axis:
                table_3d = table
                break

        assert table_3d is not None
        assert table_3d.x_axis is not None
        assert table_3d.y_axis is not None
        assert len(table_3d.children) == 2


class TestRomDefinitionMethods:
    """Test RomDefinition helper methods"""

    def test_get_scaling(self, sample_xml_path):
        """Test getting scaling by name"""
        definition = load_definition(str(sample_xml_path))

        # Get first scaling name
        scaling_name = list(definition.scalings.keys())[0]
        scaling = definition.get_scaling(scaling_name)

        assert scaling is not None
        assert scaling.name == scaling_name

    def test_get_scaling_returns_none_for_unknown(self, sample_xml_path):
        """Test that unknown scaling returns None"""
        definition = load_definition(str(sample_xml_path))
        scaling = definition.get_scaling("UNKNOWN_SCALING")

        assert scaling is None

    def test_get_tables_by_category(self, sample_xml_path):
        """Test grouping tables by category"""
        definition = load_definition(str(sample_xml_path))
        categories = definition.get_tables_by_category()

        assert len(categories) > 0
        assert all(isinstance(tables, list) for tables in categories.values())

        # Axis tables should not be included
        for tables in categories.values():
            assert all(not t.is_axis for t in tables)

    def test_get_table_by_name(self, sample_xml_path):
        """Test finding table by name"""
        definition = load_definition(str(sample_xml_path))

        # Get a table name
        table_name = definition.tables[0].name
        found_table = definition.get_table_by_name(table_name)

        assert found_table is not None
        assert found_table.name == table_name

    def test_get_table_by_name_returns_none_for_unknown(self, sample_xml_path):
        """Test that unknown table name returns None"""
        definition = load_definition(str(sample_xml_path))
        table = definition.get_table_by_name("UNKNOWN_TABLE")

        assert table is None


class TestErrorHandling:
    """Test error handling in parser"""

    def test_parse_invalid_xml_raises_error(self, tmp_path):
        """Test that invalid XML raises an error"""
        bad_xml = tmp_path / "bad.xml"
        bad_xml.write_text("This is not valid XML!")

        parser = DefinitionParser(str(bad_xml))

        with pytest.raises(DefinitionParseError):
            parser.parse()

    def test_parse_xml_without_rom_element(self, tmp_path):
        """Test that XML without <rom> element raises error"""
        no_rom_xml = tmp_path / "no_rom.xml"
        no_rom_xml.write_text('<?xml version="1.0"?><roms></roms>')

        parser = DefinitionParser(str(no_rom_xml))

        with pytest.raises(InvalidDefinitionError, match="No <rom> element found"):
            parser.parse()

    def test_parse_xml_without_romid(self, tmp_path):
        """Test that XML without <romid> element raises error"""
        no_romid_xml = tmp_path / "no_romid.xml"
        no_romid_xml.write_text('<?xml version="1.0"?><roms><rom></rom></roms>')

        parser = DefinitionParser(str(no_romid_xml))

        with pytest.raises(InvalidDefinitionError, match="No <romid> element found"):
            parser.parse()
