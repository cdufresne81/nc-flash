"""
Flash Manager — ECU Flash Orchestration

This is the most safety-critical module in the application.
A failed flash can brick the ECU. Every operation validates
preconditions, uses strict state machine transitions, and
provides abort/timeout mechanisms.

Safety principles:
1. ROM validated BEFORE any ECU communication
2. State machine prevents step skipping or re-entry
3. Every J2534/UDS call has a timeout
4. User can abort between transfer blocks
5. Comprehensive logging at every step
6. All exceptions caught and mapped to descriptive messages
"""

import logging
import threading
import time
from enum import Enum
from typing import Callable, Optional

from .constants import (
    ROM_SIZE,
    ROM_FLASH_START_MIN,
    BLOCK_SIZE,
    SBL_SIZE,
    CAN_BAUDRATE,
    J2534_PROTOCOL_ISO15765,
    DEFAULT_J2534_DLL,
)
from .exceptions import (
    ECUError,
    FlashError,
    FlashAbortedError,
    ROMValidationError,
    ChecksumError,
    NegativeResponseError,
    J2534Error,
    UDSError,
    SecureModuleNotAvailable,
)
from .checksum import correct_rom_checksums
from .rom_utils import (
    detect_vehicle_generation,
    validate_rom_size,
    find_first_difference,
    calculate_flash_start_index,
)

# Security module: try private, fall back to stub
try:
    from ._secure import compute_security_key, get_sbl_data

    SECURE_MODULE_AVAILABLE = True
except ImportError:
    from ._secure_stub import compute_security_key, get_sbl_data

    SECURE_MODULE_AVAILABLE = False

logger = logging.getLogger(__name__)


class FlashState(Enum):
    """Flash operation state machine states."""

    IDLE = "idle"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    READING = "reading"
    SCANNING_RAM = "scanning_ram"
    PREPARING_SBL = "preparing_sbl"
    TRANSFERRING_SBL = "transferring_sbl"
    TRANSFERRING_PROGRAM = "transferring_program"
    FINALIZING = "finalizing"
    RESETTING = "resetting"
    COMPLETE = "complete"
    ERROR = "error"
    ABORTED = "aborted"


# Valid state transitions
_TRANSITIONS = {
    FlashState.IDLE: {FlashState.CONNECTING},
    FlashState.CONNECTING: {FlashState.AUTHENTICATING, FlashState.ERROR},
    # Flash path: AUTHENTICATING -> PREPARING_SBL
    # Read path:  AUTHENTICATING -> READING
    # Scan path:  AUTHENTICATING -> SCANNING_RAM
    FlashState.AUTHENTICATING: {
        FlashState.PREPARING_SBL,
        FlashState.READING,
        FlashState.SCANNING_RAM,
        FlashState.ERROR,
    },
    FlashState.READING: {FlashState.COMPLETE, FlashState.ERROR, FlashState.ABORTED},
    FlashState.SCANNING_RAM: {
        FlashState.COMPLETE,
        FlashState.ERROR,
        FlashState.ABORTED,
    },
    FlashState.PREPARING_SBL: {FlashState.TRANSFERRING_SBL, FlashState.ERROR},
    FlashState.TRANSFERRING_SBL: {
        FlashState.TRANSFERRING_PROGRAM,
        FlashState.READING,
        FlashState.ERROR,
        FlashState.ABORTED,
    },
    FlashState.TRANSFERRING_PROGRAM: {
        FlashState.FINALIZING,
        FlashState.ERROR,
        FlashState.ABORTED,
    },
    FlashState.FINALIZING: {FlashState.RESETTING, FlashState.ERROR},
    FlashState.RESETTING: {FlashState.COMPLETE, FlashState.ERROR},
}


class FlashProgress:
    """Progress information passed to callbacks."""

    def __init__(
        self,
        state: FlashState,
        percent: float = 0.0,
        message: str = "",
        bytes_sent: int = 0,
        bytes_total: int = 0,
    ):
        self.state = state
        self.percent = percent
        self.message = message
        self.bytes_sent = bytes_sent
        self.bytes_total = bytes_total


