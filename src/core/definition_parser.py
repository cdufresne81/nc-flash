"""
ROM Definition XML Parser

Parses RomDrop-style XML definition files into RomDefinition objects.
"""

from lxml import etree
from typing import Optional
from pathlib import Path
import logging

from .rom_definition import RomDefinition, RomID, Scaling, Table, TableType, AxisType, TableLayout
from .exceptions import (
    DefinitionNotFoundError,
    DefinitionParseError,
    InvalidDefinitionError,
)

logger = logging.getLogger(__name__)


class DefinitionParser:
    """
    Parser for ROM definition XML files
    """

    def __init__(self, xml_path: str):
        """
        Initialize parser with path to XML definition file

        Args:
            xml_path: Path to ROM definition XML file

        Raises:
            DefinitionNotFoundError: If definition file doesn't exist
        """
        self.xml_path = Path(xml_path)
        if not self.xml_path.exists():
            logger.error(f"Definition file not found: {xml_path}")
            raise DefinitionNotFoundError(f"Definition file not found: {xml_path}")

    def parse(self) -> RomDefinition:
        """
        Parse the XML file and return a RomDefinition object

        Returns:
            RomDefinition: Complete ROM definition

        Raises:
            DefinitionParseError: If XML parsing fails
            InvalidDefinitionError: If ROM definition structure is invalid
        """
        logger.info(f"Parsing ROM definition from {self.xml_path}")

        try:
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            tree = etree.parse(str(self.xml_path), parser)
            root = tree.getroot()
        except etree.XMLSyntaxError as e:
            logger.error(f"XML syntax error in {self.xml_path}: {e}")
            raise DefinitionParseError(f"Failed to parse XML file: {e}")
        except Exception as e:
            logger.error(f"Unexpected error parsing {self.xml_path}: {e}")
            raise DefinitionParseError(f"Failed to parse definition file: {e}")

        # Parse ROM element
        # Handle two formats:
        # 1. <roms><rom>...</rom></roms> (our format with wrapper)
        # 2. <rom>...</rom> (standard RomDrop format)
        if root.tag == "rom":
            # Root is the rom element (RomDrop format)
            rom_element = root
        else:
            # Look for rom element inside wrapper (our format)
            rom_element = root.find(".//rom")
            if rom_element is None:
                logger.error(f"No <rom> element found in {self.xml_path}")
                raise InvalidDefinitionError("No <rom> element found in XML")

        # Parse ROM ID
        romid = self._parse_romid(rom_element)

        # Parse all scaling definitions
        scalings = self._parse_scalings(rom_element)
        logger.info(f"Parsed {len(scalings)} scaling definitions")

        # Parse all table definitions
        tables = self._parse_tables(rom_element)
        logger.info(f"Parsed {len(tables)} table definitions")

        logger.info(f"Successfully parsed ROM definition: {romid.xmlid}")
        return RomDefinition(
            romid=romid, scalings=scalings, tables=tables, xml_path=str(self.xml_path)
        )

    def _parse_romid(self, rom_element) -> RomID:
        """
        Parse ROM identification section

        Args:
            rom_element: The <rom> XML element

        Returns:
            RomID: Parsed ROM identification

        Raises:
            InvalidDefinitionError: If <romid> element is missing
        """
        romid_elem = rom_element.find("romid")
        if romid_elem is None:
            logger.error("No <romid> element found in ROM definition")
            raise InvalidDefinitionError("No <romid> element found")

        def get_text(tag: str, default: str = "") -> str:
            elem = romid_elem.find(tag)
            return elem.text.strip() if elem is not None and elem.text else default

        return RomID(
            xmlid=get_text("xmlid"),
            internalidaddress=get_text("internalidaddress"),
            internalidstring=get_text("internalidstring"),
            ecuid=get_text("ecuid"),
            make=get_text("make"),
            model=get_text("model"),
            flashmethod=get_text("flashmethod"),
            memmodel=get_text("memmodel"),
            checksummodule=get_text("checksummodule"),
            market=get_text("market") or None,
            submodel=get_text("submodel") or None,
            transmission=get_text("transmission") or None,
            year=get_text("year") or None,
        )

    def _parse_scalings(self, rom_element) -> dict:
        """
        Parse all scaling definitions into a dictionary

        Args:
            rom_element: The <rom> XML element

        Returns:
            dict: Dictionary mapping scaling names to Scaling objects
        """
        scalings = {}

        for scaling_elem in rom_element.findall(".//scaling"):
            name = scaling_elem.get("name")
            if not name:
                logger.debug("Skipping scaling element without name attribute")
                continue  # Skip scalings without names

            try:
                scaling = Scaling(
                    name=name,
                    units=scaling_elem.get("units", ""),
                    toexpr=scaling_elem.get("toexpr", "x"),
                    frexpr=scaling_elem.get("frexpr", "x"),
                    format=scaling_elem.get("format", "%0.2f"),
                    min=float(scaling_elem.get("min", "0")),
                    max=float(scaling_elem.get("max", "0")),
                    inc=float(scaling_elem.get("inc", "1")),
                    storagetype=scaling_elem.get("storagetype", "float"),
                    endian=scaling_elem.get("endian", "big"),
                )

                scalings[name] = scaling
                logger.debug(f"Parsed scaling: {name}")
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse scaling '{name}': {e}")
                continue

        return scalings

    def _parse_tables(self, rom_element) -> list:
        """
        Parse all table definitions

        Args:
            rom_element: The <rom> XML element

        Returns:
            list: List of top-level Table objects (not axis children)
        """
        tables = []

        # Find all top-level table elements (direct children of rom, not nested in other tables)
        # We need to handle the hierarchy: some tables contain child axis tables
        for table_elem in rom_element.findall("./table"):
            # Only parse if it has required attributes (top-level tables)
            if table_elem.get("address") and table_elem.get("type"):
                table = self._parse_table(table_elem)
                if table:
                    tables.append(table)
                    logger.debug(f"Parsed table: {table.name}")

        return tables

    def _parse_table(self, table_elem) -> Optional[Table]:
        """
        Parse a single table element and its children

        Args:
            table_elem: The <table> XML element

        Returns:
            Table object if successful, None if table is invalid
        """
        # Get table type
        type_str = table_elem.get("type")
        if not type_str:
            logger.debug("Skipping table element without type attribute")
            return None

        # Determine if this is an axis table
        axis_type = None
        if type_str in ["X Axis", "Y Axis"]:
            axis_type = AxisType.X_AXIS if type_str == "X Axis" else AxisType.Y_AXIS
            table_type = TableType.ONE_D  # Axes are always 1D
        else:
            try:
                table_type = TableType(type_str)
            except ValueError:
                logger.debug(f"Skipping table with unknown type: {type_str}")
                return None  # Unknown type

        # Parse layout attribute
        layout_str = table_elem.get("layout", "contiguous")
        try:
            layout = TableLayout(layout_str)
        except ValueError:
            layout = TableLayout.CONTIGUOUS

        # Parse basic attributes
        table = Table(
            name=table_elem.get("name", "Unnamed"),
            address=table_elem.get("address", "0"),
            elements=int(table_elem.get("elements", "0")),
            scaling=table_elem.get("scaling", ""),
            type=table_type,
            level=int(table_elem.get("level", "1")),
            category=table_elem.get("category", ""),
            swapxy=table_elem.get("swapxy", "false").lower() == "true",
            flipx=table_elem.get("flipx", "false").lower() == "true",
            flipy=table_elem.get("flipy", "false").lower() == "true",
            layout=layout,
            axis_type=axis_type,
        )

        # Parse child tables (axes for 2D/3D tables)
        for child_elem in table_elem.findall("./table"):
            child_table = self._parse_table(child_elem)
            if child_table:
                table.children.append(child_table)

        return table


def load_definition(xml_path: str) -> RomDefinition:
    """
    Convenience function to load a ROM definition from XML file

    Args:
        xml_path: Path to XML definition file

    Returns:
        RomDefinition: Parsed ROM definition
    """
    parser = DefinitionParser(xml_path)
    return parser.parse()
