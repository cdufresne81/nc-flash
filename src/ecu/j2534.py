"""
J2534 PassThru DLL Wrapper

Provides a ctypes-based interface to J2534 PassThru device drivers on Windows.
Default target: Tactrix OpenPort 2.0 (op20pt32.dll).

This is safety-critical code used for ECU communication and flash operations.
All function calls validate return codes and raise descriptive exceptions on failure.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import struct as _struct
import subprocess
import sys
import threading
import zlib
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_ulong,
    c_ubyte,
)
from pathlib import Path
from typing import Optional

from .constants import (
    J2534_STATUS_NOERROR,
    PASSTHRU_MSG_DATA_SIZE,
    J2534_PROTOCOL_ISO15765,
    CAN_BAUDRATE,
    CAN_REQUEST_ID,
    CAN_RESPONSE_ID,
    PASS_FILTER,
    BLOCK_FILTER,
    FLOW_CONTROL_FILTER,
    GET_CONFIG,
    SET_CONFIG,
    CLEAR_TX_BUFFER,
    CLEAR_RX_BUFFER,
    ISO15765_BS,
    ISO15765_STMIN,
    ISO15765_TX_FLAGS,
    DEFAULT_J2534_DLL,
)
from .exceptions import (
    J2534Error,
    J2534DLLNotFound,
    J2534DeviceNotFound,
    J2534ConnectionError,
)

logger = logging.getLogger(__name__)


def find_j2534_dll(dll_name: str = DEFAULT_J2534_DLL) -> str:
    """
    Resolve a J2534 DLL name to its full path via the Windows registry.

    J2534 drivers register under HKLM\\SOFTWARE\\PassThruSupport.04.04 (or
    WOW6432Node on 64-bit Windows). Each subkey has a 'FunctionLibrary' value
    with the absolute path to the DLL.

    Args:
        dll_name: DLL filename to find (e.g. 'op20pt32.dll').

    Returns:
        Full path if found in the registry, otherwise the original dll_name.
    """
    if sys.platform != "win32":
        return dll_name

    # If already an absolute path that exists, use it directly
    if os.path.isabs(dll_name) and os.path.isfile(dll_name):
        return dll_name

    import winreg

    registry_paths = [
        r"SOFTWARE\PassThruSupport.04.04",
        r"SOFTWARE\WOW6432Node\PassThruSupport.04.04",
    ]

    target = dll_name.lower()

    for reg_path in registry_paths:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
        except FileNotFoundError:
            continue

        try:
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        lib_path, _ = winreg.QueryValueEx(subkey, "FunctionLibrary")
                        if Path(lib_path).name.lower() == target:
                            logger.info("Found J2534 DLL in registry: %s", lib_path)
                            winreg.CloseKey(subkey)
                            winreg.CloseKey(key)
                            return lib_path
                    except FileNotFoundError:
                        pass
                    winreg.CloseKey(subkey)
                    i += 1
                except OSError:
                    break
        finally:
            winreg.CloseKey(key)

    logger.debug("J2534 DLL '%s' not found in registry, using as-is", dll_name)
    return dll_name


def _find_bridge_exe() -> Optional[str]:
    """
    Find the bundled 32-bit bridge executable.

    Searches in order:
    1. Next to the running executable (PyInstaller bundle)
    2. In dist/j2534_bridge_32/ (development build)

    Returns:
        Path to the bridge executable if found, None otherwise.
    """
    candidates = []

    # PyInstaller bundle: data files live in _internal/ (sys._MEIPASS)
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if meipass.is_dir():
            candidates.append(meipass / "j2534_bridge_32" / "j2534_bridge_32.exe")
            candidates.append(meipass / "j2534_bridge_32.exe")
        # Also check next to the exe (flat layout or older PyInstaller)
        app_dir = Path(sys.executable).parent
        candidates.append(app_dir / "j2534_bridge_32" / "j2534_bridge_32.exe")
        candidates.append(app_dir / "j2534_bridge_32.exe")

    # Development: check dist/ relative to repo root
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(repo_root / "dist" / "j2534_bridge_32" / "j2534_bridge_32.exe")

    for candidate in candidates:
        if candidate.is_file():
            logger.debug("Found bridge executable: %s", candidate)
            return str(candidate)

    return None


def _find_matching_python() -> Optional[list[str]]:
    """
    Find a Python executable whose bitness is opposite to the current process.

    Uses the Windows 'py' launcher (PEP 397) which can target specific bitness
    via 'py -3-32' or 'py -3-64'.

    Returns:
        Command list (e.g. ['py', '-3-32']) if found, None otherwise.
    """
    current_bits = _struct.calcsize("P") * 8
    target_bits = 32 if current_bits == 64 else 64
    cmd = ["py", f"-3-{target_bits}"]

    try:
        result = subprocess.run(
            cmd + ["-c", "print('ok')"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and "ok" in result.stdout:
            logger.info(
                "Found %d-bit Python via launcher: %s", target_bits, " ".join(cmd)
            )
            return cmd
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    logger.debug("No %d-bit Python found via 'py' launcher", target_bits)
    return None


def _auto_build_bridge() -> Optional[str]:
    """Auto-build the 32-bit bridge exe in development mode.

    Returns the exe path on success, None on failure (silent — caller
    falls through to the next resolution step).
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    spec_file = repo_root / "packaging" / "j2534_bridge_32.spec"
    if not spec_file.is_file():
        return None

    try:
        # Import the build helper (lives in packaging/)
        sys.path.insert(0, str(repo_root / "packaging"))
        try:
            from build_bridge import build_bridge
        finally:
            sys.path.pop(0)

        logger.info("Bridge exe not found — auto-building (first time only)...")
        result = build_bridge()
        if result and result.is_file():
            logger.info("Bridge auto-built: %s", result)
            return str(result)
    except Exception:
        logger.debug("Auto-build failed", exc_info=True)

    return None