ProgressCallback = Callable[[FlashProgress], None]


class FlashManager:
    """
    ECU flash orchestration manager.

    Coordinates the complete flash workflow: J2534 connection,
    UDS authentication, SBL upload, ROM transfer, and ECU reset.
    """

    def __init__(self, dll_path: str = DEFAULT_J2534_DLL):
        """
        Args:
            dll_path: Path to J2534 DLL (default: op20pt32.dll)
        """
        self._dll_path = dll_path
        self._state = FlashState.IDLE
        self._abort_event = threading.Event()
        self._device = None
        self._channel_id = None
        self._filter_id = None
        self._uds = None
        self._owns_connection = True  # False when using a borrowed session

    def use_session(self, device, channel_id, filter_id, uds) -> None:
        """
        Borrow handles from an ECUSession instead of opening a new connection.

        When using a borrowed session, _connect() is a no-op and _cleanup()
        does NOT close the device/channel (the session owns those).
        """
        if device is None or channel_id is None or uds is None:
            raise FlashError(
                "Cannot borrow session: device, channel_id, and uds must not be None"
            )
        self._device = device
        self._channel_id = channel_id
        self._filter_id = filter_id
        self._uds = uds
        self._owns_connection = False

    @property
    def state(self) -> FlashState:
        return self._state

    @property
    def is_busy(self) -> bool:
        return self._state not in (
            FlashState.IDLE,
            FlashState.COMPLETE,
            FlashState.ERROR,
            FlashState.ABORTED,
        )

    def _set_state(self, new_state: FlashState) -> None:
        """Transition to a new state with validation.

        Invalid transitions are blocked and logged as errors.
        Terminal states (COMPLETE, ERROR, ABORTED) refuse all outbound transitions.
        """
        allowed = _TRANSITIONS.get(self._state)
        if allowed is None or new_state not in allowed:
            logger.error(
                f"Invalid state transition blocked: "
                f"{self._state.value} -> {new_state.value}"
            )
            return
        old = self._state
        self._state = new_state
        logger.info(f"Flash state: {old.value} -> {new_state.value}")

    def _notify(
        self,
        callback: Optional[ProgressCallback],
        message: str,
        percent: float = 0.0,
        bytes_sent: int = 0,
        bytes_total: int = 0,
    ) -> None:
        """Send progress update to callback."""
        if callback:
            callback(
                FlashProgress(
                    state=self._state,
                    percent=percent,
                    message=message,
                    bytes_sent=bytes_sent,
                    bytes_total=bytes_total,
                )
            )

    def _check_abort(self) -> bool:
        """Check if abort has been requested. Thread-safe."""
        return self._abort_event.is_set()

    def abort(self) -> None:
        """
        Request abort of the current flash operation.

        The abort is checked between transfer blocks. It is NOT
        immediate — the current block will complete first.
        """
        if self.is_busy:
            self._abort_event.set()
            logger.warning("Flash abort requested by user")

    def _cleanup(self) -> None:
        """Clean up J2534 resources regardless of outcome.

        When using a borrowed session (_owns_connection=False), only clears
        local references without closing the device/channel.
        """
        if not self._owns_connection:
            # Session owns the resources — just drop our references
            self._device = None
            self._channel_id = None
            self._filter_id = None
            self._uds = None
            self._owns_connection = True  # Reset for potential reuse
            return

        try:
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
        except Exception as e:
            logger.error(f"Cleanup error (non-fatal): {e}")

    def _connect(self, callback: Optional[ProgressCallback] = None) -> None:
        """Establish J2534 connection and setup ISO-TP filters."""
        if not self._owns_connection:
            # Using borrowed session — verify ECU is still responsive
            self._set_state(FlashState.CONNECTING)
            self._notify(callback, "Verifying ECU session...", percent=5.0)
            try:
                self._uds.tester_present()
            except Exception as e:
                raise FlashError(f"Borrowed ECU session is not responsive: {e}") from e
            logger.info("Borrowed ECU session verified alive")
            return

        from .j2534 import J2534Device, setup_isotp_flow_control

        self._set_state(FlashState.CONNECTING)
        self._notify(callback, "Connecting to J2534 device...")

        self._device = J2534Device(self._dll_path)
        self._device.open()

        self._channel_id = self._device.connect(
            J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
        )

        # Set ISO-15765 block size and separation time to 0
        from .constants import ISO15765_BS, ISO15765_STMIN

        self._device.set_config(self._channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})

        # Setup flow control filter
        self._filter_id = setup_isotp_flow_control(self._device, self._channel_id)

        # Create UDS connection
        from .protocol import UDSConnection

        self._uds = UDSConnection(self._device, self._channel_id)

        self._notify(callback, "Connected to ECU", percent=5.0)
        logger.info("J2534 connection established")

    def _authenticate(
        self,
        callback: Optional[ProgressCallback] = None,
    ) -> None:
        """Perform UDS session setup and security access."""
        self._set_state(FlashState.AUTHENTICATING)

        # Step 1: Tester Present
        self._notify(callback, "Sending Tester Present...", percent=8.0)
        self._uds.tester_present()

        # Step 2: Programming Session
        self._notify(callback, "Entering programming session...", percent=10.0)
        self._uds.diagnostic_session()

        # Step 3: Security Access
        self._notify(callback, "Requesting security seed...", percent=12.0)
        seed = self._uds.security_access_request_seed()

        self._notify(callback, "Computing security key...", percent=14.0)
        key = compute_security_key(seed)
        logger.info("Security key computed: [%s] from seed [%s]", key.hex(), seed.hex())

        self._notify(callback, "Sending security key...", percent=16.0)
        self._uds.security_access_send_key(key)

        self._notify(callback, "Authentication complete", percent=20.0)
        logger.info("ECU authentication complete")

    @staticmethod
    def _save_archive(rom_data: bytes, archive_path: str) -> None:
        """
        Save the flashed ROM as the current ECU archive.

        Overwrites the archive file each time — it always reflects
        what's currently on the ECU. Like romdrop's .rda file.
        """
        from pathlib import Path

        path = Path(archive_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rom_data)
        logger.info(f"ROM archive saved: {path}")

    def flash_rom(
        self,
        rom_data: bytes,
        progress_cb: Optional[ProgressCallback] = None,
        archive_path: str | None = None,
    ) -> None:
        """
        Flash a complete ROM to the ECU.

        This is the primary flash method. It performs:
        1. ROM validation (size, generation, checksums)
        2. J2534 connection setup
        3. UDS authentication
        4. SBL preparation and upload
        5. ROM data transfer
        6. Transfer finalization and ECU reset

        Args:
            rom_data: Complete 1MB ROM data
            progress_cb: Optional progress callback
            archive_path: Optional file path to save as current ECU ROM archive

        Raises:
            ROMValidationError: ROM fails pre-flight checks
            SecureModuleNotAvailable: Security module not installed
            FlashAbortedError: User aborted the operation
            FlashError: Flash procedure failed
            ECUError: Any ECU communication error
        """
        if not SECURE_MODULE_AVAILABLE:
            raise SecureModuleNotAvailable()

        if self.is_busy:
            raise FlashError("Flash operation already in progress")

        self._abort_event.clear()
        self._state = FlashState.IDLE

        try:
            self._flash_rom_inner(
                rom_data, ROM_FLASH_START_MIN, progress_cb, archive_path
            )
        except FlashAbortedError:
            self._set_state(FlashState.ABORTED)
            self._notify(progress_cb, "Flash aborted by user")
            raise
        except Exception as e:
            self._set_state(FlashState.ERROR)
            self._notify(progress_cb, f"Flash failed: {e}")
            raise
        finally:
            self._cleanup()

    def dynamic_flash(
        self,
        rom_data: bytes,
        archive_path: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> None:
        """
        Flash only the changed regions of ROM (differential flash).

        Reads the current ECU archive from archive_path, compares against
        rom_data to determine the minimal flash region, then flashes only
        the changed portion. Updates the archive after success.

        Args:
            rom_data: New ROM data to flash
            archive_path: Path to the current ECU ROM archive file
            progress_cb: Optional progress callback

        Raises:
            ROMValidationError: ROMs are identical, archive missing, or validation fails
            FlashError: Flash procedure failed
        """
        from pathlib import Path

        if not SECURE_MODULE_AVAILABLE:
            raise SecureModuleNotAvailable()

        if self.is_busy:
            raise FlashError("Flash operation already in progress")

        self._abort_event.clear()
        self._state = FlashState.IDLE

        # Validate new ROM
        if not validate_rom_size(rom_data):
            raise ROMValidationError(f"New ROM size invalid: {len(rom_data)}")

        # Load archive (what's currently on ECU)
        archive_file = Path(archive_path)
        if not archive_file.is_file():
            raise ROMValidationError(
                f"No ECU archive found at {archive_path}. "
                "Perform a full flash first to create the archive."
            )

        archive_rom_data = archive_file.read_bytes()
        if not validate_rom_size(archive_rom_data):
            raise ROMValidationError(
                f"Archive ROM size invalid: {len(archive_rom_data)}"
            )

        # Find first difference
        diff_offset = find_first_difference(rom_data, archive_rom_data)
        if diff_offset < 0:
            raise ROMValidationError("ROMs are identical — nothing to flash")

        flash_start = calculate_flash_start_index(diff_offset)
        logger.info(
            f"Dynamic flash: first diff at 0x{diff_offset:06X}, "
            f"flash start at 0x{flash_start:06X}"
        )

        try:
            self._flash_rom_inner(rom_data, flash_start, progress_cb, archive_path)
        except FlashAbortedError:
            self._set_state(FlashState.ABORTED)
            self._notify(progress_cb, "Flash aborted by user")
            raise
        except Exception as e:
            self._set_state(FlashState.ERROR)
            self._notify(progress_cb, f"Flash failed: {e}")
            raise
        finally:
            self._cleanup()

    def _flash_rom_inner(
        self,
        rom_data: bytes,
        flash_start_index: int,
        callback: Optional[ProgressCallback],
        archive_path: str | None = None,
    ) -> None:
        """
        Internal flash implementation shared by flash_rom and dynamic_flash.

        Args:
            rom_data: Validated ROM data
            flash_start_index: Byte offset where flashing begins
            callback: Progress callback
            archive_path: Optional file path to save as current ECU ROM archive
        """
        # --- Phase 1: Validate ROM (BEFORE touching ECU) ---
        self._notify(callback, "Validating ROM...", percent=0.0)

        if not validate_rom_size(rom_data):
            raise ROMValidationError(
                f"ROM must be exactly {ROM_SIZE} bytes, got {len(rom_data)}"
            )

        generation = detect_vehicle_generation(rom_data)
        logger.info(f"Vehicle generation: {generation}")

        # Make mutable copy for checksum correction
        rom_buf = bytearray(rom_data)
        corrections = correct_rom_checksums(rom_buf)
        if corrections:
            logger.info(f"Corrected {len(corrections)} checksums")
            for start, end, offset, old, new in corrections:
                logger.debug(
                    f"  0x{start:06X}-0x{end:06X}: " f"0x{old:08X} -> 0x{new:08X}"
                )

        # Verify checksums are now correct (defense-in-depth on a copy)
        verify_corrections = correct_rom_checksums(bytearray(rom_buf))
        if verify_corrections:
            raise ChecksumError(
                f"Checksum verification failed: {len(verify_corrections)} checksum(s) "
                f"still incorrect after correction"
            )

        # Validate flash boundaries (defense-in-depth, before ECU contact)
        if not (0 < flash_start_index < len(rom_buf)):
            raise FlashError(
                f"flash_start_index out of bounds: 0x{flash_start_index:06X} "
                f"(ROM size: 0x{len(rom_buf):06X})"
            )

        self._notify(callback, f"ROM valid ({generation})", percent=2.0)

        # --- Phase 2: Connect ---
        self._connect(callback)

        # --- Phase 3: Authenticate ---
        self._authenticate(callback)

        # Flash-only: romdrop's Read ROM (0x0040530D) skips this step
        self._notify(callback, "Checking flash counter...", percent=21.0)
        self._uds.check_flash_counter()

        # --- Phase 4: Prepare and transfer SBL ---
        self._set_state(FlashState.PREPARING_SBL)
        self._notify(callback, "Preparing SBL...", percent=22.0)

        sbl_data = get_sbl_data(flash_start_index, generation)
        if len(sbl_data) != SBL_SIZE:
            raise FlashError(
                f"SBL size mismatch: expected {SBL_SIZE}, got {len(sbl_data)}"
            )

        # Request Download
        self._notify(callback, "Requesting download...", percent=24.0)
        self._uds.request_download()

        # Transfer SBL
        self._set_state(FlashState.TRANSFERRING_SBL)
        self._notify(callback, "Transferring SBL...", percent=25.0)

        def sbl_progress(sent, total):
            pct = 25.0 + (sent / total) * 10.0  # 25% -> 35%
            self._notify(
                callback,
                f"SBL: {sent}/{total} bytes",
                percent=pct,
                bytes_sent=sent,
                bytes_total=total,
            )

        self._uds.transfer_data(
            sbl_data,
            block_size=BLOCK_SIZE,
            progress_callback=sbl_progress,
            abort_check=self._check_abort,
        )

        # --- Phase 5: Transfer ROM program data ---
        self._set_state(FlashState.TRANSFERRING_PROGRAM)

        # Extract the portion of ROM to flash
        program_data = bytes(rom_buf[flash_start_index:])
        total_program = len(program_data)

        self._notify(
            callback,
            f"Transferring ROM ({total_program} bytes from 0x{flash_start_index:06X})...",
            percent=35.0,
        )

        def program_progress(sent, total):
            pct = 35.0 + (sent / total) * 55.0  # 35% -> 90%
            elapsed = time.monotonic() - transfer_start
            speed = sent / elapsed if elapsed > 0 else 0
            self._notify(
                callback,
                f"ROM: {sent}/{total} bytes ({speed / 1024:.1f} KB/s)",
                percent=pct,
                bytes_sent=sent,
                bytes_total=total,
            )

        transfer_start = time.monotonic()

        self._uds.transfer_data(
            program_data,
            block_size=BLOCK_SIZE,
            progress_callback=program_progress,
            abort_check=self._check_abort,
        )

        transfer_elapsed = time.monotonic() - transfer_start
        logger.info(
            f"ROM transfer complete: {total_program} bytes in {transfer_elapsed:.1f}s "
            f"({total_program / transfer_elapsed / 1024:.1f} KB/s)"
        )

        # --- Phase 6: Finalize ---
        self._set_state(FlashState.FINALIZING)
        self._notify(callback, "Finalizing transfer...", percent=92.0)
        self._uds.request_transfer_exit()

        # --- Save archive (what's now on the ECU) ---
        if archive_path:
            try:
                self._save_archive(bytes(rom_buf), archive_path)
                self._notify(callback, "Archive updated", percent=94.0)
            except Exception as e:
                logger.warning(f"Archive save failed (non-fatal): {e}")

        # --- Phase 7: Reset ECU ---
        self._set_state(FlashState.RESETTING)
        self._notify(callback, "Resetting ECU...", percent=95.0)
        try:
            self._uds.ecu_reset()
        except NegativeResponseError as e:
            # Flash data is already committed. NRC during reset is non-fatal.
            logger.warning(
                "ECU reset returned NRC 0x%02X (%s) — flash data already committed",
                e.nrc,
                e.description,
            )
        except Exception as e:
            # Any other error during reset is also non-fatal post-commit
            logger.warning("ECU reset error (non-fatal, flash committed): %s", e)

        # --- Done ---
        self._set_state(FlashState.COMPLETE)
        self._notify(callback, "Flash complete!", percent=100.0)
        logger.info("Flash operation completed successfully")

    def read_rom(
        self,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> bytearray:
        """
        Read the complete 1MB ROM from the ECU.

        Args:
            progress_cb: Optional progress callback

        Returns:
            1MB ROM data as bytearray
        """
        if self.is_busy:
            raise FlashError("Operation already in progress")

        self._abort_event.clear()
        self._state = FlashState.IDLE

        try:
            self._connect(progress_cb)
            self._authenticate(progress_cb)

            # Read ROM in blocks (no SBL needed for read — only for flash)
            self._set_state(FlashState.READING)
            self._notify(progress_cb, "Reading ROM...", percent=20.0)

            rom = bytearray(ROM_SIZE)
            block_size = BLOCK_SIZE
            offset = 0

            while offset < ROM_SIZE:
                if self._check_abort():
                    raise FlashAbortedError("ROM read aborted by user")

                read_size = min(block_size, ROM_SIZE - offset)
                data = self._uds.read_memory_by_address(offset, read_size)
                rom[offset : offset + len(data)] = data
                offset += read_size

                pct = 20.0 + (offset / ROM_SIZE) * 75.0
                if progress_cb:
                    self._notify(
                        progress_cb,
                        f"Reading: {offset}/{ROM_SIZE} bytes",
                        percent=pct,
                        bytes_sent=offset,
                        bytes_total=ROM_SIZE,
                    )

            # Read ROM ID
            rom_id = self._uds.read_rom_id()
            logger.info(f"ROM read complete, ROM ID: {rom_id}")

            self._set_state(FlashState.COMPLETE)
            self._notify(
                progress_cb, f"ROM read complete (ID: {rom_id})", percent=100.0
            )

            return rom

        except FlashAbortedError:
            self._set_state(FlashState.ABORTED)
            raise
        except Exception as e:
            self._set_state(FlashState.ERROR)
            self._notify(progress_cb, f"ROM read failed: {e}")
            raise
        finally:
            self._cleanup()

    def read_dtcs(self, uds=None) -> list:
        """
        Read DTCs from the ECU.

        Args:
            uds: Optional UDSConnection from an active session.
                 If None, opens a temporary connection.

        Returns:
            List of DTC objects
        """
        try:
            if uds:
                dtcs = uds.read_dtc_status()
            else:
                from .j2534 import J2534Device, setup_isotp_flow_control
                from .protocol import UDSConnection
                from .constants import ISO15765_BS, ISO15765_STMIN

                with J2534Device(self._dll_path) as device:
                    channel_id = device.connect(
                        J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
                    )
                    device.set_config(channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})
                    setup_isotp_flow_control(device, channel_id)

                    uds = UDSConnection(device, channel_id)
                    uds.tester_present()
                    dtcs = uds.read_dtc_status()

            seen = set()
            unique_dtcs = []
            for d in dtcs:
                if d.code not in seen:
                    seen.add(d.code)
                    unique_dtcs.append(d)
            logger.info("Read %d DTCs (%d unique)", len(dtcs), len(unique_dtcs))
            for dtc in unique_dtcs:
                logger.info("  %s: %s", dtc.formatted, dtc.description)
            return dtcs
        except ECUError:
            raise
        except Exception as e:
            raise FlashError(f"Failed to read DTCs: {e}") from e

    def clear_dtcs(self, uds=None) -> None:
        """Clear all DTCs from the ECU.

        Args:
            uds: Optional UDSConnection from an active session.
        """
        try:
            if uds:
                uds.clear_dtc()
            else:
                from .j2534 import J2534Device, setup_isotp_flow_control
                from .protocol import UDSConnection
                from .constants import ISO15765_BS, ISO15765_STMIN

                with J2534Device(self._dll_path) as device:
                    channel_id = device.connect(
                        J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
                    )
                    device.set_config(channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})
                    setup_isotp_flow_control(device, channel_id)

                    uds_conn = UDSConnection(device, channel_id)
                    uds_conn.tester_present()
                    uds_conn.clear_dtc()

            logger.info("DTCs cleared successfully")
        except ECUError:
            raise
        except Exception as e:
            raise FlashError(f"Failed to clear DTCs: {e}") from e

    def read_vin_block(self, uds=None) -> bytes:
        """Read VIN block from ECU and return raw data.

        Args:
            uds: Optional UDSConnection from an active session.
        """
        try:
            if uds:
                return uds.read_vin_block()

            from .j2534 import J2534Device, setup_isotp_flow_control
            from .protocol import UDSConnection
            from .constants import ISO15765_BS, ISO15765_STMIN

            with J2534Device(self._dll_path) as device:
                channel_id = device.connect(J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE)
                device.set_config(channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})
                setup_isotp_flow_control(device, channel_id)

                uds_conn = UDSConnection(device, channel_id)
                uds_conn.tester_present()
                return uds_conn.read_vin_block()
        except ECUError:
            raise
        except Exception as e:
            raise FlashError(f"Failed to read VIN block: {e}") from e

    def scan_ram(self, uds=None, progress_cb=None) -> bytearray:
        """Scan ECU RAM (0x0000-0xBFFF) and return contents.

        Uses the borrowed session (via use_session()) when available,
        otherwise opens a new J2534 connection.

        Args:
            uds: Optional UDSConnection from an active session
                 (legacy direct-call path; the UI uses use_session()).
            progress_cb: Callback receiving (current_block, total_blocks).
        """
        if self.is_busy:
            raise FlashError("Operation already in progress")

        self._abort_event.clear()
        self._state = FlashState.IDLE

        try:
            self._connect(progress_cb)
            self._authenticate(progress_cb)

            self._set_state(FlashState.SCANNING_RAM)

            base_address = 0xFFFF0000
            total_pages = 192  # 0xC0 pages: 0x00 through 0xBF
            page_size = 0x100
            read_size = page_size
            ram = bytearray(total_pages * page_size)

            for i in range(total_pages):
                if self._check_abort():
                    raise FlashAbortedError("RAM scan aborted by user")

                address = base_address + i * page_size
                data = self._uds.read_memory_by_address(address, read_size)
                offset = i * page_size
                ram[offset : offset + page_size] = data[:page_size]
                pct = ((i + 1) / total_pages) * 100.0
                self._notify(
                    progress_cb,
                    f"Scanning RAM: page {i + 1}/{total_pages}",
                    percent=pct,
                    bytes_sent=(i + 1) * page_size,
                    bytes_total=total_pages * page_size,
                )

            self._set_state(FlashState.COMPLETE)
            return ram
        except FlashAbortedError:
            self._set_state(FlashState.ABORTED)
            raise
        except ECUError:
            self._set_state(FlashState.ERROR)
            raise
        except Exception as e:
            self._set_state(FlashState.ERROR)
            raise FlashError(f"Failed to scan RAM: {e}") from e
        finally:
            self._cleanup()

    def sniff_can(self, duration_seconds: int = 20, progress_cb=None) -> bytes:
        """
        Passively capture CAN bus traffic.

        Listens for messages with 1s timeout, collects data into a buffer.
        Stops after duration_seconds of idle time (no messages received).

        Returns:
            Raw captured CAN data
        """
        from .j2534 import J2534Device
        from .constants import ISO15765_BS, ISO15765_STMIN

        try:
            with J2534Device(self._dll_path) as device:
                channel_id = device.connect(J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE)
                device.set_config(channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})

                buffer = bytearray()
                idle_count = 0
                max_idle = duration_seconds

                logger.info("CAN sniffing started")

                while idle_count < max_idle:
                    try:
                        msgs = device.read_msgs(channel_id, 1, 1000)
                    except Exception:
                        idle_count += 1
                        continue

                    if not msgs:
                        idle_count += 1
                        continue

                    idle_count = 0
                    for msg in msgs:
                        data = bytes(msg.Data[: msg.DataSize])
                        buffer.extend(data)

                    if progress_cb:
                        progress_cb(len(buffer))

                logger.info(f"CAN sniff complete: {len(buffer)} bytes captured")
                return bytes(buffer)
        except ECUError:
            raise
        except Exception as e:
            raise FlashError(f"CAN sniff failed: {e}") from e
