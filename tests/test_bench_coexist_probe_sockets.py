"""Real-socket checks for how the coexistence probe classifies failures (#92).

These exist because a mocked exception cannot pin OS behaviour. A unit test can
set ``__cause__`` to a ``ConnectionRefusedError`` and pass -- while the real
thing FAILED on the bench, because Windows does not deliver that error inside
the probe's 1.5 s budget. Winsock receives the RST for a closed port and
deliberately ignores it, retransmitting the SYN on its own schedule; the refusal
only surfaces once that schedule is exhausted, measured at ~2.0 s on default
settings.

The stakes changed with the legacy-path removal but did not go away. Nothing
writes the device's stored mode any more, so a misclassification can no longer
strand an adapter -- but it still decides which of two completely different
things the user is told: "update the firmware" (nothing is listening on the
port) versus "check your network" (the device never answered). Getting that
backwards sends someone to debug a healthy WiFi link.

Real sockets are the only thing that can catch that class of drift, so this runs
against a genuinely closed local port. No device, no network, ~4 s.

    venv-windows\\Scripts\\python.exe -m pytest tests/test_bench_coexist_probe_sockets.py -m bench

Deselected from normal runs by the ``bench`` marker.
"""

import socket
import time

import pytest

from src.ecu.constants import WICAN_DEDICATED_SLCAN_PORT, COEXIST_PROBE_TIMEOUT_MS
from src.ecu.session import ECUSession, _COEXIST_PROBE_RETRY_MS
from src.ecu.wican_transport import WiCANError

pytestmark = pytest.mark.bench


@pytest.fixture
def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _port_is_free(port: int) -> bool:
    """True when nothing is listening on the loopback ``port``."""
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        return True
    else:
        return False
    finally:
        probe.close()


def test_real_refused_port_reports_old_firmware(_qapp):
    """A really-closed port must be reported as old firmware, not a network fault.

    This is the regression the bench caught. If it ever fails again, either the
    OS refusal latency now exceeds ``_COEXIST_PROBE_RETRY_MS``, or the cause
    chain stopped carrying ``ConnectionRefusedError`` -- and every user on stock
    firmware gets sent to debug their WiFi instead of flashing the adapter.
    """
    if not _port_is_free(WICAN_DEDICATED_SLCAN_PORT):
        pytest.skip(
            f"something is listening on {WICAN_DEDICATED_SLCAN_PORT}; "
            "this check needs a genuinely closed port"
        )

    session = ECUSession(adapter_config={"kind": "wican", "host": "127.0.0.1"})

    started = time.monotonic()
    with pytest.raises(WiCANError) as excinfo:
        session._open_coexist_transport()
    elapsed_ms = (time.monotonic() - started) * 1000

    message = str(excinfo.value)
    assert "Nothing is listening" in message, (
        f"a closed port was reported as {message!r} after {elapsed_ms:.0f} ms. "
        "Users on stock firmware would be told to check their network instead "
        "of to update the adapter. If the refusal simply arrived late, raise "
        f"_COEXIST_PROBE_RETRY_MS (currently {_COEXIST_PROBE_RETRY_MS} ms)."
    )
    # It must also resolve within the budget we actually promise the user: the
    # first attempt, plus the one confirming retry, plus slack for scheduling.
    budget_ms = COEXIST_PROBE_TIMEOUT_MS + _COEXIST_PROBE_RETRY_MS + 2000
    assert (
        elapsed_ms < budget_ms
    ), f"probe took {elapsed_ms:.0f} ms, over the {budget_ms} ms budget"


def test_real_unreachable_host_reports_a_network_problem(_qapp):
    """A black-holed address must be reported as unreachable, not as old firmware.

    The counterpart to the test above: a refusal is conclusive, silence is not.
    192.0.2.1 is TEST-NET-1 (RFC 5737) -- reserved for documentation and never
    routed, so packets are dropped rather than answered. Telling that user to
    reflash their adapter would be actively misleading.
    """
    session = ECUSession(adapter_config={"kind": "wican", "host": "192.0.2.1"})

    with pytest.raises(WiCANError) as excinfo:
        session._open_coexist_transport()

    message = str(excinfo.value)
    assert "Could not reach" in message, (
        f"an unroutable host was reported as {message!r}; anything that blames "
        "the firmware sends the user to reflash a device that is simply not on "
        "the network"
    )
    assert "Nothing is listening" not in message
