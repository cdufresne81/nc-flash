"""
Unit tests for ROM detector module
"""

import pytest
from pathlib import Path
from src.core.rom_detector import RomDetector, RomIdInfo


class TestRomDetectorInitialization:
    """Test RomDetector initialization"""

    def test_init_with_valid_directory(self, metadata_dir):
        """Test initialization with valid metadata directory"""
        detector = RomDetector(str(metadata_dir))
        assert detector is not None
        assert detector.metadata_dir == metadata_dir
        assert len(detector.rom_definitions) > 0

    def test_init_with_invalid_directory(self):
        """Test initialization with non-existent directory"""
        with pytest.raises(FileNotFoundError):
            RomDetector("nonexistent_directory")

    def test_scans_xml_files_on_init(self, metadata_dir):
        """Test that XML files are scanned during initialization"""
        detector = RomDetector(str(metadata_dir))
        # Should find at least the lf9veb.xml
        assert len(detector.rom_definitions) >= 1


class TestRomIdExtraction:
    """Test ROM ID extraction from XML"""

    def test_extract_rom_id_from_valid_xml(self, sample_xml_path):
        """Test extracting ROM ID from valid XML file"""
        detector = RomDetector(str(sample_xml_path.parent))

        # Find the LF9VEB definition
        lf9veb = None
        for rom_def in detector.rom_definitions:
            if rom_def.xmlid == "LF9VEB":
                lf9veb = rom_def
                break

        assert lf9veb is not None
        assert lf9veb.xmlid == "LF9VEB"
        assert lf9veb.internalidstring == "LF9VEB"
        assert lf9veb.make == "Mazda"
        assert lf9veb.model == "MX5"

    def test_rom_id_info_properties(self, sample_xml_path):
        """Test RomIdInfo property conversions"""
        detector = RomDetector(str(sample_xml_path.parent))
        rom_info = detector.rom_definitions[0]

        # Test hex address conversion
        assert isinstance(rom_info.internal_id_address_int, int)
        assert rom_info.internal_id_address_int > 0


class TestRomIdDetection:
    """Test ROM ID detection from binary files"""

    def test_detect_rom_id_from_valid_rom(self, metadata_dir, sample_rom_path):
        """Test detecting ROM ID from valid binary file"""
        detector = RomDetector(str(metadata_dir))
        rom_id, xml_path = detector.detect_rom_id(str(sample_rom_path))

        assert rom_id is not None
        assert rom_id == "LF9VEB"
        assert xml_path is not None
        assert Path(xml_path).exists()
        assert "lf9veb.xml" in xml_path.lower()

    def test_detect_rom_id_from_nonexistent_file(self, metadata_dir):
        """Test detecting ROM ID from non-existent file"""
        detector = RomDetector(str(metadata_dir))

        with pytest.raises(FileNotFoundError):
            detector.detect_rom_id("nonexistent.bin")

    def test_detect_rom_id_returns_none_for_unknown_rom(self, metadata_dir, tmp_path):
        """Test that unknown ROM returns None"""
        detector = RomDetector(str(metadata_dir))

        # Create a fake ROM file with random data
        fake_rom = tmp_path / "fake.bin"
        fake_rom.write_bytes(b"FAKEID" * 1000)

        rom_id, xml_path = detector.detect_rom_id(str(fake_rom))

        assert rom_id is None
        assert xml_path is None


class TestDefinitionLookup:
    """Test finding definitions by ID"""

    def test_find_definition_by_xmlid(self, metadata_dir):
        """Test finding definition by XML ID"""
        detector = RomDetector(str(metadata_dir))
        xml_path = detector.find_definition_by_id("LF9VEB")

        assert xml_path is not None
        assert Path(xml_path).exists()
        assert "lf9veb.xml" in xml_path.lower()

    def test_find_definition_by_internal_id(self, metadata_dir):
        """Test finding definition by internal ID string"""
        detector = RomDetector(str(metadata_dir))
        xml_path = detector.find_definition_by_id("LF9VEB")

        assert xml_path is not None

    def test_find_definition_returns_none_for_unknown_id(self, metadata_dir):
        """Test that unknown ID returns None"""
        detector = RomDetector(str(metadata_dir))
        xml_path = detector.find_definition_by_id("UNKNOWN_ID")

        assert xml_path is None


class TestDefinitionSummary:
    """Test getting definition summaries"""

    def test_get_all_definitions(self, metadata_dir):
        """Test getting all ROM definitions"""
        detector = RomDetector(str(metadata_dir))
        definitions = detector.get_all_definitions()

        assert len(definitions) > 0
        assert all(isinstance(d, RomIdInfo) for d in definitions)

    def test_get_definitions_summary(self, metadata_dir):
        """Test getting summary of all definitions"""
        detector = RomDetector(str(metadata_dir))
        summary = detector.get_definitions_summary()

        assert len(summary) > 0
        assert all(isinstance(s, dict) for s in summary)

        # Check required keys in summary
        first_summary = summary[0]
        assert 'xmlid' in first_summary
        assert 'make' in first_summary
        assert 'model' in first_summary
        assert 'internalid' in first_summary
        assert 'xml_file' in first_summary

    def test_summary_contains_correct_data(self, metadata_dir):
        """Test that summary contains correct data for known ROM"""
        detector = RomDetector(str(metadata_dir))
        summary = detector.get_definitions_summary()

        # Find LF9VEB in summary
        lf9veb_summary = None
        for s in summary:
            if s['xmlid'] == 'LF9VEB':
                lf9veb_summary = s
                break

        assert lf9veb_summary is not None
        assert lf9veb_summary['make'] == 'Mazda'
        assert lf9veb_summary['model'] == 'MX5'
        assert lf9veb_summary['internalid'] == 'LF9VEB'


class TestErrorHandling:
    """Test error handling in ROM detector"""

    def test_handles_malformed_xml_gracefully(self, tmp_path):
        """Test that malformed XML doesn't crash the detector"""
        # Create a directory with malformed XML
        test_metadata = tmp_path / "metadata"
        test_metadata.mkdir()

        bad_xml = test_metadata / "bad.xml"
        bad_xml.write_text("This is not valid XML!")

        # Should not raise, just skip the bad file
        detector = RomDetector(str(test_metadata))

        # Should have no valid definitions
        assert len(detector.rom_definitions) == 0

    def test_handles_xml_without_romid(self, tmp_path):
        """Test that XML without romid element is skipped"""
        test_metadata = tmp_path / "metadata"
        test_metadata.mkdir()

        no_romid_xml = test_metadata / "no_romid.xml"
        no_romid_xml.write_text('<?xml version="1.0"?><roms><rom></rom></roms>')

        detector = RomDetector(str(test_metadata))
        assert len(detector.rom_definitions) == 0

    def test_handles_incomplete_romid_data(self, tmp_path):
        """Test that incomplete ROM ID data is skipped"""
        test_metadata = tmp_path / "metadata"
        test_metadata.mkdir()

        incomplete_xml = test_metadata / "incomplete.xml"
        incomplete_xml.write_text('''<?xml version="1.0"?>
<roms>
    <rom>
        <romid>
            <xmlid>TEST</xmlid>
            <!-- Missing internalidaddress and internalidstring -->
        </romid>
    </rom>
</roms>''')

        detector = RomDetector(str(test_metadata))
        # Should skip this incomplete definition
        assert len(detector.rom_definitions) == 0
