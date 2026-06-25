"""Unit tests for the WiCAN no-reboot coexistence-port capability probe
(``ECUSession._try_open_coexist_port``).

The probe opens the always-on dedicated SLCAN port, version-pings it, and only
adopts it when the firmware rev is new enough (``COEXIST_MIN_FW_REV``). Every
failure mode must degrade to ``None`` so the caller falls back to the proven
reboot-switch path — the probe must NEVER raise.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.ecu.session import ECUSession
from src.ecu.constants import (
    WICAN_DEDICATED_SLCAN_PORT,
    COEXIST_MIN_FW_REV,
    COEXIST_PROBE_TIMEOUT_MS,
)
from src.ecu.wican_transport import WiCANError

WICAN_CFG = {
    "kind": "wican",
    "host": "192.168.1.169",
    "port": 35000,
    "auto_config": True,
}


@pytest.fixture
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _session(_qapp):
    return ECUSession(adapter_config=dict(WICAN_CFG))


def _fake_probe(marker):
    """A probe transport whose version_ping yields ``marker`` bytes."""
    probe = MagicMock()
    probe.port = WICAN_DEDICATED_SLCAN_PORT
    probe.version_ping.return_value = marker
    return probe


class TestCoexistProbe:
    def test_coexist_firmware_adopted(self, _qapp):
        probe = _fake_probe(b"NCFRv%d" % COEXIST_MIN_FW_REV)
        with patch(
            "src.ecu.transport.create_ecu_transport", return_value=probe
        ) as mock_create:
            result = _session(_qapp)._try_open_coexist_port()

        assert result is probe  # adopted — handed back OPEN
        probe.open.assert_called_once()
        probe.close.assert_not_called()
        # Probed the dedicated port with the short capability timeout.
        cfg = mock_create.call_args.args[0]
        assert cfg["port"] == WICAN_DEDICATED_SLCAN_PORT
        assert cfg["connect_timeout_ms"] == COEXIST_PROBE_TIMEOUT_MS

    def test_newer_firmware_adopted(self, _qapp):
        probe = _fake_probe(b"NCFRv%d" % (COEXIST_MIN_FW_REV + 3))
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            assert _session(_qapp)._try_open_coexist_port() is probe

    def test_old_firmware_rejected_and_closed(self, _qapp):
        # A pre-coexistence build (e.g. the fastwrite NCFRv5) answers the port but
        # is below the threshold → reject and close, fall back to reboot path.
        probe = _fake_probe(b"NCFRv%d" % (COEXIST_MIN_FW_REV - 1))
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            assert _session(_qapp)._try_open_coexist_port() is None
        probe.close.assert_called_once()

    def test_no_marker_rejected_and_closed(self, _qapp):
        probe = _fake_probe(None)  # port open but no NCFRv marker
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            assert _session(_qapp)._try_open_coexist_port() is None
        probe.close.assert_called_once()

    def test_connect_refused_returns_none(self, _qapp):
        # Old firmware has no dedicated port → TCP connect refused. Must not raise.
        probe = MagicMock()
        probe.open.side_effect = WiCANError("connection refused")
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            assert _session(_qapp)._try_open_coexist_port() is None
        probe.close.assert_called_once()

    def test_create_transport_raises_returns_none(self, _qapp):
        with patch(
            "src.ecu.transport.create_ecu_transport",
            side_effect=RuntimeError("boom"),
        ):
            # No probe was created, nothing to close — just a clean None.
            assert _session(_qapp)._try_open_coexist_port() is None
