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
  * **WiCAN** (opt-in, wireless) — opens a ``WiCANTransport`` over SLCAN/TCP
    on the adapter's fixed coexistence port
    (``WICAN_DEDICATED_SLCAN_PORT``). The firmware has one mode and one CAN
    socket, so the host never reconfigures or reboots the adapter; it reserves
    the CAN bus from the datalogger for the life of the session instead.
"""

import logging
import time
from enum import Enum
from typing import Optional

from PySide6.QtCore import QObject, Signal

from .constants import DEFAULT_J2534_DLL

logger = logging.getLogger(__name__)

#: Longer window for the ONE version-ping retry when the coexistence port
#: accepted the connection but stayed silent. A busy device or a lossy WiFi link
#: can eat the first window; a retry is far cheaper than wrongly telling the user
#: their firmware is wrong when their WiFi simply dropped a packet.
_COEXIST_PROBE_RETRY_MS = 3000


def is_connection_refused(exc: BaseException) -> bool:
    """True when ``exc`` was ultimately caused by a refused TCP connect.

    The transport wraps socket errors (``raise WiCANError(...) from exc``), so
    the real cause sits on ``__cause__``/``__context__`` rather than in the
    message. Matching on the exception type is reliable on both Windows
    (WSAECONNREFUSED maps to ConnectionRefusedError) and POSIX; matching on the
    text would not be.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ConnectionRefusedError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


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

        self._state = ECUSessionState.DISCONNECTED
        self._device = None
        self._channel_id = None
        self._filter_id = None
        self._uds = None

        # WiCAN-only state
        self._transport = None
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
            # Release any bus reservation taken before the failure.
            self._teardown()
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
        """Open the WiCAN link on the dedicated SLCAN port, verify ECU.

        The firmware fork has exactly one mode and one CAN socket: the always-on
        coexistence SLCAN listener on ``WICAN_DEDICATED_SLCAN_PORT``. There is no
        protocol to switch, no reboot, and no user-chosen port — a device that
        does not answer there is running old or stock firmware and is reported as
        such rather than reconfigured.
        """
        from .protocol import UDSConnection
        from .wican_config import WiCANDatalogClient

        if not self._wican_host:
            raise ValueError("WiCAN adapter requires a host")

        # Crash recovery: if a prior run was hard-killed mid-flash after pausing the
        # datalogger, resume it now — UNLESS a flash is currently active (a second NC
        # Flash instance mid-flash; its own resume will clear it). Cheap +
        # soft-degrading: a no-op unless this host left a breadcrumb, and any
        # /datalog error is swallowed. MUST run before any flash leans on it.
        try:
            WiCANDatalogClient(self._wican_host).reconcile()
        except Exception as exc:  # never let recovery break a connect
            logger.debug("datalog reconcile at connect failed (non-fatal): %s", exc)

        self.progress.emit("Opening WiCAN link…")
        transport = self._open_coexist_transport()
        self._transport = transport
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
        transport.flush()
        self._uds = UDSConnection(transport)
        self._uds.tester_present()
        logger.info(
            "ECU session established (WiCAN %s:%s)",
            self._wican_host,
            transport.port,
        )

    def _open_coexist_transport(self):
        """Open the dedicated SLCAN port and verify the firmware contract.

        Returns the OPEN transport, or raises :class:`WiCANError` with a message
        that says which of the four distinguishable failures happened. The
        distinction is worth keeping even without a fallback path: "nothing is
        listening" (old firmware — update it) and "the network is unreachable"
        send the user to completely different places.

        Never leaves a socket behind: the probe transport is closed on every
        raising path.
        """
        from .transport import create_ecu_transport
        from .wican_sd_flash import _parse_fw_rev  # reuse the NCFRv<rev> parser
        from .wican_transport import WiCANError
        from .constants import (
            WICAN_DEDICATED_SLCAN_PORT,
            COEXIST_MIN_FW_REV,
            COEXIST_PROBE_TIMEOUT_MS,
        )

        def _open(timeout_ms):
            transport = create_ecu_transport(
                {
                    "kind": "wican",
                    "host": self._wican_host,
                    "port": WICAN_DEDICATED_SLCAN_PORT,
                    "connect_timeout_ms": timeout_ms,
                }
            )
            try:
                transport.open()
            except Exception:
                # Close here or the half-open socket leaks: the caller only ever
                # sees the exception, never this transport, so this is the only
                # place that can clean it up. Matters doubly now that a failed
                # connect may be retried.
                try:
                    transport.close()
                except Exception:
                    pass
                raise
            return transport

        probe = None
        outcome = "unresolved"
        # Set instead of raised: the transport itself raises WiCANError for a
        # failed connect, so a `raise` inside the try below would be caught by
        # our own handler and re-raised UNGRADED — the user would see a bare
        # "timed out" instead of being told which thing to go fix.
        graded = None
        t0 = time.monotonic()
        connected_at = None
        retry_note = None
        try:
            try:
                probe = _open(COEXIST_PROBE_TIMEOUT_MS)
            except Exception as first:
                if is_connection_refused(first):
                    raise
                retry_at = time.monotonic()
                # A timed-out connect and a REFUSED one are the same event seen
                # through too short a window: measured on Windows, the RST for a
                # closed port takes ~2 s to surface, while the probe budget is
                # 1.5 s -- so a genuinely pre-coexistence device looks like a
                # network problem and the user is sent to debug their WiFi
                # instead of updating the firmware. Retry once, long enough for a
                # refusal to land, purely to tell the two apart. Only failing
                # paths pay this; a healthy port connects in ~20 ms.
                try:
                    probe = _open(_COEXIST_PROBE_RETRY_MS)
                except Exception as second:
                    refused = is_connection_refused(second)
                    retry_note = (
                        f"attempt 1 unresolved in {COEXIST_PROBE_TIMEOUT_MS} ms; "
                        f"confirming retry -> "
                        f"{'refused' if refused else 'still unresolved'}"
                        f" in {(time.monotonic() - retry_at) * 1000:.0f} ms"
                    )
                    raise
                retry_note = (
                    f"attempt 1 unresolved in {COEXIST_PROBE_TIMEOUT_MS} ms; "
                    f"confirming retry -> connected in "
                    f"{(time.monotonic() - retry_at) * 1000:.0f} ms"
                )
            connected_at = time.monotonic()
            marker = probe.version_ping(window_ms=COEXIST_PROBE_TIMEOUT_MS)
            if marker is None:
                # The port ACCEPTED us and then said nothing. Only the
                # coexistence listener binds this port, so silence here is far
                # more likely to be a slow link or a busy device than the wrong
                # firmware. Give it one longer window before giving up.
                logger.info(
                    "WiCAN dedicated port accepted but stayed silent; retrying "
                    "the version ping with a longer window"
                )
                marker = probe.version_ping(window_ms=_COEXIST_PROBE_RETRY_MS)
            rev = _parse_fw_rev(marker)
            if rev is not None and rev >= COEXIST_MIN_FW_REV:
                outcome = f"NCFRv{rev}"
                logger.info(
                    "WiCAN firmware NCFRv%s on dedicated port %s",
                    rev,
                    WICAN_DEDICATED_SLCAN_PORT,
                )
            elif rev is not None:
                outcome = f"NCFRv{rev} too old"
                graded = (
                    f"WiCAN firmware NCFRv{rev} is too old — NC Flash requires "
                    f"NCFRv{COEXIST_MIN_FW_REV}+. Update the WiCAN firmware."
                )
            else:
                outcome = "no marker"
                graded = (
                    f"The adapter accepted the connection on port "
                    f"{WICAN_DEDICATED_SLCAN_PORT} but never sent its firmware "
                    f"marker. Check the Wi-Fi link and try again."
                )
        except Exception as exc:
            self._close_probe(probe)
            if is_connection_refused(exc):
                outcome = "refused"
                raise WiCANError(
                    f"Nothing is listening on SLCAN port "
                    f"{WICAN_DEDICATED_SLCAN_PORT} at {self._wican_host}. The "
                    f"adapter is running old or stock firmware — update it to "
                    f"NCFRv{COEXIST_MIN_FW_REV}+."
                ) from exc
            outcome = "unreachable"
            raise WiCANError(
                f"Could not reach the WiCAN's SLCAN port "
                f"{WICAN_DEDICATED_SLCAN_PORT} at {self._wican_host}: {exc}. "
                f"Check that the device is powered and on this network."
            ) from exc
        finally:
            now = time.monotonic()
            # One line per probe carrying the outcome and where the time went, so
            # a field report is attributable to connect / ping instead of guessed
            # at. A probe that needed the confirming retry is logged at WARNING
            # with the measured refusal latency: that number is the only evidence
            # that would justify re-tuning _COEXIST_PROBE_RETRY_MS, and it varies
            # with the OS TCP retransmission settings, not machine speed.
            logger.log(
                logging.WARNING if retry_note else logging.INFO,
                "coexist probe: outcome=%s connect=%.2fs ping=%.2fs total=%.2fs%s",
                outcome,
                (connected_at - t0) if connected_at else (now - t0),
                (now - connected_at) if connected_at else 0.0,
                now - t0,
                f" [{retry_note}]" if retry_note else "",
            )

        if graded is not None:
            self._close_probe(probe)
            raise WiCANError(graded)
        return probe  # OPEN, handed to the caller

    @staticmethod
    def _close_probe(probe):
        """Close a probe transport, swallowing errors. Never leaks a socket."""
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass

    def disconnect_ecu(self):
        """Close the transport and release the WiCAN bus reservation."""
        if self._state == ECUSessionState.DISCONNECTED:
            return
        if self._state == ECUSessionState.BUSY:
            # A flash/read worker is actively using the transport. Closing it
            # now would yank the link out from under a running operation — a
            # brick risk on a write — and hand the CAN bus back to the
            # datalogger mid-flash. The caller must release() first; refuse
            # rather than tear down mid-operation.
            logger.warning("disconnect_ecu refused while BUSY; release() first")
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
            # ECU rebooted — tear down the dead connection. Teardown still runs
            # so the whole-session bus reservation is released and the
            # datalogger resumes; the window auto-reconnects (and re-reserves)
            # after a connection-dead release.
            self._teardown()
            self._set_state(ECUSessionState.DISCONNECTED)
            logger.info("ECU session released (connection dead after reset)")
        else:
            self._set_state(ECUSessionState.CONNECTED)
            logger.info("ECU session released")

    def cleanup(self):
        """Shut down the session. Call on app exit / before discarding it.

        A no-op when already DISCONNECTED: teardown has then already released
        the bus reservation, and the firmware dead-man reaper resumes the
        datalogger if this host ever vanishes without doing so.
        """
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
        """Close the transport (error-tolerant) and release the bus reservation."""
        if self._adapter_kind == "wican":
            self._teardown_wican()
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

    def _teardown_wican(self):
        """Release the bus reservation, then close the WiCAN transport."""
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
