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
    """Real UDSConnection wired to a mock J2534Device via J2534Transport.

    The J2534Transport delegates send/receive straight to the mock device's
    ``write_msgs``/``read_msgs``, so tests drive behaviour exactly as before
    by configuring ``mock_j2534_device.read_msgs``/``write_msgs``.
    """
    from src.ecu.protocol import UDSConnection
    from src.ecu.transport import J2534Transport

    return UDSConnection(J2534Transport(mock_j2534_device, channel_id=1))


@pytest.fixture(autouse=True)
def _isolate_wican_sidecars(tmp_path_factory, monkeypatch):
    """Never let a test write a WiCAN sidecar into the user's real directory.

    The datalog breadcrumb lives in ``~/.nc-flash`` so a temp clean cannot
    destroy the record of a datalogger this host left parked (#92). That makes
    stray test writes worse than untidy: a leaked breadcrumb naming a host from
    a fixture would make the next real connect to that address try to resume a
    datalogger nobody paused.

    Autouse, so isolation is the default and no future test has to remember.
    Per-test fixtures that redirect the same helpers still win, since they are
    applied after this one.
    """
    import src.ecu.wican_config as mod

    sidecars = tmp_path_factory.mktemp("wican_sidecars")
    monkeypatch.setattr(mod, "_sidecar_dir", lambda: str(sidecars))
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(sidecars))
