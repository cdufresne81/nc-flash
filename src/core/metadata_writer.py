"""
Metadata Writer

Utilities for updating metadata XML files (scalings, tables, etc.)
"""

from pathlib import Path
from lxml import etree
import logging
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)


def update_scaling(xml_path: Path, scaling_name: str, updates: dict) -> bool:
    """
    Update scaling attributes in XML file.

    Args:
        xml_path: Path to metadata XML file
        scaling_name: The scaling's name attribute to find
        updates: Dict of attribute names to new values
                 e.g., {"min": "0.0", "max": "100.0", "units": "%"}
                 Use None to remove an attribute

    Returns:
        True if successful, False otherwise
    """
    if not xml_path.exists():
        logger.error(f"XML file not found: {xml_path}")
        return False

    try:
        # Parse XML preserving formatting
        parser = etree.XMLParser(remove_blank_text=False)
        tree = etree.parse(str(xml_path), parser)
        root = tree.getroot()

        # Find the scaling element
        scaling_elements = root.xpath(f".//scaling[@name='{scaling_name}']")

        if not scaling_elements:
            logger.error(f"Scaling '{scaling_name}' not found in {xml_path}")
            return False

        scaling_elem = scaling_elements[0]

        # Update attributes
        for attr, value in updates.items():
            if value is None:
                # Remove attribute if it exists
                if attr in scaling_elem.attrib:
                    del scaling_elem.attrib[attr]
            else:
                # Set attribute value
                scaling_elem.set(attr, str(value))

        # Create backup before writing
        backup_path = xml_path.with_suffix(f".xml.bak")
        shutil.copy2(xml_path, backup_path)

        # Write back to file
        tree.write(
            str(xml_path),
            encoding='UTF-8',
            xml_declaration=True,
            standalone='yes'
        )

        logger.info(f"Updated scaling '{scaling_name}' in {xml_path}")
        return True

    except etree.XMLSyntaxError as e:
        logger.error(f"XML syntax error in {xml_path}: {e}")
        return False
    except PermissionError as e:
        logger.error(f"Permission denied writing to {xml_path}: {e}")
        return False
    except Exception as e:
        logger.error(f"Error updating scaling in {xml_path}: {e}")
        return False


def get_scaling_attributes(xml_path: Path, scaling_name: str) -> dict:
    """
    Get current attributes of a scaling element.

    Args:
        xml_path: Path to metadata XML file
        scaling_name: The scaling's name attribute

    Returns:
        Dict of attribute names to values, or empty dict if not found
    """
    if not xml_path.exists():
        return {}

    try:
        tree = etree.parse(str(xml_path))
        root = tree.getroot()

        scaling_elements = root.xpath(f".//scaling[@name='{scaling_name}']")
        if scaling_elements:
            return dict(scaling_elements[0].attrib)
        return {}

    except Exception as e:
        logger.error(f"Error reading scaling from {xml_path}: {e}")
        return {}