def _msg_to_dict(msg: PassThruMsg) -> dict:
    """Serialize a PassThruMsg to a JSON-safe dict."""
    return {
        "ProtocolID": msg.ProtocolID,
        "TxFlags": msg.TxFlags,
        "DataSize": msg.DataSize,
        "Data": bytes(msg.Data[: msg.DataSize]).hex(),
    }


def _dict_to_msg(d: dict) -> PassThruMsg:
    """Deserialize a dict back into a PassThruMsg."""
    msg = PassThruMsg()
    msg.ProtocolID = d["ProtocolID"]
    msg.RxStatus = d.get("RxStatus", 0)
    msg.TxFlags = d.get("TxFlags", 0)
    msg.Timestamp = d.get("Timestamp", 0)
    msg.DataSize = d["DataSize"]
    msg.ExtraDataIndex = d.get("ExtraDataIndex", 0)
    data_bytes = bytes.fromhex(d["Data"])
    for i, b in enumerate(data_bytes):
        msg.Data[i] = b
    return msg


# J2534 error code descriptions
_ERROR_DESCRIPTIONS = {
    0x00: "STATUS_NOERROR",
    0x01: "ERR_NOT_SUPPORTED",
    0x02: "ERR_INVALID_CHANNEL_ID",
    0x03: "ERR_INVALID_PROTOCOL_ID",
    0x04: "ERR_NULL_PARAMETER",
    0x05: "ERR_INVALID_IOCTL_VALUE",
    0x06: "ERR_INVALID_FLAGS",
    0x07: "ERR_FAILED",
    0x08: "ERR_DEVICE_NOT_CONNECTED",
    0x09: "ERR_TIMEOUT",
    0x0A: "ERR_INVALID_MSG",
    0x0B: "ERR_INVALID_TIME_INTERVAL",
    0x0C: "ERR_EXCEEDED_LIMIT",
    0x0D: "ERR_INVALID_MSG_ID",
    0x0E: "ERR_DEVICE_IN_USE",
    0x0F: "ERR_INVALID_IOCTL_ID",
    0x10: "ERR_BUFFER_EMPTY",
    0x11: "ERR_BUFFER_FULL",
    0x12: "ERR_BUFFER_OVERFLOW",
    0x13: "ERR_PIN_INVALID",
    0x14: "ERR_CHANNEL_IN_USE",
    0x15: "ERR_MSG_PROTOCOL_ID",
    0x16: "ERR_INVALID_FILTER_ID",
    0x17: "ERR_NO_FLOW_CONTROL",
    0x18: "ERR_NOT_UNIQUE",
    0x19: "ERR_INVALID_BAUDRATE",
    0x1A: "ERR_INVALID_DEVICE_ID",
}


# ---------------------------------------------------------------------------
# ctypes Structure Definitions
# ---------------------------------------------------------------------------


