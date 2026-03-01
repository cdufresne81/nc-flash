"""
Pytest configuration and shared fixtures
"""

import pytest
from pathlib import Path


@pytest.fixture
def project_root():
    """Return the project root directory"""
    return Path(__file__).parent.parent


@pytest.fixture
def definitions_dir(project_root):
    """Return the metadata directory path"""
    return project_root / "examples" / "metadata"


@pytest.fixture
def examples_dir(project_root):
    """Return the examples directory path"""
    return project_root / "examples"


@pytest.fixture
def sample_rom_path(examples_dir):
    """Return path to sample ROM file"""
    return examples_dir / "lf9veb.bin"


@pytest.fixture
def sample_xml_path(definitions_dir):
    """Return path to sample XML metadata file"""
    return definitions_dir / "lf9veb.xml"
