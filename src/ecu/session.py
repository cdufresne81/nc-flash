"""
ECU Session Manager

Holds an ECU transport (J2534 device + ISO-TP channel, or a WiCAN SLCAN link)
open for the duration of a session. Operations reuse the open connection
instead of reconnecting each time.

No keepalive polling — the connection stays valid without it. Each UDS
operation sends its own Tester Present as needed. Connect verifies the ECU is
reachable with a single Tester Present, then holds the link open for
subsequent operations.

Two adapters share this one seam (the rest of the app is transport-agnostic):

  * **J2534** (default, wired) — opens a ``J2534Device``/ISO-TP channel. This
    path is byte-for-byte unchanged from the original implementation.
  * **WiCAN** (opt-in, wireless) — opens a ``WiCANTransport`` over SLCAN/TCP.
    If auto-config is on, the device's HTTP ``protocol`` is switched to
    ``slcan`` on the FIRST connect (a ~6 s reboot) and restored only on an
    explicit disconnect / app exit — NOT on the internal auto-reconnect after a
    read — so a single session never reboots the adapter more than once.
"""

import logging
from enum import Enum
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .constants import DEFAULT_J2534_DLL

logger = logging.getLogger(__name__)


class ECUSessionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    BUSY = "busy"  # flash/read has acquired the session


