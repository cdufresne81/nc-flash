"""
ECU Session Manager

Holds a J2534 device and ISO-TP channel open for the duration of a session.
Operations reuse the open device/channel instead of reconnecting each time.

No keepalive polling — the J2534 device handle stays valid without it.
Each UDS operation sends its own Tester Present as needed.
Connect verifies the ECU is reachable with a single Tester Present,
then holds the device open for subsequent operations.
"""

import logging
from enum import Enum
from typing import Optional

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class ECUSessionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BUSY = "busy"  # flash/read has acquired the session


class ECUSession(QObject):
    """
    Persistent J2534 device session.

    Holds the device, channel, and ISO-TP filter open so that multiple
    UDS operations can reuse them without reconnecting each time.

    Usage:
        session = ECUSession(dll_path)
        session.state_changed.connect(on_state_changed)
        session.connect_ecu()
        # ... operations use session.uds ...
        session.disconnect_ecu()
    """

    state_changed = Signal(str)  # ECUSessionState value
    connection_lost = Signal(str)  # error reason

    def __init__(self, dll_path: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._dll_path = dll_path
        self._state = ECUSessionState.DISCONNECTED
        self._device = None
        self._channel_id = None
        self._filter_id = None
        self._uds = None

    # --- Public API ---

    @property
    def state(self) -> ECUSessionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state in (ECUSessionState.CONNECTED, ECUSessionState.BUSY)

    @property
    def device(self):
        return self._device

    @property
    def channel_id(self):
        return self._channel_id

    @property
    def filter_id(self):
        return self._filter_id

    @property
    def uds(self):
        return self._uds

    def connect_ecu(self):
        """
        Open J2534 device, CAN channel, ISO-TP filter, and verify ECU responds.

        Sends a single Tester Present to confirm the ECU is reachable.
        The device and channel remain open for subsequent operations.
        """
        if self._state != ECUSessionState.DISCONNECTED:
            return

        self._set_state(ECUSessionState.CONNECTING)

        from .j2534 import J2534Device, setup_isotp_flow_control
        from .protocol import UDSConnection
        from .transport import J2534Transport
        from .constants import (
            J2534_PROTOCOL_ISO15765,
            CAN_BAUDRATE,
            ISO15765_BS,
            ISO15765_STMIN,
        )

        try:
            self._device = J2534Device(self._dll_path)
            self._device.open()

            self._channel_id = self._device.connect(
                J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
            )
            self._device.set_config(
                self._channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0}
            )
            self._filter_id = setup_isotp_flow_control(self._device, self._channel_id)
            self._uds = UDSConnection(J2534Transport(self._device, self._channel_id))

            # Single Tester Present to verify ECU is alive
            self._uds.tester_present()

            logger.info("ECU session established")
            self._set_state(ECUSessionState.CONNECTED)
        except Exception as e:
            logger.error("ECU session connect failed: %s", e)
            self._teardown()
            self._set_state(ECUSessionState.DISCONNECTED)
            self.connection_lost.emit(f"Connect failed: {e}")

    def disconnect_ecu(self):
        """Close the J2534 device and channel."""
        if self._state == ECUSessionState.DISCONNECTED:
            return
        self._teardown()
        self._set_state(ECUSessionState.DISCONNECTED)

    def acquire(self):
        """
        Acquire exclusive access to J2534 handles for flash operations.

        Returns (device, channel_id, filter_id, uds).
        Caller must call release() when done.
        """
        if self._state != ECUSessionState.CONNECTED:
            raise RuntimeError(f"Cannot acquire session in state {self._state.value}")
        self._set_state(ECUSessionState.BUSY)
        return (self._device, self._channel_id, self._filter_id, self._uds)

    def release(self, connection_dead: bool = False):
        """
        Release exclusive access after flash operation.

        Args:
            connection_dead: True if ECU was reset (connection is dead).
        """
        if self._state != ECUSessionState.BUSY:
            return
        if connection_dead:
            # ECU rebooted — tear down the dead connection
            self._teardown()
            self._set_state(ECUSessionState.DISCONNECTED)
            logger.info("ECU session released (connection dead after reset)")
        else:
            self._set_state(ECUSessionState.CONNECTED)
            logger.info("ECU session released")

    def cleanup(self):
        """Shut down session. Call on app exit."""
        if self._state != ECUSessionState.DISCONNECTED:
            self._teardown()
            self._set_state(ECUSessionState.DISCONNECTED)

    # --- Internal ---

    def _set_state(self, state: ECUSessionState):
        if self._state != state:
            self._state = state
            self.state_changed.emit(state.value)
            logger.debug("ECU session state: %s", state.value)

    def _teardown(self):
        """Clean up J2534 resources (error-tolerant)."""
        if self._filter_id is not None and self._device and self._channel_id:
            try:
                self._device.stop_msg_filter(self._channel_id, self._filter_id)
            except Exception:
                pass
            self._filter_id = None

        if self._channel_id is not None and self._device:
            try:
                self._device.disconnect(self._channel_id)
            except Exception:
                pass
            self._channel_id = None

        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None

        self._uds = None
        logger.info("ECU session disconnected")
