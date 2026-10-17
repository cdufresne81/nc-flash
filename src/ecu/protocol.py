"""
UDS Diagnostic Protocol over ISO-TP (ISO 15765)

Implements UDS service requests over a J2534 PassThru connection.
Handles response validation, NRC 0x78 (response pending) retries,
and provides typed methods for each diagnostic service used in flashing.
"""

import logging
import struct
import time
from typing import Callable, Optional

from .constants import (
    SID_DIAGNOSTIC_SESSION,
    SID_ECU_RESET,
    SID_CLEAR_DTC,
    SID_READ_DTC_STATUS,
    SID_READ_DTC_COUNT,
    SID_READ_MEM_BY_ADDR,
    SID_SECURITY_ACCESS,
    SID_REQUEST_DOWNLOAD,
    SID_TRANSFER_DATA,
    SID_TRANSFER_EXIT,
    SID_TESTER_PRESENT,
    SID_ROUTINE_CONTROL,
    DIAG_SESSION_PROGRAMMING,
    RESET_HARD,
    SECURITY_REQUEST_SEED,
    SECURITY_SEND_KEY,
    TESTER_PRESENT_SUB,
    NRC_CONDITIONS_NOT_CORRECT,
    NRC_RESPONSE_PENDING,
    DOWNLOAD_ADDR,
    DOWNLOAD_SIZE,
    BLOCK_SIZE,
    FLASH_COUNTER_CMD,
    TIMEOUT_DEFAULT,
    TIMEOUT_SECURITY,
    TIMEOUT_TRANSFER,
    TIMEOUT_READ,
    TIMEOUT_RESET,
    TIMEOUT_RESPONSE_PENDING_MAX,
    TIMEOUT_PROBE,
)
from .exceptions import (
    J2534Error,
    UDSError,
    NegativeResponseError,
    SecurityAccessDenied,
    TransferError,
    UDSTimeoutError,
)
from .dtc import get_nrc_description, format_dtc, get_dtc_description

logger = logging.getLogger(__name__)


class DTC:
    """Represents a single Diagnostic Trouble Code."""

    def __init__(self, code: int, status: int):
        self.code = code
        self.status = status
        self.formatted = format_dtc(code)
        self.description = get_dtc_description(code)

    def __repr__(self):
        return f"DTC({self.formatted}: {self.description})"


