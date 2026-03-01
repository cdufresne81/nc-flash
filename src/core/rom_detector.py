"""
ROM ID Detection and XML Matching

Automatically detects ROM ID from binary files and finds matching XML definitions.
"""

import logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from lxml import etree

from .exceptions import (
    MetadataDirectoryError,
    DefinitionParseError,
    RomFileNotFoundError,
    AddressConversionError,
)

logger = logging.getLogger(__name__)


@dataclass
class RomIdInfo:
    """ROM ID information extracted from XML definition"""

    xml_path: Path
    xmlid: str
    internalidaddress: str
    internalidstring: str
    make: str
    model: str

    @property
    def internal_id_address_int(self) -> int:
        """Convert hex address string to integer"""
        try:
            return int(self.internalidaddress, 16)
        except ValueError as e:
            raise AddressConversionError(
                f"Invalid hex address '{self.internalidaddress}': {e}"
            ) from e


class RomDetector:
    """
    Detects ROM ID from binary files and matches to XML definitions
    """

    def __init__(self, definitions_dir: str = "definitions"):
        """
        Initialize ROM detector

        Args:
            definitions_dir: Directory containing XML definition files

        Raises:
            MetadataDirectoryError: If definitions directory doesn't exist or is invalid
        """
        self.definitions_dir = Path(definitions_dir)

        if not self.definitions_dir.exists():
            raise MetadataDirectoryError(
                f"Definitions directory not found: {definitions_dir}"
            )

        if not self.definitions_dir.is_dir():
            raise MetadataDirectoryError(f"Path is not a directory: {definitions_dir}")

        logger.info(
            f"Initializing ROM detector with definitions dir: {definitions_dir}"
        )
        self.rom_definitions: List[RomIdInfo] = []
        self._scan_definitions()
        logger.info(f"Found {len(self.rom_definitions)} ROM definition(s)")

    def _scan_definitions(self):
        """Scan all XML files in definitions directory and extract ROM ID info"""
        self.rom_definitions = []

        xml_files = list(self.definitions_dir.glob("*.xml"))
        logger.debug(f"Scanning {len(xml_files)} XML file(s) in {self.definitions_dir}")

        for xml_file in xml_files:
            try:
                rom_info = self._extract_rom_id_from_xml(xml_file)
                if rom_info:
                    self.rom_definitions.append(rom_info)
                    logger.debug(
                        f"Loaded definition: {rom_info.xmlid} from {xml_file.name}"
                    )
                else:
                    logger.warning(
                        f"Skipping {xml_file.name}: missing required ROM ID fields"
                    )
            except etree.XMLSyntaxError as e:
                logger.warning(f"Skipping {xml_file.name}: XML parse error: {e}")
            except Exception as e:
                logger.warning(
                    f"Skipping {xml_file.name}: {type(e).__name__}: {e}", exc_info=True
                )

    def _extract_rom_id_from_xml(self, xml_path: Path) -> Optional[RomIdInfo]:
        """
        Extract ROM ID information from an XML definition file

        Args:
            xml_path: Path to XML definition file

        Returns:
            RomIdInfo object or None if essential fields are missing

        Raises:
            etree.XMLSyntaxError: If XML is malformed
            DefinitionParseError: If XML structure is unexpected
        """
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        tree = etree.parse(str(xml_path), parser)
        root = tree.getroot()

        # Find romid element
        romid_elem = root.find(".//romid")
        if romid_elem is None:
            logger.debug(f"No <romid> element found in {xml_path.name}")
            return None

        def get_text(tag: str, default: str = "") -> str:
            elem = romid_elem.find(tag)
            return elem.text.strip() if elem is not None and elem.text else default

        xmlid = get_text("xmlid")
        internalidaddress = get_text("internalidaddress")
        internalidstring = get_text("internalidstring")

        # Must have these essential fields
        if not xmlid or not internalidaddress or not internalidstring:
            logger.debug(
                f"Incomplete ROM ID in {xml_path.name}: "
                f"xmlid={xmlid}, address={internalidaddress}, id={internalidstring}"
            )
            return None

        return RomIdInfo(
            xml_path=xml_path,
            xmlid=xmlid,
            internalidaddress=internalidaddress,
            internalidstring=internalidstring,
            make=get_text("make"),
            model=get_text("model"),
        )

    def detect_rom_id(self, rom_path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Detect ROM ID from a binary file by trying all known definitions

        Args:
            rom_path: Path to ROM binary file

        Returns:
            Tuple of (rom_id_string, xml_path) if match found, (None, None) otherwise

        Raises:
            RomFileNotFoundError: If ROM file doesn't exist
        """
        rom_file = Path(rom_path)
        if not rom_file.exists():
            raise RomFileNotFoundError(f"ROM file not found: {rom_path}")

        logger.info(f"Detecting ROM ID from: {rom_file.name}")

        # Load ROM data
        with open(rom_file, "rb") as f:
            rom_data = f.read()

        logger.debug(f"ROM file size: {len(rom_data)} bytes")

        # Try each definition
        for rom_info in self.rom_definitions:
            try:
                address = rom_info.internal_id_address_int
                expected_id = rom_info.internalidstring
                id_length = len(expected_id)

                # Check if address is valid for this ROM
                if address + id_length > len(rom_data):
                    logger.debug(f"Skipping {rom_info.xmlid}: address out of range")
                    continue

                # Read ID from ROM
                actual_id = rom_data[address : address + id_length].decode(
                    "ascii", errors="ignore"
                )

                # Check if it matches
                if actual_id == expected_id:
                    logger.info(f"ROM ID match found: {actual_id} ({rom_info.xmlid})")
                    return (actual_id, str(rom_info.xml_path))
            except AddressConversionError as e:
                logger.warning(f"Skipping {rom_info.xmlid}: {e}")
                continue
            except Exception as e:
                logger.warning(
                    f"Error checking {rom_info.xmlid}: {type(e).__name__}: {e}"
                )
                continue

        logger.warning(f"No matching ROM definition found for {rom_file.name}")
        return (None, None)

    def find_definition_by_id(self, rom_id: str) -> Optional[str]:
        """
        Find XML definition file by ROM ID string

        Args:
            rom_id: ROM ID string (e.g., "LF9VEB")

        Returns:
            Path to XML definition file or None if not found
        """
        for rom_info in self.rom_definitions:
            if rom_info.xmlid == rom_id or rom_info.internalidstring == rom_id:
                return str(rom_info.xml_path)
        return None

    def get_all_definitions(self) -> List[RomIdInfo]:
        """
        Get list of all available ROM definitions

        Returns:
            List of RomIdInfo objects
        """
        return self.rom_definitions

    def get_definitions_summary(self) -> List[Dict[str, str]]:
        """
        Get summary of all available definitions for display

        Returns:
            List of dictionaries with ROM info
        """
        return [
            {
                "xmlid": info.xmlid,
                "make": info.make,
                "model": info.model,
                "internalid": info.internalidstring,
                "xml_file": info.xml_path.name,
            }
            for info in self.rom_definitions
        ]
