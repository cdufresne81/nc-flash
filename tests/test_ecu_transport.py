"""
Tests for src/ecu/transport.py — the UDS-message transport seam.

Tier 1: J2534Transport must be byte-for-byte identical to the raw I/O
that UDSConnection.send_request performs today. A regression here would
mis-frame every ECU message and could brick hardware, so the send path
(build_isotp_msg + write_msgs args) and receive path (Data[4:DataSize],
None on empty / short / timeout, J2534Error propagation) are all pinned.
"""

from unittest.mock import MagicMock

import pytest

from src.ecu.constants import (
    CAN_REQUEST_ID,
    J2534_PROTOCOL_ISO15765,
    ISO15765_TX_FLAGS,
)
from src.ecu.exceptions import J2534Error
from src.ecu.transport import (
    EcuTransport,
    J2534Transport,
    FakeTransport,
    create_ecu_transport,
)

from ecu_test_helpers import build_uds_response

# ---------------------------------------------------------------------------
# J2534Transport — send path
# ---------------------------------------------------------------------------


class TestJ2534TransportSend:
    """send_message must frame via build_isotp_msg and call write_msgs."""

    def test_send_writes_single_isotp_msg(self):
        device = MagicMock(name="J2534Device")
        transport = J2534Transport(device, channel_id=7)

        payload = b"\x10\x02"
        transport.send_message(payload, timeout_ms=250)

        device.write_msgs.assert_called_once()
        args, _kwargs = device.write_msgs.call_args
        channel_id, msgs, timeout = args
        assert channel_id == 7
        assert timeout == 250
        assert len(msgs) == 1

    def test_send_frames_payload_with_can_request_id_prefix(self):
        """The written msg = 4-byte CAN_REQUEST_ID prefix + UDS payload."""
        device = MagicMock(name="J2534Device")
        transport = J2534Transport(device, channel_id=1)

        payload = b"\x22\xf1\x90"
        transport.send_message(payload, timeout_ms=100)

        msg = device.write_msgs.call_args[0][1][0]
        assert msg.ProtocolID == J2534_PROTOCOL_ISO15765
        assert msg.TxFlags == ISO15765_TX_FLAGS
        assert msg.DataSize == 4 + len(payload)
        expected = CAN_REQUEST_ID.to_bytes(4, "big") + payload
        assert bytes(msg.Data[: msg.DataSize]) == expected

    def test_send_propagates_j2534_error(self):
        device = MagicMock(name="J2534Device")
        device.write_msgs.side_effect = J2534Error("bus off")
        transport = J2534Transport(device, channel_id=1)

        with pytest.raises(J2534Error):
            transport.send_message(b"\x3e\x00", timeout_ms=50)


# ---------------------------------------------------------------------------
# J2534Transport — receive path
# ---------------------------------------------------------------------------


class TestJ2534TransportReceive:
    """receive_message must return Data[4:DataSize], None on empty/short."""

    def test_receive_strips_4_byte_can_id_prefix(self):
        device = MagicMock(name="J2534Device")
        device.read_msgs.return_value = [build_uds_response(b"\x50\x02")]
        transport = J2534Transport(device, channel_id=3)

        result = transport.receive_message(timeout_ms=200)

        assert result == b"\x50\x02"
        device.read_msgs.assert_called_once_with(3, 1, 200)

    def test_receive_returns_none_on_empty_read(self):
        """read_msgs returns [] on timeout/empty -> None."""
        device = MagicMock(name="J2534Device")
        device.read_msgs.return_value = []
        transport = J2534Transport(device, channel_id=1)

        assert transport.receive_message(timeout_ms=100) is None

    def test_receive_returns_none_when_only_can_id_present(self):
        """DataSize <= 4 (CAN ID only, no UDS payload) -> None."""
        device = MagicMock(name="J2534Device")
        # build_uds_response with empty payload => DataSize == 4
        msg = build_uds_response(b"")
        assert msg.DataSize == 4
        device.read_msgs.return_value = [msg]
        transport = J2534Transport(device, channel_id=1)

        assert transport.receive_message(timeout_ms=100) is None

    def test_receive_returns_full_multibyte_payload(self):
        device = MagicMock(name="J2534Device")
        payload = bytes(range(0x40, 0x60))  # 32-byte UDS payload
        device.read_msgs.return_value = [build_uds_response(payload)]
        transport = J2534Transport(device, channel_id=1)

        assert transport.receive_message(timeout_ms=100) == payload

    def test_receive_propagates_j2534_error(self):
        device = MagicMock(name="J2534Device")
        device.read_msgs.side_effect = J2534Error("device disconnected")
        transport = J2534Transport(device, channel_id=1)

        with pytest.raises(J2534Error):
            transport.receive_message(timeout_ms=100)


# ---------------------------------------------------------------------------
# J2534Transport — lifecycle / metadata
# ---------------------------------------------------------------------------