class UDSConnection:
    """
    UDS diagnostic connection over a J2534 ISO-TP channel.

    Provides typed methods for each UDS service used in the flash workflow.
    All methods validate responses and raise appropriate exceptions.
    """

    def __init__(self, transport):
        """
        Args:
            transport: An :class:`~src.ecu.transport.EcuTransport` that
                exchanges complete UDS payloads (SID + data) with the ECU.
                The transport owns all link framing (ISO-TP, CAN IDs); this
                connection works purely in UDS bytes.
        """
        self._transport = transport

    def flush(self) -> None:
        """Discard buffered/in-flight RX data on the transport (read-retry use).

        Delegates to :meth:`EcuTransport.flush` — a no-op on reliable links, a
        stale-frame drain on lossy ones. Safe to call between idempotent reads.
        """
        self._transport.flush()

    def send_request(
        self,
        service_id: int,
        data: bytes = b"",
        timeout: int = TIMEOUT_DEFAULT,
        pending_max: Optional[int] = None,
        quiet_nrcs: Optional[set[int]] = None,
    ) -> bytes:
        """
        Send a UDS request and return the positive response payload.

        Handles NRC 0x78 (response pending) by retrying reads until
        a final response arrives or the cumulative timeout expires.

        Args:
            service_id: UDS service ID (e.g., 0x10, 0x27)
            data: Additional request data after the SID
            timeout: Per-read timeout in milliseconds
            pending_max: Cumulative budget (ms) across response-pending retries
                before giving up. Defaults to ``TIMEOUT_RESPONSE_PENDING_MAX``
                (generous, to ride out a slow flash erase). The idempotent read
                path passes a much smaller value so a *dropped* block response
                fails fast and can be retried, instead of stalling the full
                cumulative budget on every lost block.
            quiet_nrcs: NRCs the caller expects and handles gracefully. When the
                ECU returns one of these, the generic ``UDS NRC:`` record is
                logged at DEBUG instead of WARNING so a benign/expected refusal
                does not alarm the user (the ``NegativeResponseError`` is still
                raised so the caller's handling is unchanged). Default ``None``
                keeps every NRC at WARNING.

        Returns:
            Response payload bytes (after the positive response SID)

        Raises:
            NegativeResponseError: ECU returned a negative response
            TimeoutError: No response within allowed time
            UDSError: Other protocol errors
        """
        budget = (
            pending_max if pending_max is not None else TIMEOUT_RESPONSE_PENDING_MAX
        )
        # Build and send request
        request_data = bytes([service_id]) + data

        logger.debug(
            f"UDS TX: SID=0x{service_id:02X} data={data.hex() if data else '(empty)'}"
        )
        self._transport.send_message(request_data, timeout)

        # Read response with NRC 0x78 retry loop
        positive_sid = service_id + 0x40
        elapsed = 0
        start = time.monotonic()

        while elapsed < budget:
            try:
                resp_data = self._transport.receive_message(timeout)
            except J2534Error:
                raise  # Bridge/device errors should propagate as-is
            except Exception as e:
                raise UDSTimeoutError(
                    f"No response from ECU for SID 0x{service_id:02X}: {e}"
                )

            # No message / no UDS payload this read — keep waiting until the
            # cumulative response-pending budget is exhausted.
            if not resp_data:
                elapsed = int((time.monotonic() - start) * 1000)
                if elapsed >= budget:
                    raise UDSTimeoutError(
                        f"Timed out waiting for response to SID 0x{service_id:02X}"
                    )
                continue

            # Check for negative response (0x7F)
            if resp_data[0] == 0x7F:
                if len(resp_data) < 3:
                    logger.warning(
                        "UDS: malformed negative response for SID 0x%02X "
                        "(expected >= 3 bytes, got %d)",
                        service_id,
                        len(resp_data),
                    )
                    raise UDSError(
                        f"Malformed negative response for SID 0x{service_id:02X}: "
                        f"expected >= 3 bytes, got {len(resp_data)}"
                    )
                nrc = resp_data[2]
                if nrc == NRC_RESPONSE_PENDING:
                    logger.debug(
                        f"UDS: NRC 0x78 (response pending) for SID 0x{service_id:02X}"
                    )
                    elapsed = int((time.monotonic() - start) * 1000)
                    continue
                desc = get_nrc_description(nrc)
                # Expected/handled NRCs (per caller's quiet_nrcs) are demoted to
                # DEBUG so a benign refusal does not raise a scary WARNING in the
                # user-facing Activity Log; the exception is still raised below.
                nrc_level = (
                    logging.DEBUG
                    if quiet_nrcs is not None and nrc in quiet_nrcs
                    else logging.WARNING
                )
                logger.log(
                    nrc_level,
                    f"UDS NRC: SID=0x{service_id:02X} NRC=0x{nrc:02X} ({desc})",
                )
                raise NegativeResponseError(nrc, desc, service_id=resp_data[1])

            # Check for positive response
            if resp_data[0] == positive_sid:
                logger.debug(
                    f"UDS RX: positive SID=0x{positive_sid:02X} "
                    f"len={len(resp_data) - 1}"
                )
                return resp_data[1:]

            # Unexpected response byte. On the no-reboot coexist port these are
            # usually interloping datalogger OBD replies (0x41 = Mode-01 + 0x40)
            # in flight when the host took the bus — benign noise, not a fault —
            # so log at DEBUG (like quiet NRCs) rather than spamming WARNING (G5).
            logger.debug(
                f"UDS: unexpected response byte 0x{resp_data[0]:02X} "
                f"for SID 0x{service_id:02X}"
            )
            elapsed = int((time.monotonic() - start) * 1000)

        raise UDSTimeoutError(
            f"Timed out after {budget}ms " f"waiting for SID 0x{service_id:02X}"
        )

    # --- Diagnostic Services ---

    def tester_present(self, timeout_ms: Optional[int] = None) -> None:
        """Send Tester Present to keep the session alive.

        Args:
            timeout_ms: Total time to wait for the reply. ``None`` (keep-alives
                during an operation) keeps the normal per-read timeout and the
                long response-pending budget. The "is the ECU there?" probes pass
                ``TIMEOUT_PROBE`` so a silent ECU (ignition off) fails in seconds.
        """
        if timeout_ms is None:
            self.send_request(SID_TESTER_PRESENT, bytes([TESTER_PRESENT_SUB]))
        else:
            try:
                self.send_request(
                    SID_TESTER_PRESENT,
                    bytes([TESTER_PRESENT_SUB]),
                    timeout=timeout_ms,
                    pending_max=timeout_ms,
                )
            except UDSTimeoutError as exc:
                if exc.__context__ is not None:
                    raise  # a corrupt frame, not silence: the hint would mislead
                raise UDSTimeoutError(
                    f"No reply from the ECU within {timeout_ms / 1000:.0f} s. Check "
                    f"the ignition is ON and the adapter is connected. ({exc})"
                ) from exc
        logger.debug("ECU >> Tester Present acknowledged")

    # --- OBD-II Live Data ---

    def read_obd_pid(self, pid: int, timeout_ms: Optional[int] = None) -> bytes:
        """
        Read a standard OBD-II PID via Service 0x01.

        Args:
            pid: OBD-II PID number (e.g., 0x0C for RPM, 0x42 for voltage)
            timeout_ms: Total time to wait for the reply. ``None`` keeps the
                normal per-read timeout and response-pending budget.

        Returns:
            Raw PID data bytes (after the PID echo byte)
        """
        from .constants import SID_OBD_CURRENT_DATA

        budget = {}
        if timeout_ms is not None:
            budget = {"timeout": timeout_ms, "pending_max": timeout_ms}
        # On a post-op reconnect the ECU answers OBD reads with NRC 0x22
        # (conditions not correct, still in a programming session). Expected,
        # so keep it quiet; the callers decide what it means.
        response = self.send_request(
            SID_OBD_CURRENT_DATA,
            bytes([pid]),
            quiet_nrcs={NRC_CONDITIONS_NOT_CORRECT},
            **budget,
        )
        # Response: [pid_echo, data...] (send_request strips the 0x41 positive SID)
        if not response or response[0] != pid:
            from .exceptions import UDSError

            raise UDSError(f"OBD PID 0x{pid:02X}: unexpected response format")
        return response[1:]

    def _read_pid_word(
        self, pid: int, name: str, strict: bool, wait_session_exit: bool = False
    ) -> Optional[int]:
        """Read a 2-byte OBD PID as an unsigned int, or ``None`` if unavailable.

        "Unavailable" means the ECU ANSWERED and declined this OBD request (a
        negative response echoing SID 0x01): the PID is unsupported, or the ECU
        is in a state that refuses OBD (NRC 0x11 from the bootloader after a
        failed flash). That must never block a flash, or a recovery re-flash
        would be impossible.

        Anything else (no reply, a corrupt or malformed reply, a transport error,
        a refusal of some other request) says nothing about the car. With
        ``strict`` it raises, so a flash guard stops instead of silently
        skipping its check. Without it (the live dashboard) it returns ``None``.

        With ``wait_session_exit`` (the flash guards; never the UI-thread
        dashboard), NRC 0x22 (the ECU still in a programming session after a
        ROM read) gets one wait-and-retry so the check really runs. A second
        0x22 raises with ``strict``: the check could not run, so the flash must
        not silently skip it.
        """
        from .constants import (
            PROGRAMMING_SESSION_EXIT_WAIT_MS,
            SID_OBD_CURRENT_DATA,
        )

        for attempt in (1, 2):
            try:
                data = self.read_obd_pid(pid, timeout_ms=TIMEOUT_PROBE)
                if len(data) < 2:
                    raise UDSError(
                        f"OBD PID 0x{pid:02X}: short reply ({len(data)} data bytes)"
                    )
                return (data[0] << 8) | data[1]
            except NegativeResponseError as exc:
                if exc.service_id not in (None, SID_OBD_CURRENT_DATA):
                    # A refusal of some OTHER request (a late reply, another
                    # tester) says nothing about this PID: not a decline.
                    if strict:
                        raise UDSError(
                            f"OBD PID 0x{pid:02X}: got a refusal meant for SID "
                            f"0x{exc.service_id:02X} ({exc})"
                        ) from exc
                    logger.debug(f"{name}: foreign-SID refusal ignored: {exc}")
                    return None
                if wait_session_exit and exc.nrc == NRC_CONDITIONS_NOT_CORRECT:
                    if attempt == 2:
                        if strict:
                            raise UDSError(
                                f"OBD PID 0x{pid:02X}: the ECU is still in a "
                                "programming session after "
                                f"{PROGRAMMING_SESSION_EXIT_WAIT_MS / 1000.0:.1f} s "
                                f"({exc})"
                            ) from exc
                        return None
                    logger.info(
                        "%s: the ECU is still in a programming session (NRC 0x22); "
                        "waiting %.1f s for it to return to normal, then reading "
                        "again",
                        name,
                        PROGRAMMING_SESSION_EXIT_WAIT_MS / 1000.0,
                    )
                    time.sleep(PROGRAMMING_SESSION_EXIT_WAIT_MS / 1000.0)
                    continue
                logger.debug(
                    f"{name} declined by the ECU (treated as unsupported): {exc}"
                )
                return None
            except Exception as exc:
                if strict:
                    raise
                logger.debug(f"{name} failed (treated as unavailable): {exc}")
                return None
        return None  # not reached: attempt 2 always returns or raises

    def read_battery_voltage(
        self, strict: bool = False, wait_session_exit: bool = False
    ) -> float | None:
        """Read control module voltage via OBD-II PID 0x42.

        Returns voltage in volts, or None if the ECU declined the request. With
        ``strict=False`` a failed read also returns None; with ``strict=True``
        it raises. ``wait_session_exit`` waits out a programming session (see
        :meth:`_read_pid_word`).
        """
        from .constants import OBD_PID_CONTROL_MODULE_VOLTAGE

        raw = self._read_pid_word(
            OBD_PID_CONTROL_MODULE_VOLTAGE,
            "read_battery_voltage",
            strict,
            wait_session_exit,
        )
        return None if raw is None else raw / 1000.0

    def read_engine_rpm(
        self, strict: bool = False, wait_session_exit: bool = False
    ) -> float | None:
        """Read engine RPM via OBD-II PID 0x0C.

        Returns RPM, or None if the ECU declined the request. With
        ``strict=False`` a failed read also returns None; with ``strict=True``
        it raises. ``wait_session_exit`` waits out a programming session (see
        :meth:`_read_pid_word`).
        """
        from .constants import OBD_PID_ENGINE_RPM

        raw = self._read_pid_word(
            OBD_PID_ENGINE_RPM, "read_engine_rpm", strict, wait_session_exit
        )
        return None if raw is None else raw / 4.0

    # --- Diagnostic Sessions ---

    def diagnostic_session(self, sub_function: int = DIAG_SESSION_PROGRAMMING) -> None:
        """
        Enter a diagnostic session.

        Args:
            sub_function: Session type (default: 0x85 programming session)
        """
        self.send_request(SID_DIAGNOSTIC_SESSION, bytes([sub_function]))
        logger.info(f"ECU >> Diagnostic session 0x{sub_function:02X} active")

    def ecu_reset(self, reset_type: int = RESET_HARD) -> None:
        """
        Request ECU reset.

        Args:
            reset_type: Reset type (default: 0x01 hard reset)
        """
        try:
            self.send_request(SID_ECU_RESET, bytes([reset_type]), timeout=TIMEOUT_RESET)
        except UDSTimeoutError:
            # ECU may reset before sending response - this is expected
            logger.info(
                "Tool >> ECU reset requested (no response - ECU likely resetting)"
            )
            return
        logger.info(f"ECU >> Reset type 0x{reset_type:02X} acknowledged")

    def security_access_request_seed(self) -> bytes:
        """
        Request security access seed from ECU.

        Returns:
            8-byte seed value
        """
        response = self.send_request(
            SID_SECURITY_ACCESS,
            bytes([SECURITY_REQUEST_SEED]),
            timeout=TIMEOUT_SECURITY,
        )
        if len(response) < 2:
            raise SecurityAccessDenied("Seed response too short")

        # Response: sub_function(1) + seed(N)
        seed = response[1:]
        logger.info(
            "ECU >> Security seed: %d bytes [%s]",
            len(seed),
            seed.hex(),
        )
        return seed

    def security_access_send_key(self, key: bytes) -> None:
        """
        Send computed security key to ECU.

        Args:
            key: 3-byte computed key

        Raises:
            SecurityAccessDenied: If key is rejected
        """
        try:
            self.send_request(
                SID_SECURITY_ACCESS,
                bytes([SECURITY_SEND_KEY]) + key,
                timeout=TIMEOUT_SECURITY,
            )
        except NegativeResponseError as e:
            if e.nrc in (0x35, 0x36, 0x33):
                raise SecurityAccessDenied(
                    f"Security key rejected: {e.description}"
                ) from e
            raise
        logger.info("ECU >> Security access granted")

    def check_flash_counter(self) -> bytes:
        """
        Check the ECU flash counter via Routine Control.

        Returns:
            Raw response data from the routine
        """
        response = self.send_request(SID_ROUTINE_CONTROL, FLASH_COUNTER_CMD[1:])
        logger.info(f"ECU >> Flash counter: {response.hex()}")
        return response

    def request_download(
        self, address: int = DOWNLOAD_ADDR, size: int = DOWNLOAD_SIZE
    ) -> None:
        """
        Request Download — tell ECU to prepare for data reception.

        Mazda NC uses KWP2000-style RequestDownload: raw address(4) + size(4),
        no dataFormatIdentifier or addressAndLengthFormatIdentifier byte.
        Verified against romdrop disassembly at 0x0040472F.

        Args:
            address: Download start address (default: 0x8000)
            size: Total download size (default: 0xFF800)
        """
        data = address.to_bytes(4, "big") + size.to_bytes(4, "big")
        self.send_request(SID_REQUEST_DOWNLOAD, data, timeout=TIMEOUT_TRANSFER)
        logger.info(
            f"ECU >> Download request accepted: addr=0x{address:08X} size=0x{size:06X}"
        )

    def transfer_data(
        self,
        data: bytes,
        block_size: int = BLOCK_SIZE,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        """
        Transfer data to ECU in blocks.

        Args:
            data: Raw data to transfer
            block_size: Transfer block size (default: 1024 bytes)
            progress_callback: Called with (bytes_sent, total_bytes)
            abort_check: Called between blocks; if returns True, abort

        Raises:
            TransferError: If a block transfer fails
            FlashAbortedError: If abort_check returns True
        """
        from .exceptions import FlashAbortedError

        total = len(data)
        sent = 0
        block_num = 0

        logger.info(
            f"Tool >> Starting data transfer: {total} bytes in {block_size}-byte blocks"
        )

        while sent < total:
            # Check abort between blocks
            if abort_check and abort_check():
                raise FlashAbortedError("Transfer aborted by user")

            chunk_end = min(sent + block_size, total)
            chunk = data[sent:chunk_end]
            block_num += 1

            # SAFETY: Mazda NC KWP2000-style TransferData: SID(0x36) + raw data only.
            # No blockSequenceCounter byte — verified against romdrop
            # disassembly at 0x004047A6 which sends (param_3 + 1) bytes:
            # [0x36][data...] with no counter prefix.
            # DO NOT add block counter validation — the ECU does not echo one.
            # Doing so would break the flash and could brick the ECU.
            try:
                self.send_request(SID_TRANSFER_DATA, chunk, timeout=TIMEOUT_TRANSFER)
            except NegativeResponseError as e:
                raise TransferError(
                    f"Transfer failed at block {block_num} "
                    f"(offset 0x{sent:06X}): {e.description}"
                ) from e

            sent = chunk_end

            if progress_callback:
                progress_callback(sent, total)

        # Post-condition: all bytes transferred
        if sent != total:
            raise TransferError(f"Transfer incomplete: sent {sent} of {total} bytes")

        logger.info(f"Tool >> Transfer complete: {sent} bytes in {block_num} blocks")

    def request_transfer_exit(self) -> None:
        """Signal end of data transfer to ECU."""
        self.send_request(SID_TRANSFER_EXIT, timeout=TIMEOUT_TRANSFER)
        logger.info("ECU >> Transfer exit acknowledged")

    # --- Memory Read ---

    def read_memory_by_address(
        self,
        address: int,
        size: int,
        timeout: int = TIMEOUT_READ,
        pending_max: Optional[int] = None,
    ) -> bytes:
        """
        Read memory from ECU.

        Mazda NC uses KWP2000-style ReadMemoryByAddress: raw address(4) + size(2),
        no addressAndLengthFormatIdentifier byte.
        Verified against romdrop disassembly at 0x004045B3.

        Args:
            address: 4-byte memory address
            size: 2-byte read size (max ~0x400)
            timeout: Per-read timeout in milliseconds.
            pending_max: Cumulative response-pending budget (ms). Reads over a
                lossy link pass a small value so a dropped block fails fast and
                the caller can retry (see :meth:`send_request`).

        Returns:
            Raw memory data
        """
        data = address.to_bytes(4, "big") + size.to_bytes(2, "big")
        response = self.send_request(
            SID_READ_MEM_BY_ADDR, data, timeout=timeout, pending_max=pending_max
        )
        return response

    def read_rom_id(self) -> str:
        """
        Read ROM ID string from ECU.

        Uses ReadDataByIdentifier (SID 0x22, sub=0xE6, record=0x11).

        Returns:
            ROM ID string
        """
        response = self.send_request(
            SID_READ_DTC_COUNT,  # 0x22 is overloaded - used for ReadDataByIdentifier
            bytes([0xE6, 0x11]),
        )
        # Response starts with echo of sub/record (0xE6, 0x11), then ROM ID
        if response and len(response) > 2:
            return response[2:].rstrip(b"\x00").decode("ascii", errors="replace")
        return ""

    # --- DTC Operations ---

    def read_dtc_count(self) -> int:
        """
        Read the number of stored DTCs.

        Returns:
            Number of DTCs
        """
        try:
            response = self.send_request(
                SID_READ_DTC_COUNT,
                bytes([0x02, 0x00]),
                quiet_nrcs={NRC_CONDITIONS_NOT_CORRECT},
            )
        except NegativeResponseError as e:
            if e.nrc == NRC_CONDITIONS_NOT_CORRECT:
                logger.info(
                    "ECU >> ReadDTCCount: conditions not correct "
                    "(NRC 0x22) — returning 0"
                )
                return 0
            raise
        if len(response) >= 3:
            return response[2]
        return 0

    def read_dtc_status(self) -> list[DTC]:
        """
        Read all stored DTCs with their status.

        Returns:
            List of DTC objects
        """
        count = self.read_dtc_count()
        if count == 0:
            return []

        # ReadDTCByStatus: status mask = 0x00FF00
        try:
            response = self.send_request(
                SID_READ_DTC_STATUS,
                bytes([0x00, 0xFF, 0x00]),
                quiet_nrcs={NRC_CONDITIONS_NOT_CORRECT},
            )
        except NegativeResponseError as e:
            if e.nrc == NRC_CONDITIONS_NOT_CORRECT:
                logger.info(
                    "ECU >> ReadDTCByStatus: conditions not correct "
                    "(NRC 0x22) — returning empty DTC list"
                )
                return []
            raise

        dtcs = []
        # Response format: [countOfDTC, {code_hi, code_lo, status}...]
        # Skip the first byte (KWP2000 countOfDTC header)
        offset = 1
        while offset + 2 < len(response):
            code = (response[offset] << 8) | response[offset + 1]
            status = response[offset + 2] if offset + 2 < len(response) else 0
            if code != 0:
                dtcs.append(DTC(code, status))
            offset += 3

        unique_count = len({d.code for d in dtcs})
        logger.info(f"ECU >> Read {len(dtcs)} DTCs ({unique_count} unique)")
        return dtcs

    def clear_dtc(self) -> None:
        """Clear all stored DTCs."""
        self.send_request(SID_CLEAR_DTC, bytes([0xFF, 0x00]))
        logger.info("ECU >> DTCs cleared")

    def read_vin_block(self) -> bytes:
        """
        Read VIN block from ECU (SID 0x21, sub=0x00).

        Returns:
            Raw VIN block data (variable length, typically includes
            6 header bytes that romdrop strips)
        """
        response = self.send_request(0x21, bytes([0x00]))
        # Strip first 6 bytes (header) like romdrop does
        if len(response) > 6:
            return response[6:]
        return response

    def scan_ram(self, progress_callback=None) -> bytearray:
        """
        Scan ECU RAM at 0xFFFF0000-0xFFFFBFFF.

        Reads 192 pages (0x00-0xBF) stepping by 0x100.  Each UDS request
        fetches 0x1F0 bytes (matching romdrop), but only the first 0x100
        bytes per page are kept, giving a clean 48 KB dump.

        Based on romdrop's uds_ScanRAM at 0x00404AE2.

        Returns:
            RAM contents as bytearray (49152 bytes)
        """
        base_address = 0xFFFF0000
        total_pages = 192  # 0xC0 pages: 0x00 through 0xBF
        page_size = 0x100
        read_size = page_size
        ram = bytearray(total_pages * page_size)

        for i in range(total_pages):
            address = base_address + i * page_size
            data = self.read_memory_by_address(address, read_size)
            offset = i * page_size
            ram[offset : offset + page_size] = data[:page_size]
            if progress_callback:
                progress_callback(i + 1, total_pages)

        return ram
