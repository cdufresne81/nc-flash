"""User-facing text for ECU operation errors (issue 91).

An EcuTek-tuned (locked) ECU refuses the programming session with NRC 0x22
("Conditions not correct"). The user used to see only the raw exception text,
with the traceback in the log. ``ecu_error_message`` maps that refusal to a
plain-words explanation; the flash/read worker emits it while still logging
the full traceback.
"""

import logging

import pytest

from ecu_test_helpers import build_negative_response, build_positive_response
from src.ecu.constants import (
    NRC_CONDITIONS_NOT_CORRECT,
    SID_DIAGNOSTIC_SESSION,
    SID_OBD_CURRENT_DATA,
    SID_SECURITY_ACCESS,
    SID_TESTER_PRESENT,
)
from src.ecu.exceptions import FlashError, NegativeResponseError
from src.ui.error_helpers import SESSION_REFUSED_MESSAGE, ecu_error_message


def _nrc(nrc, sid):
    return NegativeResponseError(nrc, "Conditions not correct", service_id=sid)


class TestEcuErrorMessage:
    def test_session_refused_maps_to_plain_words(self):
        exc = _nrc(NRC_CONDITIONS_NOT_CORRECT, SID_DIAGNOSTIC_SESSION)
        msg = ecu_error_message(exc)
        assert msg == SESSION_REFUSED_MESSAGE
        assert "EcuTek" in msg
        assert "engine is OFF" in msg

    def test_wrapped_session_refusal_is_still_recognised(self):
        cause = _nrc(NRC_CONDITIONS_NOT_CORRECT, SID_DIAGNOSTIC_SESSION)
        try:
            try:
                raise cause
            except NegativeResponseError as e:
                raise FlashError(f"Failed to scan RAM: {e}") from e
        except FlashError as wrapped:
            assert ecu_error_message(wrapped) == SESSION_REFUSED_MESSAGE

    @pytest.mark.parametrize(
        "sid", [SID_SECURITY_ACCESS, SID_OBD_CURRENT_DATA, SID_TESTER_PRESENT, None]
    )
    def test_nrc_0x22_on_other_requests_is_unchanged(self, sid):
        # 0x22 on an OBD read means "still in a programming session", not a
        # locked ECU; only the session request itself gets the EcuTek text.
        exc = _nrc(NRC_CONDITIONS_NOT_CORRECT, sid)
        assert ecu_error_message(exc) == str(exc)

    def test_other_nrc_on_session_is_unchanged(self):
        exc = NegativeResponseError(0x12, "Sub-function not supported", 0x10)
        assert ecu_error_message(exc) == str(exc)

    def test_plain_error_is_unchanged(self):
        exc = FlashError("Borrowed ECU session is not responsive")
        assert ecu_error_message(exc) == str(exc)


class TestFlashWorkerEmitsFriendlyText:
    """End to end through the real UDS layer: the ECU answers the session
    request with 7F 10 22 and the worker's error signal carries the plain
    text, while the log keeps the traceback."""

    @pytest.mark.parametrize("operation", ["read", "scan_ram"])
    def test_session_refusal_during_auth(
        self, operation, mock_uds, mock_j2534_device, caplog
    ):
        from src.ecu.flash_manager import FlashManager
        from src.ui.ecu_window import _FlashWorker

        mock_j2534_device.read_msgs.side_effect = [
            # _connect (borrowed session) and _authenticate step 1
            [build_positive_response(SID_TESTER_PRESENT, b"\x00")],
            [build_positive_response(SID_TESTER_PRESENT, b"\x00")],
            # _authenticate step 2: programming session refused
            [
                build_negative_response(
                    SID_DIAGNOSTIC_SESSION, NRC_CONDITIONS_NOT_CORRECT
                )
            ],
        ]
        manager = FlashManager()
        manager.use_uds(mock_uds)
        worker = _FlashWorker(manager, operation)
        errors = []
        worker.error.connect(errors.append)

        with caplog.at_level(logging.ERROR, logger="src.ui.ecu_window"):
            worker.run()

        assert errors == [SESSION_REFUSED_MESSAGE]
        records = [r for r in caplog.records if "Flash worker error" in r.message]
        assert records, "worker error must still be logged"
        assert records[0].exc_info is not None, "traceback must stay in the log"
        assert isinstance(records[0].exc_info[1], NegativeResponseError)
