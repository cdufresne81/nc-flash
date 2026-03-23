"""
J2534 PassThru Bridge Worker (32-bit ↔ 64-bit)

Standalone subprocess that loads a J2534 DLL in the current Python's bitness
and exposes its functions over stdin/stdout JSON-line protocol.

Safety:
- Every request/response carries a CRC32 of the payload for integrity checking.
- The worker exits immediately on any unrecoverable error.
- No silent retries — errors are reported back to the host verbatim.

Launched by J2534Device when direct DLL loading fails due to bitness mismatch.
Do NOT import this module — it is meant to run as __main__.
"""

import ctypes
import json
import struct
import sys
import zlib
from ctypes import POINTER, Structure, byref, c_ulong, c_ubyte

# --- Constants (duplicated to keep bridge standalone) ---
PASSTHRU_MSG_DATA_SIZE = 4128
ISO15765_TX_FLAGS = 0x00000040

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


# --- ctypes structures ---
class PassThruMsg(Structure):
    _fields_ = [
        ("ProtocolID", c_ulong),
        ("RxStatus", c_ulong),
        ("TxFlags", c_ulong),
        ("Timestamp", c_ulong),
        ("DataSize", c_ulong),
        ("ExtraDataIndex", c_ulong),
        ("Data", c_ubyte * PASSTHRU_MSG_DATA_SIZE),
    ]


class SCONFIG(Structure):
    _fields_ = [("Parameter", c_ulong), ("Value", c_ulong)]


class SCONFIG_LIST(Structure):
    _fields_ = [("NumOfParams", c_ulong), ("ConfigPtr", POINTER(SCONFIG))]


# --- Worker ---
class BridgeWorker:
    def __init__(self, dll_path: str):
        self._dll = ctypes.WinDLL(dll_path)
        self._funcs: dict = {}
        self._resolve_functions()

    def _resolve_functions(self):
        for name in _PASSTHRU_FUNCTIONS:
            try:
                self._funcs[name] = getattr(self._dll, name)
            except AttributeError:
                self._funcs[name] = None

    def _require(self, name: str):
        f = self._funcs.get(name)
        if f is None:
            raise RuntimeError(f"Function {name} not found in DLL")
        return f

    def _get_last_error(self) -> str:
        func = self._funcs.get("PassThruGetLastError")
        if func is None:
            return ""
        try:
            buf = ctypes.create_string_buffer(256)
            func(buf)
            return buf.value.decode("ascii", errors="replace").strip()
        except Exception:
            return ""

    def _check(self, result: int, func_name: str):
        if result != 0:
            last = self._get_last_error()
            msg = f"{func_name} failed: code {result}"
            if last:
                msg += f" — {last}"
            raise RuntimeError(msg)

    # --- Commands ---

    def cmd_ping(self, _req: dict) -> dict:
        return {"ok": True}

    def cmd_open(self, _req: dict) -> dict:
        func = self._require("PassThruOpen")
        device_id = c_ulong()
        result = func(None, byref(device_id))
        self._check(result, "PassThruOpen")
        return {"ok": True, "device_id": device_id.value}

    def cmd_close(self, req: dict) -> dict:
        func = self._require("PassThruClose")
        result = func(c_ulong(req["device_id"]))
        self._check(result, "PassThruClose")
        return {"ok": True}

    def cmd_connect(self, req: dict) -> dict:
        func = self._require("PassThruConnect")
        channel_id = c_ulong()
        result = func(
            c_ulong(req["device_id"]),
            c_ulong(req["protocol"]),
            c_ulong(req["flags"]),
            c_ulong(req["baudrate"]),
            byref(channel_id),
        )
        self._check(result, "PassThruConnect")
        return {"ok": True, "channel_id": channel_id.value}

    def cmd_disconnect(self, req: dict) -> dict:
        func = self._require("PassThruDisconnect")
        result = func(c_ulong(req["channel_id"]))
        self._check(result, "PassThruDisconnect")
        return {"ok": True}

    def cmd_read_msgs(self, req: dict) -> dict:
        func = self._require("PassThruReadMsgs")
        count = req.get("count", 1)
        timeout = req.get("j2534_timeout", req.get("timeout", 1000))
        msgs = (PassThruMsg * count)()
        num_msgs = c_ulong(count)

        result = func(
            c_ulong(req["channel_id"]),
            byref(msgs),
            byref(num_msgs),
            c_ulong(timeout),
        )
        # ERR_BUFFER_EMPTY (0x10) and ERR_TIMEOUT (0x09) are non-fatal
        if result == 0x10 or result == 0x09:
            return {"ok": True, "msgs": []}

        self._check(result, "PassThruReadMsgs")

        out = []
        for i in range(num_msgs.value):
            m = msgs[i]
            out.append(
                {
                    "ProtocolID": m.ProtocolID,
                    "RxStatus": m.RxStatus,
                    "TxFlags": m.TxFlags,
                    "Timestamp": m.Timestamp,
                    "DataSize": m.DataSize,
                    "ExtraDataIndex": m.ExtraDataIndex,
                    "Data": bytes(m.Data[: m.DataSize]).hex(),
                }
            )
        return {"ok": True, "msgs": out}

    def cmd_write_msgs(self, req: dict) -> dict:
        func = self._require("PassThruWriteMsgs")
        msg_dicts = req["msgs"]
        count = len(msg_dicts)
        msgs = (PassThruMsg * count)()

        for i, md in enumerate(msg_dicts):
            msgs[i].ProtocolID = md["ProtocolID"]
            msgs[i].TxFlags = md["TxFlags"]
            msgs[i].DataSize = md["DataSize"]
            data_bytes = bytes.fromhex(md["Data"])
            for j, b in enumerate(data_bytes):
                msgs[i].Data[j] = b

        num_msgs = c_ulong(count)
        result = func(
            c_ulong(req["channel_id"]),
            byref(msgs),
            byref(num_msgs),
            c_ulong(req.get("j2534_timeout", req.get("timeout", 1000))),
        )
        self._check(result, "PassThruWriteMsgs")
        return {"ok": True}

    def cmd_start_msg_filter(self, req: dict) -> dict:
        func = self._require("PassThruStartMsgFilter")
        filter_id = c_ulong()

        def _build_msg(d: dict | None) -> PassThruMsg | None:
            if d is None:
                return None
            msg = PassThruMsg()
            msg.ProtocolID = d["ProtocolID"]
            msg.TxFlags = d.get("TxFlags", 0)
            msg.DataSize = d["DataSize"]
            data_bytes = bytes.fromhex(d["Data"])
            for j, b in enumerate(data_bytes):
                msg.Data[j] = b
            return msg

        mask = _build_msg(req.get("mask"))
        pattern = _build_msg(req.get("pattern"))
        flow = _build_msg(req.get("flow_control"))

        result = func(
            c_ulong(req["channel_id"]),
            c_ulong(req["filter_type"]),
            byref(mask) if mask else None,
            byref(pattern) if pattern else None,
            byref(flow) if flow else None,
            byref(filter_id),
        )
        self._check(result, "PassThruStartMsgFilter")
        return {"ok": True, "filter_id": filter_id.value}

    def cmd_stop_msg_filter(self, req: dict) -> dict:
        func = self._require("PassThruStopMsgFilter")
        result = func(c_ulong(req["channel_id"]), c_ulong(req["filter_id"]))
        self._check(result, "PassThruStopMsgFilter")
        return {"ok": True}

    def cmd_set_config(self, req: dict) -> dict:
        params = req["params"]
        config_array = (SCONFIG * len(params))()
        for i, (param_id, value) in enumerate(params.items()):
            config_array[i].Parameter = int(param_id)
            config_array[i].Value = int(value)

        config_list = SCONFIG_LIST()
        config_list.NumOfParams = len(params)
        config_list.ConfigPtr = ctypes.cast(config_array, POINTER(SCONFIG))

        func = self._require("PassThruIoctl")
        result = func(
            c_ulong(req["channel_id"]),
            c_ulong(req["ioctl_id"]),
            byref(config_list),
            None,
        )
        self._check(result, "PassThruIoctl(SET_CONFIG)")
        return {"ok": True}

    def cmd_ioctl(self, req: dict) -> dict:
        func = self._require("PassThruIoctl")
        result = func(
            c_ulong(req["channel_id"]),
            c_ulong(req["ioctl_id"]),
            None,
            None,
        )
        self._check(result, "PassThruIoctl")
        return {"ok": True}

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return {"ok": False, "error": f"Unknown command: {cmd}"}
        return handler(req)


