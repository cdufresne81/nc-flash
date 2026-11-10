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


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point get_settings() at a throwaway INI file (never the user's registry)."""
    from PySide6.QtCore import QSettings

    import src.utils.settings as settings_mod

    s = settings_mod.AppSettings()
    s.settings = QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat)
    monkeypatch.setattr(settings_mod, "_settings", s)
    return s


class FakeClipboard:
    """In-memory stand-in for the system clipboard (text and mime data)."""

    def __init__(self):
        from PySide6.QtCore import QMimeData

        self._mime = QMimeData()

    def setMimeData(self, mime, mode=None):  # noqa: N802 - QClipboard API
        self._mime = mime

    def mimeData(self, mode=None):  # noqa: N802
        return self._mime

    def setText(self, text, mode=None):  # noqa: N802
        from PySide6.QtCore import QMimeData

        self._mime = QMimeData()
        self._mime.setText(text)

    def text(self, mode=None):
        return self._mime.text()


@pytest.fixture
def fake_clipboard(monkeypatch):
    """Copy/paste through an in-memory clipboard, never the system one.

    On Windows another program can hold the clipboard for a moment (a
    clipboard manager, a remote-desktop clipboard sync): a paste right after
    setText() then reads nothing and the test fails at random. The real
    clipboard also belongs to the user, whose copied text a test would wipe.
    """
    from PySide6.QtWidgets import QApplication

    board = FakeClipboard()
    monkeypatch.setattr(QApplication, "clipboard", staticmethod(lambda: board))
    return board


# --- graph-engine tests: off by default on local runs ----------------------


def pytest_collection_modifyitems(config, items):
    """Skip the slow, window-heavy graph tests (``@pytest.mark.graph``) locally.

    Run them by hand after touching the table display or the graph engine:
    ``NCFLASH_GRAPH_TESTS=1 pytest tests/test_graph_gpu.py``. CI always runs
    them (GitHub Actions sets ``CI``; the lavapipe job sets
    ``NCFLASH_REQUIRE_GPU``).
    """
    import os

    if (
        os.environ.get("CI")
        or os.environ.get("NCFLASH_REQUIRE_GPU") == "1"
        or os.environ.get("NCFLASH_GRAPH_TESTS") == "1"
    ):
        return
    skip = pytest.mark.skip(
        reason="graph tests are off locally (NCFLASH_GRAPH_TESTS=1)"
    )
    for item in items:
        if item.get_closest_marker("graph"):
            item.add_marker(skip)


# --- CI: record pytest's verdict, skip native teardown after the run -------


def pytest_sessionfinish(session, exitstatus):
    session.config._ncflash_session = session


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """Record pytest's own exit status, then (Linux CI) end the process with it.

    After every test has passed, CI runs sometimes die while the process shuts
    down: on Linux, heap corruption ("malloc_consolidate(): unaligned fastbin
    chunk detected", exit 134) or a segfault (139) while native libraries
    unload (Qt widgets the tests left alive, the GPU stack); on Windows, a bare
    exit code 1 with no output (#149). Nothing is left to check at that point.

    - ``NCFLASH_PYTEST_STATUS_FILE`` set (CI): write pytest's final status
      there, so the CI step can tell "pytest failed" from "the process died
      after pytest passed".
    - Linux CI: skip that shutdown with ``os._exit``. Not on Windows: there an
      ``os._exit`` is reported as 1 too. Local runs are unchanged: os._exit
      would also kill a caller that runs pytest in-process.
    """
    import os
    import sys

    session = getattr(config, "_ncflash_session", None)
    if session is None:
        return
    # session.exitstatus, not sessionfinish's argument: it is what pytest returns
    status = int(session.exitstatus)
    status_file = os.environ.get("NCFLASH_PYTEST_STATUS_FILE")
    if status_file:
        # no newline: on Windows its carriage return would survive the CI
        # step's $(cat ...) and "0" would never match
        with open(status_file, "w", encoding="ascii") as f:
            f.write(str(status))
    if not os.environ.get("CI") or os.name == "nt":
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(status)
