"""
Pytest configuration and shared fixtures
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from ecu_test_helpers import (  # noqa: F401 — re-exported for fixture use
    build_uds_response,
    build_positive_response,
    build_negative_response,
)


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


@pytest.fixture
def mock_j2534_device():
    """MagicMock standing in for J2534Device.

    Pre-configured with sensible defaults. Tests override
    ``read_msgs.side_effect`` or ``read_msgs.return_value`` per scenario.
    """
    device = MagicMock(name="J2534Device")
    device.open.return_value = None
    device.close.return_value = None
    device.connect.return_value = 1
    device.disconnect.return_value = None
    device.set_config.return_value = None
    device.start_msg_filter.return_value = 100
    device.stop_msg_filter.return_value = None
    device.write_msgs.return_value = None
    device.read_msgs.return_value = []
    device.__enter__ = MagicMock(return_value=device)
    device.__exit__ = MagicMock(return_value=None)
    return device


@pytest.fixture
def mock_uds(mock_j2534_device):
    """Real UDSConnection wired to a mock J2534Device."""
    from src.ecu.protocol import UDSConnection

    return UDSConnection(mock_j2534_device, channel_id=1)
