"""Unit tests for the WiCAN SLCAN-port open (``ECUSession._open_coexist_transport``).

The firmware has exactly one mode and one CAN socket: the always-on coexistence
SLCAN listener. The session opens it, version-pings it, and adopts it only when
the firmware rev is new enough (``COEXIST_MIN_FW_REV``). There is no fallback
path any more, so a failure is simply a failed connect — but WHICH failure still
matters, because "nothing is listening on the port" (update the firmware) and
"the device never answered" (check the network) send the user to completely
different places.

Two invariants these tests exist to protect:

  * every raising path closes the probe transport (a leaked half-open socket on
    the adapter's only CAN listener would block the next connect attempt);
  * silence earns ONE longer retry before we give up, so a lossy WiFi link is
    not reported as broken firmware.
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

WICAN_CFG = {"kind": "wican", "host": "192.168.1.169"}


@pytest.fixture(autouse=True)
def _qapp():
    """ECUSession is a QObject, so a QApplication must exist. Autouse — the
    sibling suite does the same, and threading it through every signature as an
    inert argument bought nothing."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _session():
    return ECUSession(adapter_config=dict(WICAN_CFG))


def _fake_probe(marker):
    """A probe transport whose version_ping yields ``marker`` bytes."""
    probe = MagicMock()
    probe.port = WICAN_DEDICATED_SLCAN_PORT
    probe.version_ping.return_value = marker
    return probe


class TestCoexistTransportOpen:
    @pytest.mark.parametrize(
        "rev",
        [COEXIST_MIN_FW_REV, COEXIST_MIN_FW_REV + 3],
        ids=["at-the-threshold", "newer"],
    )
    def test_supported_firmware_adopted(self, rev):
        """COEXIST_MIN_FW_REV is the contract both sides commit to, so the
        boundary case matters: an off-by-one would lock every device on the
        minimum build out of the app entirely."""
        probe = _fake_probe(b"NCFRv%d" % rev)
        with patch(
            "src.ecu.transport.create_ecu_transport", return_value=probe
        ) as mock_create:
            result = _session()._open_coexist_transport()

        assert result is probe  # adopted -- handed back OPEN
        probe.open.assert_called_once()
        probe.close.assert_not_called()
        # Probed the dedicated port with the short capability timeout.
        cfg = mock_create.call_args.args[0]
        assert cfg["port"] == WICAN_DEDICATED_SLCAN_PORT
        assert cfg["connect_timeout_ms"] == COEXIST_PROBE_TIMEOUT_MS

    def test_old_firmware_raises_naming_both_revs_and_closes(self):
        # A pre-coexistence build answers the port but is below the threshold.
        # The message must name what it found AND what is required, or the user
        # cannot tell whether their OTA actually took.
        probe = _fake_probe(b"NCFRv%d" % (COEXIST_MIN_FW_REV - 1))
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            with pytest.raises(WiCANError) as excinfo:
                _session()._open_coexist_transport()

        message = str(excinfo.value)
        assert f"NCFRv{COEXIST_MIN_FW_REV - 1}" in message
        assert f"NCFRv{COEXIST_MIN_FW_REV}" in message
        probe.close.assert_called_once()

    def test_silence_retries_once_then_raises_and_closes(self):
        # The port ACCEPTED the connection and then said nothing. Only the
        # coexistence listener ever binds that port, so silence is far more
        # likely to be a slow link or a busy device than the wrong firmware --
        # it earns one longer window before we report a failure.
        probe = _fake_probe(None)
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            with pytest.raises(WiCANError) as excinfo:
                _session()._open_coexist_transport()

        assert "never sent its firmware marker" in str(excinfo.value)
        assert probe.version_ping.call_count == 2
        probe.close.assert_called_once()

    def test_connect_refused_blames_the_firmware_and_closes(self):
        # Nothing listening on the port IS proof of stock/old firmware. Note the
        # cause chain, not the message, is what the implementation inspects.
        probe = MagicMock()
        err = WiCANError("connection refused")
        err.__cause__ = ConnectionRefusedError(111, "Connection refused")
        probe.open.side_effect = err
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            with pytest.raises(WiCANError) as excinfo:
                _session()._open_coexist_transport()

        message = str(excinfo.value)
        assert "Nothing is listening" in message
        assert f"NCFRv{COEXIST_MIN_FW_REV}" in message
        # _open()'s own cleanup closes the half-open socket; the outer handler
        # must not depend on that, so at least one close must have happened.
        assert probe.close.called

    def test_unreachable_blames_the_network_not_the_firmware(self):
        # A device that is simply off or on another subnet must NOT be reported
        # as needing a firmware update -- that sends the user to reflash a
        # perfectly good adapter.
        probe = MagicMock()
        err = WiCANError("timed out")
        err.__cause__ = TimeoutError("timed out")
        probe.open.side_effect = err
        with patch("src.ecu.transport.create_ecu_transport", return_value=probe):
            with pytest.raises(WiCANError) as excinfo:
                _session()._open_coexist_transport()

        message = str(excinfo.value)
        assert "Could not reach" in message
        assert "Nothing is listening" not in message

    def test_create_transport_raises_is_reported_not_swallowed(self):
        with patch(
            "src.ecu.transport.create_ecu_transport",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(WiCANError) as excinfo:
                _session()._open_coexist_transport()
        assert "boom" in str(excinfo.value)

    def test_a_refused_connect_is_not_retried(self):
        # The confirming retry exists ONLY to tell a slow refusal from a timeout.
        # A refusal that already landed needs no second attempt; retrying would
        # add ~3 s to every connect against stock firmware.
        probe = MagicMock()
        err = WiCANError("connection refused")
        err.__cause__ = ConnectionRefusedError(111, "Connection refused")
        probe.open.side_effect = err
        with patch(
            "src.ecu.transport.create_ecu_transport", return_value=probe
        ) as mock_create:
            with pytest.raises(WiCANError):
                _session()._open_coexist_transport()
        assert mock_create.call_count == 1
