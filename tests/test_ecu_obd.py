"""Tests for OBD-II PID reading (protocol.py read_obd_pid, read_battery_voltage, read_engine_rpm)."""

from unittest.mock import MagicMock, patch
import pytest


def _make_uds(response_bytes):
    """Create a UDSConnection mock that returns given bytes from send_request."""
    from src.ecu.protocol import UDSConnection

    uds = UDSConnection.__new__(UDSConnection)
    uds._transport = MagicMock()
    uds.send_request = MagicMock(return_value=response_bytes)
    return uds


class TestReadObdPid:
    def test_valid_response_strips_echo(self):
        # PID 0x0C response: [0x0C, data_a, data_b]
        uds = _make_uds(bytes([0x0C, 0x12, 0x34]))
        result = uds.read_obd_pid(0x0C)
        assert result == bytes([0x12, 0x34])

    def test_pid_echo_mismatch_raises(self):
        from src.ecu.exceptions import UDSError

        uds = _make_uds(bytes([0xFF, 0x00]))
        with pytest.raises(UDSError, match="unexpected response format"):
            uds.read_obd_pid(0x0C)

    def test_empty_response_raises(self):
        from src.ecu.exceptions import UDSError

        uds = _make_uds(b"")
        with pytest.raises(UDSError, match="unexpected response format"):
            uds.read_obd_pid(0x42)

    def test_none_response_raises(self):
        from src.ecu.exceptions import UDSError

        uds = _make_uds(None)
        with pytest.raises(UDSError, match="unexpected response format"):
            uds.read_obd_pid(0x42)


class TestReadBatteryVoltage:
    def test_valid_voltage(self):
        # PID 0x42: 2 bytes, value/1000 = volts
        # 12.4V = 12400 = 0x3070
        uds = _make_uds(bytes([0x42, 0x30, 0x70]))
        result = uds.read_battery_voltage()
        assert result == pytest.approx(12.4, abs=0.01)

    def test_low_voltage(self):
        # 11.5V = 11500 = 0x2CEC
        uds = _make_uds(bytes([0x42, 0x2C, 0xEC]))
        result = uds.read_battery_voltage()
        assert result == pytest.approx(11.5, abs=0.01)

    def test_send_request_exception_returns_none(self):
        uds = _make_uds(None)
        uds.send_request = MagicMock(side_effect=Exception("timeout"))
        result = uds.read_battery_voltage()
        assert result is None

    def test_short_response_returns_none(self):
        # Only 1 data byte instead of 2
        uds = _make_uds(bytes([0x42, 0x30]))
        result = uds.read_battery_voltage()
        assert result is None


class TestReadEngineRpm:
    def test_engine_off(self):
        # 0 RPM = 0x0000
        uds = _make_uds(bytes([0x0C, 0x00, 0x00]))
        result = uds.read_engine_rpm()
        assert result == 0.0

    def test_idle_rpm(self):
        # 800 RPM = 800 * 4 = 3200 = 0x0C80
        uds = _make_uds(bytes([0x0C, 0x0C, 0x80]))
        result = uds.read_engine_rpm()
        assert result == pytest.approx(800.0)

    def test_high_rpm(self):
        # 7000 RPM = 7000 * 4 = 28000 = 0x6D60
        uds = _make_uds(bytes([0x0C, 0x6D, 0x60]))
        result = uds.read_engine_rpm()
        assert result == pytest.approx(7000.0)

    def test_send_request_exception_returns_none(self):
        uds = _make_uds(None)
        uds.send_request = MagicMock(side_effect=Exception("NRC"))
        result = uds.read_engine_rpm()
        assert result is None