class ECUSession(QObject):
    """
    Persistent ECU session over a J2534 or WiCAN transport.

    Holds the connection (device/channel/filter for J2534, or transport for
    WiCAN) open so that multiple UDS operations can reuse it without
    reconnecting each time.

    Usage:
        # J2534 (default — back-compatible positional form):
        session = ECUSession(dll_path)
        # WiCAN (opt-in):
        session = ECUSession(adapter_config={"kind": "wican", "host": ...})

        session.state_changed.connect(on_state_changed)
        session.connect_ecu()
        # ... operations use session.uds ...
        session.disconnect_ecu()
    """

    state_changed = Signal(str)  # ECUSessionState value
    connection_lost = Signal(str)  # error reason
    progress = Signal(str)  # human-readable connect-step message

    def __init__(
        self,
        dll_path: str = DEFAULT_J2534_DLL,
        parent: Optional[QObject] = None,
        *,
        adapter_config: Optional[dict] = None,
    ):
        super().__init__(parent)
        # Copy the relevant fields out of the config so the session never holds
        # a shared mutable dict (architecture rule).
        cfg = dict(adapter_config or {})
        self._adapter_kind = "wican" if cfg.get("kind") == "wican" else "j2534"
        self._dll_path = cfg.get("dll_path") or dll_path
        self._wican_host = cfg.get("host")
        self._wican_port = cfg.get("port")
        self._wican_auto_config = bool(cfg.get("auto_config", True))

        self._state = ECUSessionState.DISCONNECTED
        self._device = None
        self._channel_id = None
        self._filter_id = None
        self._uds = None

        # WiCAN-only state
        self._transport = None
        self._configurator = None
        self._slcan_prev_protocol = None  # original protocol to restore to
        self._slcan_switched = False  # True once we've switched this session
        # No-reboot coexistence (#36): the WHOLE-session bus reservation. On the
        # dedicated coexist port the datalogger owns the single CAN bus and eats the
        # ECU's UDS replies until the host reserves it, so we hold a refcounted
        # bus-claim+pause for the life of the session (acquired in _connect_wican,
        # released in _teardown_wican). The flash fence nests on this SAME client, so
        # there is exactly one bus owner. None unless connected via the coexist port.
        self._wican_datalog = None

    # --- Public API ---

    @property
    def state(self) -> ECUSessionState:
        return self._state

    @property
    def adapter_kind(self) -> str:
        """``"j2534"`` or ``"wican"`` — which transport this session drives."""
        return self._adapter_kind

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

    @property
    def transport(self):
        """The open ``EcuTransport`` (WiCAN sessions only; ``None`` for J2534)."""
        return self._transport

    @property
    def wican_datalog(self):
        """The session's coexist datalog client holding the whole-session bus
        reservation, or ``None`` (J2534, legacy reboot path, or disconnected). The
        flash driver reuses THIS instance so its fence nests on the one bus owner."""
        return self._wican_datalog

    def connect_ecu(self):
        """
        Open the transport and verify the ECU responds with one Tester Present.

        Dispatches to the J2534 or WiCAN connect path by adapter kind. The
        connection remains open for subsequent operations.
        """
        if self._state != ECUSessionState.DISCONNECTED:
            return

        self._set_state(ECUSessionState.CONNECTING)
        try:
            if self._adapter_kind == "wican":
                self._connect_wican()
            else:
                self._connect_j2534()
            self._set_state(ECUSessionState.CONNECTED)
        except Exception as e:
            logger.error("ECU session connect failed: %s", e)
            # Restore the WiCAN protocol if we switched it before failing.
            self._teardown(restore_protocol=True)
            self._set_state(ECUSessionState.DISCONNECTED)
            self.connection_lost.emit(f"Connect failed: {e}")

    def _connect_j2534(self):
        """Open J2534 device, CAN channel, ISO-TP filter, verify ECU. Unchanged."""
        from .j2534 import J2534Device, setup_isotp_flow_control
        from .protocol import UDSConnection
        from .transport import J2534Transport
        from .constants import (
            J2534_PROTOCOL_ISO15765,
            CAN_BAUDRATE,
            ISO15765_BS,
            ISO15765_STMIN,
        )

        self._device = J2534Device(self._dll_path)
        self._device.open()

        self._channel_id = self._device.connect(
            J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
        )
        self._device.set_config(self._channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})
        self._filter_id = setup_isotp_flow_control(self._device, self._channel_id)
        self._uds = UDSConnection(J2534Transport(self._device, self._channel_id))

        # Single Tester Present to verify ECU is alive
        self._uds.tester_present()
        logger.info("ECU session established (J2534)")

    def _connect_wican(self):
        """Open the WiCAN link, verify ECU.

        Prefers the **no-reboot dedicated SLCAN port** when the adapter runs
        coexistence firmware (capability-probed via ``version_ping``): that path
        skips the ``WiCANConfigurator`` protocol switch entirely (no ~6 s reboot)
        and leaves the datalogger undisturbed. Against stock/old firmware the
        probe fails and we fall back to the proven reboot-switch path — strictly
        non-breaking.
        """
        from .protocol import UDSConnection
        from .transport import create_ecu_transport
        from .wican_config import WiCANConfigurator, WiCANDatalogClient

        if not self._wican_host or not self._wican_port:
            raise ValueError("WiCAN adapter requires a host and port")

        # No-reboot coexistence (#36.C) crash recovery: if a prior run was hard-killed
        # mid-flash after pausing the datalogger, resume it now — UNLESS a flash is
        # currently active (a second NC Flash instance mid-flash; its own resume will
        # clear it). Cheap + soft-degrading: a no-op unless this host left a breadcrumb,
        # and any /datalog error is swallowed. MUST run before any flash leans on it.
        try:
            WiCANDatalogClient(self._wican_host).reconcile()
        except Exception as exc:  # never let recovery break a connect
            logger.debug("datalog reconcile at connect failed (non-fatal): %s", exc)

        # No-reboot path: coexistence firmware keeps an always-on dedicated SLCAN
        # port open, so flashing needs neither the protocol-switch reboot nor the
        # WiCANConfigurator. Probe for it first; ANY failure falls back below.
        if self._wican_auto_config and not self._slcan_switched:
            coexist = self._try_open_coexist_port()
            if coexist is not None:
                self._transport = coexist
                # Reserve the bus for the WHOLE session BEFORE the first UDS frame.
                # Without this the datalogger (poll_log) is the sole TWAI consumer and
                # swallows the ECU's reply, so Tester-Present — and every later DTC /
                # RAM-scan / flash op — would time out. Refcounted + soft-degrading; the
                # firmware dead-man reaper auto-resumes the logger if this host vanishes.
                self._wican_datalog = WiCANDatalogClient(self._wican_host)
                self._wican_datalog.acquire_bus()
                # Drain datalogger frames still in-flight when we took the bus.
                # acquire_bus() pauses poll_log, but Mode-01 PID responses already
                # on the wire keep arriving for a beat; without this they bleed into
                # the first TesterPresent receive and are mis-parsed (the benign but
                # noisy "unexpected response byte 0x41 for SID 0x3E" warnings). Flush
                # until the bus is quiet so the first UDS exchange starts clean.
                coexist.flush()
                self._uds = UDSConnection(coexist)
                self._uds.tester_present()
                logger.info(
                    "ECU session established (WiCAN no-reboot port %s:%s)",
                    self._wican_host,
                    coexist.port,
                )
                return
            # Not coexistence firmware — switch the adapter into SLCAN mode ONCE
            # per session (a ~6 s reboot). Restore only on a real disconnect.
            self.progress.emit("Switching adapter to SLCAN (~6 s reboot)…")
            self._configurator = WiCANConfigurator(self._wican_host)
            self._slcan_prev_protocol = self._enter_slcan_durable(self._configurator)
            self._slcan_switched = True

        self.progress.emit("Opening WiCAN link…")
        self._transport = create_ecu_transport(
            {"kind": "wican", "host": self._wican_host, "port": self._wican_port}
        )
        self._transport.open()
        self._uds = UDSConnection(self._transport)

        # Single Tester Present to verify ECU is alive
        self._uds.tester_present()
        logger.info(
            "ECU session established (WiCAN %s:%s)", self._wican_host, self._wican_port
        )

    def _try_open_coexist_port(self):
        """Probe for no-reboot coexistence firmware; return an OPEN transport on
        its dedicated SLCAN port, or ``None`` to fall back to the reboot path.

        A coexistence build (firmware rev ``>= COEXIST_MIN_FW_REV``) keeps an
        always-on dedicated SLCAN TCP port that routes through the
        protocol-agnostic fast-read/write codecs, so the host can flash without
        the ~6 s protocol-switch reboot and without the ``WiCANConfigurator``.
        Detection is a ``version_ping`` over that port with a short connect
        timeout. ANY failure — old firmware refusing the port, no ``NCFRv``
        marker, a network hiccup — returns ``None`` so the caller takes the
        proven legacy path. Strictly non-breaking: the probe never raises.
        """
        from .transport import create_ecu_transport
        from .wican_sd_flash import _parse_fw_rev  # reuse the NCFRv<rev> parser
        from .constants import (
            WICAN_DEDICATED_SLCAN_PORT,
            COEXIST_MIN_FW_REV,
            COEXIST_PROBE_TIMEOUT_MS,
        )

        probe = None
        try:
            probe = create_ecu_transport(
                {
                    "kind": "wican",
                    "host": self._wican_host,
                    "port": WICAN_DEDICATED_SLCAN_PORT,
                    "connect_timeout_ms": COEXIST_PROBE_TIMEOUT_MS,
                }
            )
            probe.open()
            rev = _parse_fw_rev(probe.version_ping(window_ms=COEXIST_PROBE_TIMEOUT_MS))
            if rev is not None and rev >= COEXIST_MIN_FW_REV:
                self.progress.emit("No-reboot coexistence firmware detected…")
                logger.info(
                    "WiCAN coexistence firmware NCFRv%s on dedicated port %s",
                    rev,
                    WICAN_DEDICATED_SLCAN_PORT,
                )
                return probe  # hand the OPEN transport to the caller
            logger.info(
                "WiCAN dedicated port answered rev=%s (< NCFRv%s); legacy reboot path",
                rev,
                COEXIST_MIN_FW_REV,
            )
        except Exception as exc:
            logger.debug(
                "WiCAN coexist-port probe failed (%s); legacy reboot path", exc
            )
        # Not coexistence (or probe failed): close any half-open probe, fall back.
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass
        return None

    @staticmethod
    def _enter_slcan_durable(configurator) -> str:
        """Enter SLCAN, persisting the TRUE original protocol to the crash-recovery
        sidecar BEFORE switching, and return that original.

        Mirrors ``WiCANConfigurator.slcan_session`` for this event-driven (non
        context-manager) lifecycle: if a prior run crashed mid-session it left a
        breadcrumb, so a recorded original is preferred over the device's current
        (possibly already-``slcan``) value. The breadcrumb is written before the
        switch so a hard kill during the multi-second reboot is recoverable.
        """
        recorded = configurator.read_recovery()
        current = configurator.current_protocol()
        true_prev = recorded if recorded is not None else current
        if true_prev != "slcan":
            configurator.write_recovery(true_prev)
        if current != "slcan":
            configurator.set_protocol("slcan")
        return true_prev

    def disconnect_ecu(self, restore_protocol: bool = True):
        """Close the transport.

        Args:
            restore_protocol: For WiCAN, restore the adapter's original HTTP
                protocol (a reboot). The internal auto-reconnect after a read
                passes ``False`` so the adapter stays in SLCAN across the
                reconnect; a user-initiated disconnect uses the default ``True``.
        """
        if self._state == ECUSessionState.DISCONNECTED:
            return
        if self._state == ECUSessionState.BUSY:
            # A flash/read worker is actively using the transport. Closing it now
            # (and rebooting the WiCAN protocol) would yank the link out from
            # under a running operation — a brick risk on a write. The caller
            # must release() first; refuse rather than tear down mid-operation.
            logger.warning("disconnect_ecu refused while BUSY; release() first")
            return
        self._teardown(restore_protocol=restore_protocol)
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
            # ECU rebooted — tear down the dead connection. Keep the WiCAN
            # adapter in SLCAN (restore_protocol=False): the window always
            # auto-reconnects after a connection-dead release, and rebooting the
            # adapter's protocol on every read would be a needless reboot storm.
            self._teardown(restore_protocol=False)
            self._set_state(ECUSessionState.DISCONNECTED)
            logger.info("ECU session released (connection dead after reset)")
        else:
            self._set_state(ECUSessionState.CONNECTED)
            logger.info("ECU session released")

    def cleanup(self):
        """Shut down session and restore the adapter. Call on app exit / before
        discarding the session.

        Restores the WiCAN protocol even when the session is already
        DISCONNECTED but still holds an SLCAN switch — e.g. after a
        ``release(connection_dead=True)`` whose follow-up reconnect never
        happened (a failed flash/read). Without this, discarding such a session
        would strand the adapter in SLCAN and lose the original protocol.
        """
        if self._state != ECUSessionState.DISCONNECTED:
            self._teardown(restore_protocol=True)
            self._set_state(ECUSessionState.DISCONNECTED)
        else:
            self._restore_wican_protocol()

    # --- Internal ---

    def _set_state(self, state: ECUSessionState):
        if self._state != state:
            self._state = state
            self.state_changed.emit(state.value)
            logger.debug("ECU session state: %s", state.value)

    def _teardown(self, restore_protocol: bool = True):
        """Close the transport (error-tolerant); optionally restore WiCAN protocol."""
        if self._adapter_kind == "wican":
            self._teardown_wican(restore_protocol=restore_protocol)
        else:
            self._teardown_j2534()
        self._uds = None
        logger.info("ECU session disconnected")

    def _teardown_j2534(self):
        """Clean up J2534 resources (error-tolerant). Unchanged."""
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

    def _teardown_wican(self, restore_protocol: bool):
        """Close the WiCAN transport; restore the original protocol if asked.

        ``restore_protocol`` is ``False`` on the internal auto-reconnect (keep
        the adapter in SLCAN) and ``True`` on a user disconnect / app exit.
        """
        # Release the whole-session bus reservation FIRST (resume the datalogger)
        # while the transport is still up. Best-effort: a failed release just leaves
        # the firmware dead-man reaper to auto-resume the logger. The flash fence's
        # own ref is already dropped by the time any teardown runs (its context
        # manager is fully contained in flash_rom), so this is the last ref.
        if self._wican_datalog is not None:
            try:
                self._wican_datalog.release_bus()
            except Exception:
                pass
            self._wican_datalog = None

        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

        if restore_protocol:
            self._restore_wican_protocol()

    def _restore_wican_protocol(self):
        """Restore the original WiCAN protocol and clear the recovery breadcrumb.

        Idempotent and adapter-agnostic: a no-op unless this session actually
        switched the adapter to SLCAN (``_slcan_switched``). Safe to call from
        any state, so :meth:`cleanup` can recover a session that was torn down
        without a protocol restore (``release(connection_dead=True)``).
        """
        if not (self._slcan_switched and self._configurator):
            return
        prev = self._slcan_prev_protocol
        try:
            if prev and prev != "slcan":
                self.progress.emit("Restoring adapter protocol (~6 s reboot)…")
                self._configurator.restore(prev)
        except Exception as exc:
            logger.warning("WiCAN protocol restore failed: %s", exc)
        finally:
            try:
                self._configurator.clear_recovery()
            except Exception:
                pass
            self._slcan_switched = False
            self._slcan_prev_protocol = None
            self._configurator = None
