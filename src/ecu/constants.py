"""
ECU Protocol Constants

CAN, UDS, ROM layout, and flash parameters for Mazda NC Miata ECU communication.
"""

# --- CAN Bus ---
CAN_REQUEST_ID = 0x7E0
CAN_RESPONSE_ID = 0x7E8
CAN_BAUDRATE = 500_000
J2534_PROTOCOL_ISO15765 = 6
ISO15765_TX_FLAGS = 0x00000040  # ISO15765 frame pad

# --- ROM Layout ---
ROM_SIZE = 0x100000  # 1 MB
ROM_FLASH_START_MIN = 0x2000
ROM_ID_OFFSET = 0xFFC4C
CAL_ID_OFFSETS = (0xC0046, 0xB8046)
GEN_DETECT_OFFSET = 0x2030
GEN_NC1 = 0x35
GEN_NC2_A = 0x36
GEN_NC2_B = 0x37

# --- Checksum ---
CHECKSUM_MAGIC = 0x5AA5A55A
CHECKSUM_TABLE_OFFSET = 0xFF650
CHECKSUM_TABLE_END = 0xFF7FC
CHECKSUM_ENTRY_SIZE = 12  # 3 x 4-byte big-endian words

# --- Flash Transfer ---
SBL_SIZE = 0x1800  # 6 KB
SBL_UPLOAD_ADDR = 0x8000
DOWNLOAD_ADDR = 0x00008000
DOWNLOAD_SIZE = 0x000FF800
BLOCK_SIZE = 0x400  # 1 KB transfer blocks

# --- Dynamic Flash Alignment ---
DYNAMIC_ALIGN_SMALL = 0x1000  # alignment for regions < 0x8000
DYNAMIC_ALIGN_LARGE = 0x20000  # alignment for regions >= 0x20000
DYNAMIC_THRESHOLD = 0x8000

# --- OBD-II Service IDs ---
SID_OBD_CURRENT_DATA = 0x01  # Service 0x01: current data (PIDs)

# Standard OBD-II PIDs
OBD_PID_ENGINE_RPM = 0x0C  # 2 bytes, value/4 = RPM
OBD_PID_CONTROL_MODULE_VOLTAGE = 0x42  # 2 bytes, value/1000 = volts

# --- Safety Thresholds ---
BATTERY_VOLTAGE_WARNING = 12.0  # volts — block flash below this
#: RPM at/above which a flash is refused in code (engine running). Read once via
#: OBD PID 0x0C at the flash boundary, BEFORE the programming session is entered
#: (in-session OBD Mode-01 returns NRC 0x11). Override is explicit + off by default.
RPM_FLASH_GATE = 1.0

# --- WiCAN no-reboot coexistence (docs/internal/WICAN_SLCAN_COEXISTENCE_PLAN.md) ---
#: TCP port of the always-on dedicated SLCAN listener that no-reboot coexistence
#: firmware keeps open alongside the datalogger. The host flashes over this port
#: with NO protocol-switch reboot and without disturbing the datalogger. Pinned +
#: probed via version_ping (never assumed present).
WICAN_DEDICATED_SLCAN_PORT = 35001
#: Firmware build (NCFRv<rev>) at/above which the dedicated SLCAN port exists.
#: Today's fastwrite firmware is NCFRv5 (no dedicated port) → the host falls back
#: to the legacy reboot-switch. The coexistence firmware (task #36) bumps the
#: marker to this rev AND opens WICAN_DEDICATED_SLCAN_PORT; both sides share this
#: one contract.
COEXIST_MIN_FW_REV = 6
#: Probe connect timeout (ms) for the coexist-port capability check. Short so a
#: device WITHOUT the dedicated port (every current build) falls back to the
#: proven reboot path quickly instead of stalling the connect.
COEXIST_PROBE_TIMEOUT_MS = 1500
#: Settle (s) after a bus-claim+pause before the FIRST host-driven ECU contact.
#: On the coexist port the datalogger owns the single CAN bus until it parks; the
#: poll task parks on can_should_park() at its own cadence, so a poll frame in
#: flight can otherwise collide with the first Tester-Present / auth handshake.
#: Used by the refcounted bus reservation (WiCANDatalogClient.acquire_bus) so it
#: runs once on the 0->1 transition, BEFORE connect's Tester-Present and the flash.
PRE_SESSION_SETTLE_S = 0.2