class TestJ2534TransportLifecycle:
    def test_open_close_are_noops(self):
        """open/close must not touch the device (ECUSession owns it)."""
        device = MagicMock(name="J2534Device")
        transport = J2534Transport(device, channel_id=1)

        transport.open()
        transport.close()

        device.open.assert_not_called()
        device.close.assert_not_called()
        device.connect.assert_not_called()
        device.disconnect.assert_not_called()

    def test_flush_is_a_safe_noop(self):
        """A reliable J2534 link has nothing to flush; it must not touch I/O."""
        device = MagicMock(name="J2534Device")
        transport = J2534Transport(device, channel_id=1)
        transport.flush()
        device.read_msgs.assert_not_called()
        device.write_msgs.assert_not_called()

    def test_description(self):
        transport = J2534Transport(MagicMock(), channel_id=1)
        assert transport.description == "J2534"

    def test_is_ecu_transport(self):
        transport = J2534Transport(MagicMock(), channel_id=1)
        assert isinstance(transport, EcuTransport)


# ---------------------------------------------------------------------------
# FakeTransport
# ---------------------------------------------------------------------------


class TestFakeTransport:
    def test_is_ecu_transport(self):
        assert isinstance(FakeTransport(), EcuTransport)

    def test_records_sent_payloads(self):
        transport = FakeTransport()
        transport.send_message(b"\x10\x02", timeout_ms=100)
        transport.send_message(b"\x27\x01", timeout_ms=200)

        assert transport.sent == [(b"\x10\x02", 100), (b"\x27\x01", 200)]
        assert transport.sent_payloads == [b"\x10\x02", b"\x27\x01"]

    def test_scripted_queue_served_in_order(self):
        transport = FakeTransport(responses=[b"\x50\x02", b"\x67\x01"])

        assert transport.receive_message(100) == b"\x50\x02"
        assert transport.receive_message(100) == b"\x67\x01"
        # Queue exhausted -> None (timeout)
        assert transport.receive_message(100) is None

    def test_queued_none_models_timeout(self):
        transport = FakeTransport(responses=[None, b"\x50\x02"])

        assert transport.receive_message(100) is None
        assert transport.receive_message(100) == b"\x50\x02"

    def test_queue_response_appends(self):
        transport = FakeTransport()
        assert transport.receive_message(100) is None

        transport.queue_response(b"\x7e\x00")
        assert transport.receive_message(100) == b"\x7e\x00"

    def test_responder_callable_takes_precedence(self):
        calls: list[int] = []

        def responder(timeout_ms: int):
            calls.append(timeout_ms)
            return b"\x51"

        transport = FakeTransport(responses=[b"\xff"], responder=responder)

        assert transport.receive_message(123) == b"\x51"
        assert transport.receive_message(456) == b"\x51"
        assert calls == [123, 456]

    def test_open_close_recorded(self):
        transport = FakeTransport()
        assert transport.opened is False
        assert transport.closed is False

        transport.open()
        transport.close()

        assert transport.opened is True
        assert transport.closed is True

    def test_default_and_custom_description(self):
        assert FakeTransport().description == "Fake"
        assert FakeTransport(description="WiCAN-sim").description == "WiCAN-sim"


# ---------------------------------------------------------------------------
# create_ecu_transport factory
# ---------------------------------------------------------------------------


class TestCreateEcuTransport:
    """The factory maps a serialisable config to a concrete transport."""

    def test_j2534_kind_builds_j2534_transport(self):
        device = MagicMock(name="J2534Device")
        transport = create_ecu_transport(
            {"kind": "j2534", "device": device, "channel_id": 5}
        )
        assert isinstance(transport, J2534Transport)
        # The returned transport must drive the supplied device/channel.
        transport.send_message(b"\x3e\x00", timeout_ms=100)
        channel_id, _msgs, _timeout = device.write_msgs.call_args[0]
        assert channel_id == 5

    def test_j2534_missing_keys_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required key"):
            create_ecu_transport({"kind": "j2534", "device": MagicMock()})

    def test_wican_kind_builds_wican_transport(self):
        from src.ecu.wican_transport import WiCANTransport

        transport = create_ecu_transport(
            {"kind": "wican", "host": "192.168.4.1", "port": 3333}
        )
        assert isinstance(transport, WiCANTransport)
        # Not opened by the factory — no socket created.
        assert transport.description == "WiCAN (192.168.4.1:3333)"

    def test_wican_forwards_optional_params(self):
        transport = create_ecu_transport(
            {
                "kind": "wican",
                "host": "10.0.0.9",
                "port": 1234,
                "tx_id": 0x7E5,
                "rx_id": 0x7ED,
                "connect_timeout_ms": 1500,
                "padding": 0xAA,
            }
        )
        assert transport._tx_id == 0x7E5
        assert transport._rx_id == 0x7ED
        assert transport._connect_timeout_ms == 1500
        assert transport._padding == 0xAA

    def test_wican_missing_keys_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required key"):
            create_ecu_transport({"kind": "wican", "host": "10.0.0.1"})

    def test_missing_kind_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required 'kind'"):
            create_ecu_transport({"host": "x", "port": 1})

    def test_unknown_kind_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown transport kind"):
            create_ecu_transport({"kind": "carbus"})