class PassThruMsg(Structure):
    """J2534 PASSTHRU_MSG structure for CAN/ISO-15765 message exchange."""

    _fields_ = [
        ("ProtocolID", c_ulong),
        ("RxStatus", c_ulong),
        ("TxFlags", c_ulong),
        ("Timestamp", c_ulong),
        ("DataSize", c_ulong),
        ("ExtraDataIndex", c_ulong),
        ("Data", c_ubyte * PASSTHRU_MSG_DATA_SIZE),
    ]


# Alias for external use
PASSTHRU_MSG = PassThruMsg


class SCONFIG(Structure):
    """J2534 SCONFIG structure for a single configuration parameter."""

    _fields_ = [
        ("Parameter", c_ulong),
        ("Value", c_ulong),
    ]


class SCONFIG_LIST(Structure):
    """J2534 SCONFIG_LIST structure for batch configuration operations."""

    _fields_ = [
        ("NumOfParams", c_ulong),
        ("ConfigPtr", POINTER(SCONFIG)),
    ]


# J2534 PassThru function names as defined in the SAE J2534 specification
_PASSTHRU_FUNCTIONS = [
    "PassThruOpen",
    "PassThruClose",
    "PassThruConnect",
    "PassThruDisconnect",
    "PassThruReadMsgs",
    "PassThruWriteMsgs",
    "PassThruStartPeriodicMsg",
    "PassThruStopPeriodicMsg",
    "PassThruStartMsgFilter",
    "PassThruStopMsgFilter",
    "PassThruSetProgrammingVoltage",
    "PassThruReadVersion",
    "PassThruGetLastError",
    "PassThruIoctl",
]