# --- WiCAN dead-man's-switch / datalog auto-resume (docs/internal/WICAN_DEADMAN_AUTORESUME.md) ---
# ONE timing contract shared by host + firmware (firmware uses the *_US = *_S * 1e6
# microsecond forms). These govern the brick-safe auto-resume of the datalogger when
# NC-Flash vanishes (lid close / crash / Wi-Fi drop) WITHOUT ever resuming during a
# live ECU write. The firmware reaper is authoritative; these host values just keep
# the leases renewed while NC-Flash is present.
#: Advisory datalog-park lease TTL (s). The firmware reaper auto-resumes the logger
#: this long after the host stops renewing — but ONLY when no flash/claim owns the bus.
PARK_LEASE_TTL_S = 12.0
#: Host bus-claim lease TTL (s). MUST exceed the worst-case host-driven auth window
#: (TIMEOUT_RESPONSE_PENDING_MAX 60s + settle + key-compute + margin) — a 30s TTL is
#: provably too small. This is only the host-GONE backstop; the firmware
#: !host_bus_claim_active() gate is a HARD bit that never expires under a present host.
HOST_CLAIM_LEASE_TTL_S = 75.0
#: Keepalive POST interval (s). ⅓ of the park TTL (tolerates 2 lost keepalives before a
#: false expiry) and under DATALOG_TIMEOUT_S so renews never pile up. Renews BOTH leases.
DATALOG_KEEPALIVE_INTERVAL_S = 4.0
#: Bus-idle quiesce window (ms): the reaper requires this long with NO tx AND no rx
#: before resuming (the SD flash drives blocks ~211ms apart, so 300ms proves
#: "between operations"). Host-side informational; the firmware enforces it.
BUS_IDLE_QUIESCE_MS = 300
#: After a host-claim lease expiry the firmware reaper waits this long (ECU drops its
#: programming session on host silence) before resuming. Host-side informational.
HOST_SESSION_TEARDOWN_GRACE_MS = 3000
#: Firmware raises a "power-cycle required" alarm (never auto-clears the brick bit) if
#: FLASH_ACTIVE_BIT stays set longer than this. Host-side informational.
STUCK_FLASH_CEILING_MS = 180000

# --- UDS Service IDs ---
SID_DIAGNOSTIC_SESSION = 0x10
SID_ECU_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC_STATUS = 0x18
SID_READ_DTC_COUNT = 0x22
SID_READ_MEM_BY_ADDR = 0x23
SID_SECURITY_ACCESS = 0x27
SID_REQUEST_DOWNLOAD = 0x34
SID_TRANSFER_DATA = 0x36
SID_TRANSFER_EXIT = 0x37
SID_TESTER_PRESENT = 0x3E
SID_ROUTINE_CONTROL = 0xB1

# --- UDS Sub-functions ---
DIAG_SESSION_PROGRAMMING = 0x85
RESET_HARD = 0x01
SECURITY_REQUEST_SEED = 0x01
SECURITY_SEND_KEY = 0x02
TESTER_PRESENT_SUB = 0x01

# --- UDS Negative Response ---
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_RESPONSE_PENDING = 0x78

# --- J2534 Constants ---
J2534_STATUS_NOERROR = 0
PASSTHRU_MSG_DATA_SIZE = 4128  # SAE J2534-1 specifies PASSTHRU_MSG.Data as byte[4128]

# J2534 Filter Types
PASS_FILTER = 0x01
BLOCK_FILTER = 0x02
FLOW_CONTROL_FILTER = 0x03

# J2534 IOCTL IDs
GET_CONFIG = 0x01
SET_CONFIG = 0x02
CLEAR_TX_BUFFER = 0x07
CLEAR_RX_BUFFER = 0x08

# J2534 Config Parameter IDs
ISO15765_BS = 0x1E
ISO15765_STMIN = 0x1F

# --- Timeouts (ms) ---
TIMEOUT_DEFAULT = 5000
TIMEOUT_SECURITY = 10000
TIMEOUT_TRANSFER = 10000
TIMEOUT_READ = 5000
TIMEOUT_RESET = 15000
TIMEOUT_RESPONSE_PENDING_MAX = 60000  # max wait for NRC 0x78 retries

# --- Flash Counter ---
FLASH_COUNTER_CMD = bytes([SID_ROUTINE_CONTROL, 0x00, 0xB2, 0x00])
#: ROM region the ECU itself stamps each time it is programmed (a flash-cycle
#: counter). It is NOT part of the image we write, so a read-back verify must
#: exclude it — otherwise a byte-perfect flash "fails" on these bytes. Matches the
#: region ``get_calibration_crc(clear_flash_counter=True)`` masks before hashing.
FLASH_COUNTER_OFFSET = 0xFFB00
FLASH_COUNTER_SIZE = 8

# --- Archive ---
ARCHIVE_FILENAME = (
    "ncflash.rda"  # Single file: current ROM on ECU (overwritten each flash)
)

# --- Default J2534 DLL ---
DEFAULT_J2534_DLL = "op20pt32.dll"
