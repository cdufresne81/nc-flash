"""
Tests for Metadata Writer

Tests XML scaling update functionality.
"""

import pytest
from pathlib import Path
from lxml import etree
import tempfile
import shutil

from src.core.metadata_writer import update_scaling, get_scaling_attributes


@pytest.fixture
def sample_xml_content():
    """Sample XML content with scalings"""
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rom>
    <romid>
        <xmlid>TEST001</xmlid>
    </romid>
    <scaling name="TestScaling" units="%" min="0" max="100" format="%0.2f" inc="1" storagetype="uint8" expression="x" frexpr="x"/>
    <scaling name="FuelScaling" units="ms" min="0" max="25.5" format="%0.1f" storagetype="uint8" expression="x*0.1" frexpr="x/0.1"/>
    <table name="TestTable" scaling="TestScaling" address="0x1000"/>
</rom>
"""


@pytest.fixture
def temp_xml_file(sample_xml_content):
    """Create a temporary XML file for testing"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
        f.write(sample_xml_content)
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    if temp_path.exists():
        temp_path.unlink()
    backup_path = temp_path.with_suffix('.xml.bak')
    if backup_path.exists():
        backup_path.unlink()


class TestUpdateScaling:
    """Tests for update_scaling function"""

    def test_update_scaling_min_max(self, temp_xml_file):
        """Test updating min and max attributes"""
        result = update_scaling(temp_xml_file, "TestScaling", {"min": "10", "max": "200"})

        assert result is True

        # Verify changes were written
        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["min"] == "10"
        assert attrs["max"] == "200"
        # Other attributes should remain unchanged
        assert attrs["units"] == "%"
        assert attrs["format"] == "%0.2f"

    def test_update_scaling_units(self, temp_xml_file):
        """Test updating units attribute"""
        result = update_scaling(temp_xml_file, "TestScaling", {"units": "kPa"})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["units"] == "kPa"

    def test_update_scaling_format(self, temp_xml_file):
        """Test updating format string"""
        result = update_scaling(temp_xml_file, "TestScaling", {"format": "%0.3f"})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["format"] == "%0.3f"

    def test_update_scaling_inc(self, temp_xml_file):
        """Test updating increment value"""
        result = update_scaling(temp_xml_file, "TestScaling", {"inc": "0.5"})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["inc"] == "0.5"

    def test_update_multiple_attributes(self, temp_xml_file):
        """Test updating multiple attributes at once"""
        updates = {
            "min": "5",
            "max": "95",
            "units": "degrees",
            "format": "%0.1f",
            "inc": "2"
        }
        result = update_scaling(temp_xml_file, "TestScaling", updates)

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["min"] == "5"
        assert attrs["max"] == "95"
        assert attrs["units"] == "degrees"
        assert attrs["format"] == "%0.1f"
        assert attrs["inc"] == "2"

    def test_update_scaling_remove_attribute(self, temp_xml_file):
        """Test removing an attribute by setting to None"""
        result = update_scaling(temp_xml_file, "TestScaling", {"min": None})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert "min" not in attrs
        # max should still exist
        assert attrs["max"] == "100"

    def test_update_nonexistent_scaling(self, temp_xml_file):
        """Test updating a scaling that doesn't exist"""
        result = update_scaling(temp_xml_file, "NonExistentScaling", {"min": "10"})

        assert result is False

    def test_update_scaling_creates_backup(self, temp_xml_file):
        """Test that updating creates a backup file"""
        backup_path = temp_xml_file.with_suffix('.xml.bak')

        # Ensure backup doesn't exist initially
        if backup_path.exists():
            backup_path.unlink()

        result = update_scaling(temp_xml_file, "TestScaling", {"min": "10"})

        assert result is True
        assert backup_path.exists()

    def test_update_scaling_nonexistent_file(self):
        """Test updating a file that doesn't exist"""
        result = update_scaling(Path("/nonexistent/path/file.xml"), "TestScaling", {"min": "10"})

        assert result is False

    def test_update_different_scaling(self, temp_xml_file):
        """Test updating a different scaling in the file"""
        result = update_scaling(temp_xml_file, "FuelScaling", {"min": "0.5", "max": "30"})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "FuelScaling")
        assert attrs["min"] == "0.5"
        assert attrs["max"] == "30"
        assert attrs["units"] == "ms"  # unchanged


class TestGetScalingAttributes:
    """Tests for get_scaling_attributes function"""

    def test_get_existing_scaling(self, temp_xml_file):
        """Test getting attributes from existing scaling"""
        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")

        assert attrs["name"] == "TestScaling"
        assert attrs["units"] == "%"
        assert attrs["min"] == "0"
        assert attrs["max"] == "100"
        assert attrs["format"] == "%0.2f"
        assert attrs["inc"] == "1"

    def test_get_nonexistent_scaling(self, temp_xml_file):
        """Test getting attributes from nonexistent scaling"""
        attrs = get_scaling_attributes(temp_xml_file, "NonExistentScaling")

        assert attrs == {}

    def test_get_from_nonexistent_file(self):
        """Test getting attributes from nonexistent file"""
        attrs = get_scaling_attributes(Path("/nonexistent/path/file.xml"), "TestScaling")

        assert attrs == {}

    def test_get_scaling_with_expression(self, temp_xml_file):
        """Test getting scaling that has expression attribute"""
        attrs = get_scaling_attributes(temp_xml_file, "FuelScaling")

        assert attrs["expression"] == "x*0.1"
        assert attrs["frexpr"] == "x/0.1"


class TestEdgeCases:
    """Tests for edge cases and error handling"""

    def test_update_with_numeric_value(self, temp_xml_file):
        """Test that numeric values are converted to strings"""
        result = update_scaling(temp_xml_file, "TestScaling", {"min": 10, "max": 200.5})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["min"] == "10"
        assert attrs["max"] == "200.5"

    def test_update_preserves_other_elements(self, temp_xml_file):
        """Test that updating scaling doesn't affect other XML elements"""
        # Update a scaling
        update_scaling(temp_xml_file, "TestScaling", {"min": "10"})

        # Verify the table element is still intact
        tree = etree.parse(str(temp_xml_file))
        tables = tree.xpath("//table[@name='TestTable']")
        assert len(tables) == 1
        assert tables[0].get("scaling") == "TestScaling"

    def test_malformed_xml(self):
        """Test handling of malformed XML file"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write("This is not valid XML <broken>")
            temp_path = Path(f.name)

        try:
            result = update_scaling(temp_path, "TestScaling", {"min": "10"})
            assert result is False

            attrs = get_scaling_attributes(temp_path, "TestScaling")
            assert attrs == {}
        finally:
            temp_path.unlink()

    def test_empty_updates_dict(self, temp_xml_file):
        """Test with empty updates dictionary"""
        # Get original attributes
        original_attrs = get_scaling_attributes(temp_xml_file, "TestScaling")

        result = update_scaling(temp_xml_file, "TestScaling", {})

        assert result is True

        # Attributes should remain unchanged
        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs == original_attrs

    def test_add_new_attribute(self, temp_xml_file):
        """Test adding a new attribute that didn't exist"""
        result = update_scaling(temp_xml_file, "TestScaling", {"newattr": "newvalue"})

        assert result is True

        attrs = get_scaling_attributes(temp_xml_file, "TestScaling")
        assert attrs["newattr"] == "newvalue"