class J2534Device:
    """
    J2534 PassThru device interface.

    Wraps a J2534-compliant DLL to provide ECU communication over CAN/ISO-15765.
    Supports context manager protocol for automatic resource cleanup.

    Usage:
        with J2534Device() as dev:
            ch = dev.connect(J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE)
            ...
            dev.disconnect(ch)
    """

    def __init__(self, dll_path: str = DEFAULT_J2534_DLL):
        self._dll_path = dll_path
        self._dll = None
        self._funcs: dict = {}
        self._device_id: Optional[int] = None
        self._bridge: Optional[subprocess.Popen] = None  # 32/64-bit bridge

        # Dev only: pre-build the 32-bit bridge exe if missing
        if not getattr(sys, "frozen", False) and _find_bridge_exe() is None:
            _auto_build_bridge()

    def __enter__(self) -> J2534Device:
        self.open()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        self.close()
        return None

    # ------------------------------------------------------------------
    # DLL Loading
    # ------------------------------------------------------------------

    def _ensure_dll(self) -> None:
        """Lazy-load the J2534 DLL on first use, with bridge fallback."""
        if self._dll is not None or self._bridge is not None:
            return

        # Resolve DLL name to full path via Windows registry
        resolved = find_j2534_dll(self._dll_path)
        try:
            try:
                self._dll = ctypes.WinDLL(resolved)
            except AttributeError:
                # Non-Windows or WinDLL unavailable — fall back to CDLL
                self._dll = ctypes.CDLL(resolved)
        except OSError as e:
            needs_bridge = (
                getattr(e, "winerror", 0) == 193  # bitness mismatch
                or (
                    getattr(sys, "frozen", False)
                    and "application was frozen" in str(e)
                )
            )
            if needs_bridge:
                logger.debug(
                    "Cannot load DLL directly ('%s'), attempting bridge",
                    resolved,
                )
                self._start_bridge(resolved)
                return
            raise J2534DLLNotFound(
                f"Cannot load J2534 DLL '{self._dll_path}': {e}. "
                f"Try using the full path."
            ) from e

        self._resolve_functions()

    # ------------------------------------------------------------------
    # Bridge Subprocess (32/64-bit interop)
    # ------------------------------------------------------------------

    def _start_bridge(self, dll_path: str) -> None:
        """
        Spawn a 32-bit subprocess to load the J2534 DLL.

        Resolution order:
        1. Bundled bridge executable (j2534_bridge_32.exe) — works in
           PyInstaller builds and dev when the exe has been pre-built
           (auto-built in __init__ if missing).
        2. 32-bit Python via 'py -3-32' launcher — dev fallback.

        Safety: The bridge is health-checked before returning. If it fails
        to start, an exception is raised immediately.
        """
        bridge_exe = _find_bridge_exe()
        if bridge_exe:
            cmd = [bridge_exe, dll_path]
            logger.debug("Starting bridge via exe: %s", bridge_exe)
        else:
            python_cmd = _find_matching_python()
            if python_cmd is None:
                current_bits = _struct.calcsize("P") * 8
                other_bits = 32 if current_bits == 64 else 64
                raise J2534DLLNotFound(
                    f"J2534 DLL requires {other_bits}-bit Python but only "
                    f"{current_bits}-bit is running. Install it with:\n"
                    f"  winget install Python.Python.3.12 --architecture x86\n"
                    f"Then restart the app — the bridge will be built "
                    f"automatically."
                )
            bridge_script = str(Path(__file__).parent / "j2534_bridge.py")
            cmd = [*python_cmd, bridge_script, dll_path]
            logger.debug("Starting bridge via py launcher: %s", " ".join(cmd))

        # Hide console window on Windows
        creationflags = (
            subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        )
        self._bridge = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )

        # Wait for "ready" signal from bridge
        try:
            resp = self._bridge_recv(timeout=10.0)
        except Exception as e:
            stderr = ""
            if self._bridge and self._bridge.stderr:
                try:
                    stderr = self._bridge.stderr.read().decode(errors="replace")
                except Exception:
                    pass
            self._kill_bridge()
            raise J2534DLLNotFound(
                f"J2534 bridge failed to start: {e}"
                + (f"\nBridge stderr: {stderr}" if stderr else "")
            ) from e

        if not resp.get("ok"):
            self._kill_bridge()
            raise J2534DLLNotFound(
                f"J2534 bridge: {resp.get('error', 'unknown error')}"
            )

        logger.debug("J2534 bridge started (PID=%d)", self._bridge.pid)

    def _bridge_alive(self) -> bool:
        """Check if the bridge subprocess is still running."""
        return self._bridge is not None and self._bridge.poll() is None

    def _bridge_call(self, cmd: str, timeout: float = 10.0, **kwargs) -> dict:
        """
        Send a command to the bridge and return the response.

        Safety:
        - Checks bridge liveness before sending
        - CRC32 on request and response for data integrity
        - Strict timeout kills bridge on hang (prevents stuck mid-flash)
        - No retries — any failure raises immediately
        """
        if not self._bridge_alive():
            raise J2534Error(
                "J2534 bridge process died unexpectedly. "
                "ECU communication lost — do NOT retry without verifying ECU state."
            )

        request = {"cmd": cmd, **kwargs}
        # Add CRC32 for integrity
        payload = json.dumps(request, sort_keys=True).encode()
        request["crc32"] = zlib.crc32(payload) & 0xFFFFFFFF

        line = json.dumps(request) + "\n"
        try:
            self._bridge.stdin.write(line.encode())
            self._bridge.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill_bridge()
            raise J2534Error(f"J2534 bridge pipe broken: {e}") from e

        resp = self._bridge_recv(timeout=timeout)

        if not resp.get("ok"):
            error = resp.get("error", "Unknown bridge error")
            raise J2534Error(f"J2534 bridge: {error}")

        return resp

    def _bridge_recv(self, timeout: float = 10.0) -> dict:
        """
        Read one JSON response line from the bridge with timeout.

        Uses a daemon thread for the blocking readline (Windows pipes
        don't support select/poll). On timeout, kills the bridge
        immediately — a hung bridge during flash is catastrophic.
        """
        result: list = [None]
        error: list = [None]

        def _read():
            try:
                raw = self._bridge.stdout.readline()
                if not raw:
                    error[0] = "Bridge closed stdout (process died)"
                    return
                result[0] = json.loads(raw)
            except Exception as e:
                error[0] = str(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            self._kill_bridge()
            raise J2534Error(
                f"J2534 bridge unresponsive (timeout={timeout}s). "
                "Bridge killed for safety — do NOT assume ECU state is clean."
            )

        if error[0]:
            raise J2534Error(f"J2534 bridge recv: {error[0]}")

        resp = result[0]
        if resp is None:
            raise J2534Error("J2534 bridge returned empty response")

        # Verify CRC32 integrity
        expected_crc = resp.pop("crc32", None)
        if expected_crc is not None:
            check_payload = json.dumps(resp, sort_keys=True).encode()
            actual_crc = zlib.crc32(check_payload) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                self._kill_bridge()
                raise J2534Error(
                    "J2534 bridge CRC mismatch — data corrupted in transit. "
                    "Bridge killed. Do NOT continue ECU operations."
                )

        return resp

    def _kill_bridge(self) -> None:
        """Forcibly terminate the bridge subprocess."""
        if self._bridge is not None:
            logger.debug("Cleaning up J2534 bridge (PID=%d)", self._bridge.pid)
            try:
                self._bridge.kill()
                self._bridge.wait(timeout=5)
            except Exception:
                pass
            self._bridge = None

    def _resolve_functions(self) -> None:
        """Resolve all 14 PassThru function pointers from the loaded DLL."""
        for name in _PASSTHRU_FUNCTIONS:
            try:
                func = getattr(self._dll, name)
                self._funcs[name] = func
            except AttributeError:
                logger.warning("J2534 function '%s' not found in DLL", name)
                self._funcs[name] = None

    # ------------------------------------------------------------------
    # Error Handling
    # ------------------------------------------------------------------

    def _check_error(self, result: int, function_name: str) -> None:
        """
        Validate a J2534 return code.

        Raises J2534Error with a descriptive message if the result is non-zero.
        Maps known error codes to SAE J2534 error names.
        """
        if result == J2534_STATUS_NOERROR:
            return

        desc = _ERROR_DESCRIPTIONS.get(result, f"UNKNOWN_ERROR_0x{result:02X}")
        last_error = self._get_last_error()
        msg = f"{function_name} failed: {desc} (code {result})"
        if last_error:
            msg += f" — {last_error}"
        logger.error(msg)
        raise J2534Error(msg)

    def _get_last_error(self) -> str:
        """Retrieve the last error description string from the DLL."""
        func = self._funcs.get("PassThruGetLastError")
        if func is None:
            return ""
        try:
            buf = ctypes.create_string_buffer(256)
            func(buf)
            return buf.value.decode("ascii", errors="replace").strip()
        except Exception:
            return ""

    def _require_func(self, name: str):
        """Return the resolved function pointer or raise J2534Error."""
        func = self._funcs.get(name)
        if func is None:
            raise J2534Error(
                f"J2534 function '{name}' is not available in the loaded DLL"
            )
        return func

    # ------------------------------------------------------------------
    # Device / Channel Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """
        Open a connection to the J2534 device.

        Stores the device ID for subsequent operations.
        Raises J2534DeviceNotFound if the device cannot be opened.
        """
        self._ensure_dll()

        if self._bridge:
            try:
                resp = self._bridge_call("open")
            except J2534Error as e:
                raise J2534DeviceNotFound(str(e)) from e
            self._device_id = resp["device_id"]
            logger.info("Opened J2534 device via bridge (ID=%d)", self._device_id)
            return

        func = self._require_func("PassThruOpen")
        device_id = c_ulong()

        try:
            result = func(None, byref(device_id))
        except OSError as e:
            raise J2534DeviceNotFound(
                f"Failed to communicate with J2534 device: {e}"
            ) from e

        try:
            self._check_error(result, "PassThruOpen")
        except J2534Error as e:
            raise J2534DeviceNotFound(str(e)) from e

        self._device_id = device_id.value
        logger.info("Opened J2534 device (ID=%d)", self._device_id)

    def close(self) -> None:
        """
        Close the J2534 device connection.

        Safe to call multiple times. Logs but does not raise on failure,
        since close is often called during cleanup.
        """
        if self._bridge:
            if self._device_id is not None and self._bridge_alive():
                try:
                    self._bridge_call("close", device_id=self._device_id)
                except Exception as e:
                    logger.warning("Bridge close error (non-fatal): %s", e)
            # Send exit and terminate
            if self._bridge_alive():
                try:
                    line = json.dumps({"cmd": "exit"}) + "\n"
                    self._bridge.stdin.write(line.encode())
                    self._bridge.stdin.flush()
                    self._bridge.wait(timeout=5)
                except Exception:
                    pass
            self._kill_bridge()
            logger.info("Closed J2534 device via bridge (ID=%s)", self._device_id)
            self._device_id = None
            return

        if self._device_id is None:
            return

        func = self._funcs.get("PassThruClose")
        if func is not None:
            try:
                result = func(c_ulong(self._device_id))
                if result != J2534_STATUS_NOERROR:
                    desc = _ERROR_DESCRIPTIONS.get(result, f"0x{result:02X}")
                    logger.warning("PassThruClose returned %s", desc)
            except OSError as e:
                logger.warning("PassThruClose raised OSError: %s", e)

        logger.info("Closed J2534 device (ID=%d)", self._device_id)
        self._device_id = None

    def connect(self, protocol: int, flags: int, baudrate: int) -> int:
        """
        Open a communication channel on the device.

        Args:
            protocol: J2534 protocol ID (e.g. J2534_PROTOCOL_ISO15765).
            flags: Connection flags (typically 0).
            baudrate: Channel baud rate (e.g. 500000 for CAN).

        Returns:
            Channel ID for use in subsequent read/write/filter operations.

        Raises:
            J2534ConnectionError: If the connection cannot be established.
        """
        if self._device_id is None:
            raise J2534Error("Device not open — call open() first")

        self._ensure_dll()

        if self._bridge:
            try:
                resp = self._bridge_call(
                    "connect",
                    device_id=self._device_id,
                    protocol=protocol,
                    flags=flags,
                    baudrate=baudrate,
                )
            except J2534Error as e:
                raise J2534ConnectionError(str(e)) from e
            ch = resp["channel_id"]
            logger.info(
                "Connected channel %d via bridge (protocol=%d, baudrate=%d)",
                ch,
                protocol,
                baudrate,
            )
            return ch

        func = self._require_func("PassThruConnect")
        channel_id = c_ulong()

        try:
            result = func(
                c_ulong(self._device_id),
                c_ulong(protocol),
                c_ulong(flags),
                c_ulong(baudrate),
                byref(channel_id),
            )
        except OSError as e:
            raise J2534ConnectionError(f"PassThruConnect failed: {e}") from e

        try:
            self._check_error(result, "PassThruConnect")
        except J2534Error as e:
            raise J2534ConnectionError(str(e)) from e

        logger.info(
            "Connected channel %d (protocol=%d, baudrate=%d)",
            channel_id.value,
            protocol,
            baudrate,
        )
        return channel_id.value

    def disconnect(self, channel_id: int) -> None:
        """
        Disconnect a communication channel.

        Args:
            channel_id: Channel ID returned by connect().
        """
        self._ensure_dll()

        if self._bridge:
            self._bridge_call("disconnect", channel_id=channel_id)
            logger.info("Disconnected channel %d via bridge", channel_id)
            return

        func = self._require_func("PassThruDisconnect")
        result = func(c_ulong(channel_id))
        self._check_error(result, "PassThruDisconnect")
        logger.info("Disconnected channel %d", channel_id)

    # ------------------------------------------------------------------
    # Message I/O
    # ------------------------------------------------------------------

    def read_msgs(
        self, channel_id: int, count: int = 1, timeout: int = 1000
    ) -> list[PassThruMsg]:
        """
        Read messages from a channel.

        Args:
            channel_id: Channel ID returned by connect().
            count: Maximum number of messages to read.
            timeout: Read timeout in milliseconds.

        Returns:
            List of received PassThruMsg instances (may be fewer than count).
        """
        self._ensure_dll()

        if self._bridge:
            # Bridge timeout: J2534 timeout (ms→s) + overhead for IPC
            bridge_timeout = (timeout / 1000.0) + 2.0
            resp = self._bridge_call(
                "read_msgs",
                timeout=bridge_timeout,
                channel_id=channel_id,
                count=count,
                j2534_timeout=timeout,
            )
            return [_dict_to_msg(d) for d in resp.get("msgs", [])]

        func = self._require_func("PassThruReadMsgs")

        msgs = (PassThruMsg * count)()
        num_msgs = c_ulong(count)

        result = func(
            c_ulong(channel_id),
            byref(msgs),
            byref(num_msgs),
            c_ulong(timeout),
        )

        # ERR_BUFFER_EMPTY (0x10) and ERR_TIMEOUT (0x09) are non-fatal for reads
        if result == 0x10 or result == 0x09:
            return []

        self._check_error(result, "PassThruReadMsgs")
        return [msgs[i] for i in range(num_msgs.value)]

    def write_msgs(
        self,
        channel_id: int,
        msgs: list[PassThruMsg],
        timeout: int = 1000,
    ) -> None:
        """
        Write messages to a channel.

        Args:
            channel_id: Channel ID returned by connect().
            msgs: List of PassThruMsg instances to transmit.
            timeout: Write timeout in milliseconds.
        """
        if not msgs:
            return

        self._ensure_dll()

        if self._bridge:
            bridge_timeout = (timeout / 1000.0) + 2.0
            self._bridge_call(
                "write_msgs",
                timeout=bridge_timeout,
                channel_id=channel_id,
                msgs=[_msg_to_dict(m) for m in msgs],
                j2534_timeout=timeout,
            )
            logger.debug(
                "Wrote %d message(s) to channel %d via bridge", len(msgs), channel_id
            )
            return

        func = self._require_func("PassThruWriteMsgs")

        msg_array = (PassThruMsg * len(msgs))(*msgs)
        num_msgs = c_ulong(len(msgs))

        result = func(
            c_ulong(channel_id),
            byref(msg_array),
            byref(num_msgs),
            c_ulong(timeout),
        )
        self._check_error(result, "PassThruWriteMsgs")
        logger.debug("Wrote %d message(s) to channel %d", num_msgs.value, channel_id)

    # ------------------------------------------------------------------
    # Message Filters
    # ------------------------------------------------------------------

    def start_msg_filter(
        self,
        channel_id: int,
        filter_type: int,
        mask_msg: Optional[PassThruMsg],
        pattern_msg: Optional[PassThruMsg],
        flow_control_msg: Optional[PassThruMsg] = None,
    ) -> int:
        """
        Start a message filter on a channel.

        Args:
            channel_id: Channel ID returned by connect().
            filter_type: PASS_FILTER, BLOCK_FILTER, or FLOW_CONTROL_FILTER.
            mask_msg: Mask message (which bits to check).
            pattern_msg: Pattern message (expected bit values).
            flow_control_msg: Flow control message (required for ISO-15765).

        Returns:
            Filter ID for use with stop_msg_filter().
        """
        self._ensure_dll()

        if self._bridge:
            resp = self._bridge_call(
                "start_msg_filter",
                channel_id=channel_id,
                filter_type=filter_type,
                mask=_msg_to_dict(mask_msg) if mask_msg else None,
                pattern=_msg_to_dict(pattern_msg) if pattern_msg else None,
                flow_control=(
                    _msg_to_dict(flow_control_msg) if flow_control_msg else None
                ),
            )
            fid = resp["filter_id"]
            logger.info(
                "Started filter %d on channel %d via bridge (type=%d)",
                fid,
                channel_id,
                filter_type,
            )
            return fid

        func = self._require_func("PassThruStartMsgFilter")

        filter_id = c_ulong()

        mask_ptr = byref(mask_msg) if mask_msg is not None else None
        pattern_ptr = byref(pattern_msg) if pattern_msg is not None else None
        fc_ptr = byref(flow_control_msg) if flow_control_msg is not None else None

        result = func(
            c_ulong(channel_id),
            c_ulong(filter_type),
            mask_ptr,
            pattern_ptr,
            fc_ptr,
            byref(filter_id),
        )
        self._check_error(result, "PassThruStartMsgFilter")
        logger.info(
            "Started filter %d on channel %d (type=%d)",
            filter_id.value,
            channel_id,
            filter_type,
        )
        return filter_id.value

    def stop_msg_filter(self, channel_id: int, filter_id: int) -> None:
        """
        Stop a previously started message filter.

        Args:
            channel_id: Channel ID returned by connect().
            filter_id: Filter ID returned by start_msg_filter().
        """
        self._ensure_dll()

        if self._bridge:
            self._bridge_call(
                "stop_msg_filter", channel_id=channel_id, filter_id=filter_id
            )
            logger.info(
                "Stopped filter %d on channel %d via bridge", filter_id, channel_id
            )
            return

        func = self._require_func("PassThruStopMsgFilter")
        result = func(c_ulong(channel_id), c_ulong(filter_id))
        self._check_error(result, "PassThruStopMsgFilter")
        logger.info("Stopped filter %d on channel %d", filter_id, channel_id)

    # ------------------------------------------------------------------
    # Configuration (IOCTL)
    # ------------------------------------------------------------------

    def set_config(self, channel_id: int, params: dict[int, int]) -> None:
        """
        Set configuration parameters on a channel via SET_CONFIG IOCTL.

        Args:
            channel_id: Channel ID returned by connect().
            params: Dict mapping parameter IDs to values
                    (e.g. {ISO15765_BS: 0, ISO15765_STMIN: 0}).
        """
        if not params:
            return

        self._ensure_dll()

        if self._bridge:
            # Send params as {str(id): value} for JSON serialization
            self._bridge_call(
                "set_config",
                channel_id=channel_id,
                ioctl_id=SET_CONFIG,
                params={str(k): v for k, v in params.items()},
            )
            logger.debug(
                "Set %d config param(s) on channel %d via bridge",
                len(params),
                channel_id,
            )
            return

        config_array = (SCONFIG * len(params))()
        for i, (param_id, value) in enumerate(params.items()):
            config_array[i].Parameter = param_id
            config_array[i].Value = value

        config_list = SCONFIG_LIST()
        config_list.NumOfParams = len(params)
        config_list.ConfigPtr = ctypes.cast(config_array, POINTER(SCONFIG))

        self.ioctl(channel_id, SET_CONFIG, byref(config_list))
        logger.debug("Set %d config param(s) on channel %d", len(params), channel_id)

    def ioctl(
        self,
        channel_id: int,
        ioctl_id: int,
        input_data=None,
    ) -> None:
        """
        Execute a J2534 IOCTL command on a channel.

        Args:
            channel_id: Channel ID returned by connect().
            ioctl_id: IOCTL command ID (e.g. SET_CONFIG, CLEAR_TX_BUFFER).
            input_data: Optional ctypes pointer/byref to input data structure.
        """
        self._ensure_dll()

        if self._bridge:
            # Bridge only supports simple IOCTL (no ctypes input_data).
            # SET_CONFIG goes through set_config() which has its own bridge path.
            # CLEAR_TX/RX_BUFFER pass None for input_data.
            if input_data is not None:
                raise J2534Error(
                    "Bridge mode does not support IOCTL with ctypes input_data. "
                    "Use set_config() for SET_CONFIG operations."
                )
            self._bridge_call("ioctl", channel_id=channel_id, ioctl_id=ioctl_id)
            return

        func = self._require_func("PassThruIoctl")

        result = func(
            c_ulong(channel_id),
            c_ulong(ioctl_id),
            input_data,
            None,
        )
        self._check_error(result, "PassThruIoctl")


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------


def build_isotp_msg(data: bytes, tx_id: int = CAN_REQUEST_ID) -> PassThruMsg:
    """
    Build an ISO-15765 message for transmission.

    The first 4 bytes of the message Data field contain the CAN arbitration ID
    in big-endian byte order, followed by the payload.

    Args:
        data: Payload bytes to send (UDS request, etc.).
        tx_id: CAN arbitration ID (default 0x7E0).

    Returns:
        A populated PassThruMsg ready for write_msgs().
    """
    msg = PassThruMsg()
    msg.ProtocolID = J2534_PROTOCOL_ISO15765
    msg.TxFlags = ISO15765_TX_FLAGS
    # First 4 bytes are the CAN ID in big-endian
    id_bytes = tx_id.to_bytes(4, "big")
    for i, b in enumerate(id_bytes):
        msg.Data[i] = b
    for i, b in enumerate(data):
        msg.Data[4 + i] = b
    msg.DataSize = 4 + len(data)
    return msg


def _build_can_id_msg(protocol: int, can_id: int) -> PassThruMsg:
    """Build a PassThruMsg containing a 4-byte CAN ID for filter setup."""
    msg = PassThruMsg()
    msg.ProtocolID = protocol
    msg.TxFlags = ISO15765_TX_FLAGS
    id_bytes = can_id.to_bytes(4, "big")
    for i, b in enumerate(id_bytes):
        msg.Data[i] = b
    msg.DataSize = 4
    return msg


def setup_isotp_flow_control(device: J2534Device, channel_id: int) -> int:
    """
    Setup ISO-15765 flow control filter for ECU communication (0x7E0 -> 0x7E8).

    Configures the standard OBD-II flow control filter so that ISO-TP
    multi-frame communication works correctly between the tester (0x7E0)
    and the ECU (0x7E8).

    Args:
        device: An open J2534Device instance.
        channel_id: Channel ID returned by device.connect().

    Returns:
        Filter ID that can be used with device.stop_msg_filter().
    """
    mask_msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, 0xFFFFFFFF)
    pattern_msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, CAN_RESPONSE_ID)
    flow_control_msg = _build_can_id_msg(J2534_PROTOCOL_ISO15765, CAN_REQUEST_ID)

    return device.start_msg_filter(
        channel_id,
        FLOW_CONTROL_FILTER,
        mask_msg,
        pattern_msg,
        flow_control_msg,
    )
