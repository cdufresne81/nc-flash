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
    CAN_REQUEST_ID,
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

    def __init__(self, j2534_device, channel_id: int):
        """
        Args:
            j2534_device: An open J2534Device instance
            channel_id: Connected channel ID from j2534_device.connect()
        """
        self._device = j2534_device
        self._channel_id = channel_id

    def send_request(
        self,
        service_id: int,
        data: bytes = b"",
        timeout: int = TIMEOUT_DEFAULT,
    ) -> bytes:
        """
        Send a UDS request and return the positive response payload.

        Handles NRC 0x78 (response pending) by retrying reads until
        a final response arrives or the cumulative timeout expires.

        Args:
            service_id: UDS service ID (e.g., 0x10, 0x27)
            data: Additional request data after the SID
            timeout: Per-read timeout in milliseconds

        Returns:
            Response payload bytes (after the positive response SID)

        Raises:
            NegativeResponseError: ECU returned a negative response
            TimeoutError: No response within allowed time
            UDSError: Other protocol errors
        """
        from .j2534 import build_isotp_msg

        # Build and send request
        request_data = bytes([service_id]) + data
        msg = build_isotp_msg(request_data)

        logger.debug(
            f"UDS TX: SID=0x{service_id:02X} data={data.hex() if data else '(empty)'}"
        )
        self._device.write_msgs(self._channel_id, [msg], timeout)

        # Read response with NRC 0x78 retry loop
        positive_sid = service_id + 0x40
        elapsed = 0
        start = time.monotonic()

        while elapsed < TIMEOUT_RESPONSE_PENDING_MAX:
            try:
                msgs = self._device.read_msgs(self._channel_id, 1, timeout)
            except J2534Error:
                raise  # Bridge/device errors should propagate as-is
            except Exception as e:
                raise UDSTimeoutError(
                    f"No response from ECU for SID 0x{service_id:02X}: {e}"
                )

            if not msgs:
                elapsed = int((time.monotonic() - start) * 1000)
                if elapsed >= TIMEOUT_RESPONSE_PENDING_MAX:
                    raise UDSTimeoutError(
                        f"Timed out waiting for response to SID 0x{service_id:02X}"
                    )
                continue

            resp_msg = msgs[0]
            # Extract payload (skip 4-byte CAN ID prefix)
            if resp_msg.DataSize <= 4:
                continue

            resp_data = bytes(resp_msg.Data[4 : resp_msg.DataSize])

            if not resp_data:
                continue

            # Check for negative response (0x7F)
            if resp_data[0] == 0x7F and len(resp_data) >= 3:
                nrc = resp_data[2]
                if nrc == NRC_RESPONSE_PENDING:
                    logger.debug(
                        f"UDS: NRC 0x78 (response pending) for SID 0x{service_id:02X}"
                    )
                    elapsed = int((time.monotonic() - start) * 1000)
                    continue
                desc = get_nrc_description(nrc)
                logger.warning(
                    f"UDS NRC: SID=0x{service_id:02X} NRC=0x{nrc:02X} ({desc})"
                )
                raise NegativeResponseError(nrc, desc)

            # Check for positive response
            if resp_data[0] == positive_sid:
                logger.debug(
                    f"UDS RX: positive SID=0x{positive_sid:02X} "
                    f"len={len(resp_data) - 1}"
                )
                return resp_data[1:]

            # Unexpected response byte
            logger.warning(
                f"UDS: unexpected response byte 0x{resp_data[0]:02X} "
                f"for SID 0x{service_id:02X}"
            )
            elapsed = int((time.monotonic() - start) * 1000)

        raise UDSTimeoutError(
            f"Timed out after {TIMEOUT_RESPONSE_PENDING_MAX}ms "
            f"waiting for SID 0x{service_id:02X}"
        )

    # --- Diagnostic Services ---

    def tester_present(self) -> None:
        """Send Tester Present to keep the session alive."""
        self.send_request(SID_TESTER_PRESENT, bytes([TESTER_PRESENT_SUB]))
        logger.info("ECU >> Tester Present acknowledged")

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
            logger.info("Tool >> ECU reset requested (no response - ECU likely resetting)")
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

            # Mazda NC KWP2000-style TransferData: SID(0x36) + raw data only.
            # No blockSequenceCounter byte — verified against romdrop
            # disassembly at 0x004047A6 which sends (param_3 + 1) bytes:
            # [0x36][data...] with no counter prefix.
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

        logger.info(f"Tool >> Transfer complete: {sent} bytes in {block_num} blocks")

    def request_transfer_exit(self) -> None:
        """Signal end of data transfer to ECU."""
        self.send_request(SID_TRANSFER_EXIT, timeout=TIMEOUT_TRANSFER)
        logger.info("ECU >> Transfer exit acknowledged")

    # --- Memory Read ---

    def read_memory_by_address(self, address: int, size: int) -> bytes:
        """
        Read memory from ECU.

        Mazda NC uses KWP2000-style ReadMemoryByAddress: raw address(4) + size(2),
        no addressAndLengthFormatIdentifier byte.
        Verified against romdrop disassembly at 0x004045B3.

        Args:
            address: 4-byte memory address
            size: 2-byte read size (max ~0x400)

        Returns:
            Raw memory data
        """
        data = address.to_bytes(4, "big") + size.to_bytes(2, "big")
        response = self.send_request(SID_READ_MEM_BY_ADDR, data, timeout=TIMEOUT_READ)
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
        response = self.send_request(SID_READ_DTC_COUNT, bytes([0x02, 0x00]))
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
        response = self.send_request(
            SID_READ_DTC_STATUS,
            bytes([0x00, 0xFF, 0x00]),
        )

        dtcs = []
        # Response format: each DTC is 3 bytes (code_hi, code_lo, status)
        offset = 0
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
        Scan ECU RAM addresses 0x0000-0xBFFF.

        Reads 192 blocks of 0x1F0 bytes each (total ~96KB).
        Based on romdrop's uds_ScanRAM at 0x00404AE2.

        Returns:
            RAM contents as bytearray
        """
        total_blocks = 192  # 0xC0
        block_size = 0x1F0
        ram = bytearray(total_blocks * block_size)

        for i in range(total_blocks):
            address = i * block_size
            data = self.read_memory_by_address(address, block_size)
            ram[address : address + len(data)] = data
            if progress_callback:
                progress_callback(i + 1, total_blocks)

        return ram
