"""Shared helpers for ECU mock tests."""

from unittest.mock import MagicMock

CAN_RESPONSE_ID = 0x7E8


def build_uds_response(payload: bytes, can_id: int = CAN_RESPONSE_ID):
    """Build a mock PassThruMsg with 4-byte CAN ID prefix + payload."""
    full = can_id.to_bytes(4, "big") + payload
    msg = MagicMock(name="PassThruMsg")
    msg.DataSize = len(full)
    msg.Data = list(full) + [0] * (4128 - len(full))
    return msg


def build_positive_response(service_id: int, data: bytes = b""):
    """Positive UDS response: (SID + 0x40) + data."""
    return build_uds_response(bytes([service_id + 0x40]) + data)


def build_negative_response(service_id: int, nrc: int):
    """Negative UDS response: 0x7F + SID + NRC."""
    return build_uds_response(bytes([0x7F, service_id, nrc]))