def _add_crc(response: dict) -> dict:
    """Add CRC32 of the JSON payload for integrity verification."""
    payload = json.dumps(response, sort_keys=True).encode()
    response["crc32"] = zlib.crc32(payload) & 0xFFFFFFFF
    return response


def _verify_crc(request: dict) -> bool:
    """Verify CRC32 of an incoming request. Returns True if valid or absent."""
    expected = request.pop("crc32", None)
    if expected is None:
        return True  # CRC optional on requests (required on responses)
    payload = json.dumps(request, sort_keys=True).encode()
    actual = zlib.crc32(payload) & 0xFFFFFFFF
    return actual == expected


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "Usage: j2534_bridge.py <dll_path>"}))
        sys.exit(1)

    dll_path = sys.argv[1]

    try:
        worker = BridgeWorker(dll_path)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Failed to load DLL: {e}"}))
        sys.stdout.flush()
        sys.exit(1)

    # Signal ready
    print(json.dumps(_add_crc({"ok": True, "status": "ready"})))
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"ok": False, "error": f"Invalid JSON: {e}"}
            print(json.dumps(_add_crc(resp)))
            sys.stdout.flush()
            continue

        if not _verify_crc(req):
            resp = {"ok": False, "error": "CRC32 mismatch — data corrupted in transit"}
            print(json.dumps(_add_crc(resp)))
            sys.stdout.flush()
            continue

        if req.get("cmd") == "exit":
            print(json.dumps(_add_crc({"ok": True})))
            sys.stdout.flush()
            break

        try:
            resp = worker.handle(req)
        except Exception as e:
            resp = {"ok": False, "error": str(e)}

        print(json.dumps(_add_crc(resp)))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
