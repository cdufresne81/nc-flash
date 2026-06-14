"""
Tests proving V2 TCM detection works against the real dump.

These cover the migration from the old V1 TCM definition to the four V2
definitions, including detection against the real LFG1TF000 dump and that
each V2 definition parses into a usable RomDefinition.
"""

import pytest
from pathlib import Path
from src.core.rom_detector import RomDetector
from src.core.definition_parser import DefinitionParser

V2_DEFINITIONS = [
    "LFACTA000_v02.xml",
    "LFAMTA000_v02.xml",
    "LFG1TF000_v02.xml",
    "LFG1TG000_v02.xml",
]


class TestTcmV2Detection:
    """Detection of the V2 TCM definitions against the real dump."""

    def test_detect_lfg1tf000_v2(self, definitions_dir, examples_dir):
        """detect_rom_id matches the real LFG1TF000 dump to the V2 def."""
        detector = RomDetector(str(definitions_dir))
        rom_path = examples_dir / "LFG1TF000.bin"

        matched_id, xml_path = detector.detect_rom_id(str(rom_path))

        assert matched_id == "SW-LFG1TF000.HEX"
        assert xml_path is not None
        assert Path(xml_path).name.endswith("LFG1TF000_v02.xml")

    def test_old_v1_definition_removed(self, definitions_dir):
        """The old V1 TCM definition must no longer exist."""
        assert not (definitions_dir / "lfg1tf000.xml").exists()


class TestTcmV2DefinitionsParse:
    """Each V2 definition must parse into a usable RomDefinition."""

    @pytest.mark.parametrize("def_name", V2_DEFINITIONS)
    def test_v2_definition_parses(self, definitions_dir, def_name):
        """Loading a V2 def yields a RomDefinition with an id and tables."""
        xml_path = definitions_dir / def_name
        assert xml_path.exists()

        definition = DefinitionParser(str(xml_path)).parse()

        assert definition.romid is not None
        assert definition.romid.xmlid
        assert len(definition.tables) > 0
