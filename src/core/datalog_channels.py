"""Datalog channels: what the Datalogger polls, and how a reply becomes a value.

One channel list serves both polling engines:

- the host engine (Tactrix / OBDX), where NC Flash sends each request itself
  and decodes the reply with :meth:`Channel.decode`;
- the WiCAN engine, where NC Flash hands the list to the firmware poller as
  ``auto_pid.json`` rows built by :func:`to_firmware_row`, and the firmware
  decodes with its own expression language.

A channel's ``formula`` is plain infix arithmetic on ``x`` (``+ - * /``,
parentheses, numbers, unary minus), the one subset both engines can run. ``x``
is the raw value read from the reply: ``size`` bytes starting ``offset`` bytes
into the data, big-endian, unsigned unless ``kind`` says ``signed`` or
``float``, or a single bit when ``bit`` is set.

Pure data and arithmetic: no Qt, no ``src.ecu`` import.
"""

from __future__ import annotations

import ast
import math
import struct
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Callable, Optional

MODE_OBD = 0x01  # OBD-II current data (PID)
MODE_DID = 0x22  # ReadDataByIdentifier (Mazda DID)
MODE_MEM = 0x23  # ReadMemoryByAddress (needs the patched ROM)
MODES = (MODE_OBD, MODE_DID, MODE_MEM)
MODE_LABELS = {MODE_OBD: "01", MODE_DID: "22", MODE_MEM: "23"}

KINDS = ("unsigned", "signed", "float")

#: CSV column names are cut at 47 characters by the firmware; the WiCAN web
#: page refuses 32 and up. Keep under both.
MAX_NAME_LEN = 31
#: The WiCAN web page refuses expressions of 64 characters and up.
MAX_FIRMWARE_EXPRESSION_LEN = 63
MAX_EVERY = 64  # firmware AUTOPID_MAX_SAMPLE_EVERY

#: The firmware poller reads one CAN frame per reply, so a channel's bytes must
#: sit inside that first frame. First data byte index in the frame (B0 is the
#: ISO-TP length byte) and how many data bytes follow, per mode.
_FIRMWARE_FIRST_BYTE = {MODE_OBD: 3, MODE_DID: 4, MODE_MEM: 2}
_FIRMWARE_DATA_BYTES = {MODE_OBD: 5, MODE_DID: 4, MODE_MEM: 6}

#: ECU reply cost per request, measured on the bench ECU (Oct 2026): standard
#: OBD answers in ~1.2 ms, every other service in ~8 ms. Used for the rate
#: estimate shown before logging starts.
REQUEST_COST_MS = {MODE_OBD: 1.2, MODE_DID: 8.0, MODE_MEM: 8.0}


#: Column names the CSV already uses (host engine: Time; WiCAN: the rest).
RESERVED_NAMES = {"time", "timestamp_ms", "datetime", "batt_v", "mark"}

#: Mode 0x23 reads are limited to the ECU's RAM, where the patched ROM's live
#: variables are, at an address aligned to the read size: an unmapped or
#: misaligned access could reset the ECU while the engine runs.
RAM_START = 0xFFFF0000
RAM_END = 0xFFFFBFFF  # SH7058 RAM; above it are peripheral registers


class ChannelError(ValueError):
    """A channel definition that can't be polled or sent to the WiCAN."""


def _check_memory_read(channel: "Channel") -> None:
    size = channel.read_size
    if not RAM_START <= channel.ident <= RAM_END - size + 1:
        raise ChannelError(
            f"{channel.name}: mode 0x23 reads RAM only "
            f"(0x{RAM_START:08X} to 0x{RAM_END:08X})."
        )
    align = 4 if size >= 4 else (2 if size >= 2 else 1)
    if channel.ident % align:
        raise ChannelError(
            f"{channel.name}: a {size}-byte read must start at an address that "
            f"is a multiple of {align}."
        )


@dataclass
class Channel:
    name: str
    mode: int
    ident: int  # PID, DID or 32-bit address
    size: int = 1  # raw value bytes (for mode 0x23: the bytes read)
    offset: int = 0  # bytes into the reply data where the raw value starts
    kind: str = "unsigned"
    bit: Optional[int] = None  # 0-7: x is this bit of the byte at offset
    formula: str = "x"
    unit: str = ""
    min: Optional[float] = None  # out-of-range samples are dropped
    max: Optional[float] = None
    every: int = 1  # poll every Nth sweep
    enabled: bool = True
    description: str = ""
    builtin: bool = field(default=False, compare=False)

    # --- validation ---------------------------------------------------------

    def validate(self) -> None:
        """Raise :class:`ChannelError` if this channel can't be polled."""
        if not self.name or not self.name.strip():
            raise ChannelError("The channel needs a name.")
        if len(self.name) > MAX_NAME_LEN:
            raise ChannelError(
                f"{self.name}: the name is longer than {MAX_NAME_LEN} characters."
            )
        if any(c in self.name for c in ',"\r\n'):
            raise ChannelError(f"{self.name}: the name can't contain , or quotes.")
        if self.name.strip().lower() in RESERVED_NAMES:
            raise ChannelError(f"{self.name}: that name is used by the log itself.")
        if self.mode not in MODES:
            raise ChannelError(f"{self.name}: mode must be 0x01, 0x22 or 0x23.")
        limit = {MODE_OBD: 0xFF, MODE_DID: 0xFFFF, MODE_MEM: 0xFFFFFFFF}[self.mode]
        if not 0 <= self.ident <= limit:
            raise ChannelError(f"{self.name}: identifier out of range for the mode.")
        if self.kind not in KINDS:
            raise ChannelError(f"{self.name}: type must be one of {', '.join(KINDS)}.")
        if self.size not in (1, 2, 3, 4):
            raise ChannelError(f"{self.name}: size must be 1 to 4 bytes.")
        if self.kind == "float" and self.size != 4:
            raise ChannelError(f"{self.name}: a float is 4 bytes.")
        if self.kind == "signed" and self.size == 3:
            # The WiCAN firmware doesn't sign-extend 24-bit values.
            raise ChannelError(f"{self.name}: a signed value is 1, 2 or 4 bytes.")
        if self.bit is not None:
            if not 0 <= self.bit <= 7:
                raise ChannelError(f"{self.name}: bit must be 0 to 7.")
            if self.size != 1 or self.kind != "unsigned":
                raise ChannelError(f"{self.name}: a bit channel reads one byte.")
        if self.offset < 0:
            raise ChannelError(f"{self.name}: offset can't be negative.")
        if self.offset + self.size > _FIRMWARE_DATA_BYTES[self.mode]:
            raise ChannelError(
                f"{self.name}: bytes {self.offset}..{self.offset + self.size - 1} "
                f"are past the first {_FIRMWARE_DATA_BYTES[self.mode]} data bytes "
                f"a mode {MODE_LABELS[self.mode]} reply carries."
            )
        if self.mode == MODE_MEM:
            _check_memory_read(self)
        if not 1 <= self.every <= MAX_EVERY:
            raise ChannelError(f"{self.name}: 'every' must be 1 to {MAX_EVERY}.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ChannelError(f"{self.name}: min is above max.")
        compile_formula(self.formula)  # raises ChannelError

    @property
    def read_size(self) -> int:
        """Bytes requested by a mode 0x23 read."""
        return self.offset + self.size

    # --- decoding -----------------------------------------------------------

    def raw_value(self, data: bytes) -> float:
        """The raw ``x`` from the reply data (after the PID/DID echo)."""
        chunk = data[self.offset : self.offset + self.size]
        if len(chunk) < self.size:
            raise ChannelError(f"{self.name}: reply too short ({len(data)} data bytes)")
        if self.bit is not None:
            return float((chunk[0] >> self.bit) & 1)
        if self.kind == "float":
            return float(struct.unpack(">f", chunk)[0])
        return float(int.from_bytes(chunk, "big", signed=self.kind == "signed"))

    def decode(self, data: bytes) -> Optional[float]:
        """The scaled value, or None when it falls outside min/max."""
        value = compile_formula(self.formula)(self.raw_value(data))
        if not math.isfinite(value):
            return None
        if self.min is not None and value < self.min:
            return None
        if self.max is not None and value > self.max:
            return None
        return value

    # --- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("builtin", None)
        return d

    @classmethod
    def from_dict(cls, d: dict, *, builtin: bool = False) -> "Channel":
        known = {f for f in cls.__dataclass_fields__ if f != "builtin"}
        values = {k: v for k, v in d.items() if k in known}
        return cls(**values, builtin=builtin)


# --- formulas -----------------------------------------------------------------

_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}


def _check_formula_node(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _check_formula_node(node.body)
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        _check_formula_node(node.left)
        _check_formula_node(node.right)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        _check_formula_node(node.operand)
    elif isinstance(node, ast.Constant) and type(node.value) in (int, float):
        pass
    elif isinstance(node, ast.Name) and node.id == "x":
        pass
    else:
        raise ChannelError(
            "A formula may only use x, numbers, + - * / and parentheses."
        )


_compiled: dict[str, Callable[[float], float]] = {}


def compile_formula(formula: str) -> Callable[[float], float]:
    """Compile an ``x`` formula once; raise :class:`ChannelError` if not allowed.

    Deliberately not ``rom_reader._is_safe_numpy_expr``: that one also allows
    ``**`` and ``//``, which the WiCAN firmware can't run.
    """
    fn = _compiled.get(formula)
    if fn is not None:
        return fn
    try:
        tree = ast.parse(formula.strip() or "x", mode="eval")
    except SyntaxError as exc:
        raise ChannelError(f"Formula {formula!r} is not valid: {exc.msg}") from exc
    _check_formula_node(tree)
    code = compile(tree, "<datalog formula>", "eval")

    def fn(x: float, _code=code) -> float:
        try:
            return float(eval(_code, {"__builtins__": {}}, {"x": x}))
        except ZeroDivisionError:
            return math.nan

    _compiled[formula] = fn
    return fn


def _fixed(value: float) -> str:
    """A number in fixed-point (the firmware rejects ``1e-05``)."""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    text = format(Decimal(repr(float(value))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _emit(node: ast.AST, raw: str) -> str:
    if isinstance(node, ast.Expression):
        return _emit(node.body, raw)
    if isinstance(node, ast.BinOp):
        left = _emit(node.left, raw)
        right = _emit(node.right, raw)
        return f"({left}{_BINOPS[type(node.op)]}{right})"
    if isinstance(node, ast.UnaryOp):
        inner = _emit(node.operand, raw)
        # The firmware has no unary minus.
        return f"(0-{inner})" if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.Constant):
        return _fixed(node.value)
    return raw  # x


def _strip_outer(expr: str) -> str:
    """Drop one pair of parentheses wrapping the whole expression."""
    if not (expr.startswith("(") and expr.endswith(")")):
        return expr
    depth = 0
    for i, c in enumerate(expr):
        depth += c == "("
        depth -= c == ")"
        if depth == 0 and i < len(expr) - 1:
            return expr
    return expr[1:-1]


# --- WiCAN firmware rows ------------------------------------------------------


def firmware_raw_token(channel: Channel) -> str:
    """The firmware expression for the raw ``x`` of *channel*."""
    n = _FIRMWARE_FIRST_BYTE[channel.mode] + channel.offset
    if channel.bit is not None:
        # The form the firmware's own defaults use ("B4&1"), parenthesized so
        # a formula around it keeps the bitwise step first.
        if channel.bit == 0:
            return f"(B{n}&1)"
        return f"((B{n}>>{channel.bit})&1)"
    if channel.kind == "float":
        return f"F{n}"
    if channel.kind == "signed":
        if channel.size == 1:
            return f"S{n}"
        return f"[S{n}:S{n + channel.size - 1}]"
    if channel.size == 1:
        return f"B{n}"
    if channel.size == 2:
        # The form the firmware's own defaults use.
        return f"((B{n}*256)+B{n + 1})"
    return f"[B{n}:B{n + channel.size - 1}]"


def firmware_expression(channel: Channel) -> str:
    """*channel*'s formula in the firmware's byte-token expression language."""
    tree = ast.parse(channel.formula.strip() or "x", mode="eval")
    _check_formula_node(tree)
    expr = _strip_outer(_emit(tree, firmware_raw_token(channel)))
    if len(expr) > MAX_FIRMWARE_EXPRESSION_LEN:
        raise ChannelError(
            f"{channel.name}: the formula is too long for the WiCAN "
            f"({len(expr)} characters, max {MAX_FIRMWARE_EXPRESSION_LEN})."
        )
    return expr


def firmware_pid(channel: Channel) -> str:
    """The firmware ``PID`` string: mode + identifier (+ size) + frames hint."""
    if channel.mode == MODE_OBD:
        return f"01{channel.ident:02X}1"
    if channel.mode == MODE_DID:
        return f"22{channel.ident:04X}1"
    return f"23{channel.ident:08X}{channel.read_size:04X}1"


def to_firmware_row(channel: Channel) -> dict:
    """An ``auto_pid.json`` ``pids`` row for the WiCAN firmware poller."""
    channel.validate()
    row = {
        "Name": channel.name,
        "Mode": MODE_LABELS[channel.mode],
        "PID": firmware_pid(channel),
        "Expression": firmware_expression(channel),
        "Unit": channel.unit,
        "Class": "",
        "MinValue": "" if channel.min is None else _fixed(channel.min),
        "MaxValue": "" if channel.max is None else _fixed(channel.max),
        "SampleEvery": channel.every,
        "enabled": channel.enabled,
    }
    if channel.description:
        row["description"] = channel.description
    return row


# --- built-in catalog ---------------------------------------------------------


def _c(name, mode, ident, size=1, formula="x", unit="", every=1, **kw) -> Channel:
    return Channel(
        name=name,
        mode=mode,
        ident=ident,
        size=size,
        formula=formula,
        unit=unit,
        every=every,
        builtin=True,
        **kw,
    )


#: Channels for the MX-5 NC ECU, from the bench-checked logcfg.txt set and the
#: WiCAN firmware's default table. Mode 0x23 channels need the patched ROM and
#: a known RAM address, so none are built in: add them as custom channels.
#: ``enabled`` marks the default selection.
BUILTIN_CHANNELS: tuple[Channel, ...] = (
    _c("RPM", MODE_OBD, 0x0C, 2, "x*0.25", "rpm", description="Engine speed"),
    _c("LOAD", MODE_OBD, 0x43, 2, "x*0.0039215684", "g/rev", description="Load"),
    _c("MAF", MODE_OBD, 0x10, 2, "x*0.01", "g/s", description="Mass air flow"),
    _c("SPARKADV", MODE_OBD, 0x0E, 1, "x*0.5-64", "deg", description="Ignition advance"),
    _c("EQ_RATIO_ACT", MODE_OBD, 0x34, 2, "x*0.000030517578", "lambda",
       description="Measured lambda (front O2)"),
    _c("ECT", MODE_OBD, 0x05, 1, "x-40", "C", every=8, description="Coolant temperature"),
    _c("IAT", MODE_OBD, 0x0F, 1, "x-40", "C", every=8, description="Intake air temperature"),
    _c("APP", MODE_DID, 0x1340, 1, "x*0.39215684", "%", description="Accelerator pedal"),
    _c("FUEL_PW", MODE_DID, 0x1410, 2, "x*0.008", "ms", description="Injector pulse width"),
    _c("KNOCKR", MODE_DID, 0x1746, 1, "x*0.3521126761", "deg", description="Knock retard"),
    _c("VSS", MODE_OBD, 0x0D, 1, "x", "km/h", enabled=False, description="Vehicle speed"),
    _c("MAP", MODE_OBD, 0x0B, 1, "x", "kPa", enabled=False,
       description="Manifold absolute pressure"),
    _c("TPS", MODE_OBD, 0x11, 1, "x*0.39215686", "%", enabled=False,
       description="Throttle position"),
    _c("EQ_RATIO_DSD", MODE_OBD, 0x44, 2, "x*0.000030517578", "lambda", every=2,
       enabled=False, description="Commanded lambda"),
    _c("STFT", MODE_OBD, 0x06, 1, "x*0.78125-100", "%", every=8, enabled=False,
       description="Short-term fuel trim"),
    _c("LTFT", MODE_OBD, 0x07, 1, "x*0.78125-100", "%", every=8, enabled=False,
       description="Long-term fuel trim"),
    _c("FUELSYS", MODE_OBD, 0x03, 2, "x*0.00390625", "", every=8, enabled=False,
       description="Fuel system status (open/closed loop)"),
    _c("AAT", MODE_OBD, 0x46, 1, "x-40", "C", every=16, enabled=False,
       description="Ambient air temperature"),
    _c("BARO", MODE_OBD, 0x33, 1, "x", "kPa", every=16, enabled=False,
       description="Barometric pressure"),
    _c("VPWR", MODE_OBD, 0x42, 2, "x*0.001", "V", every=16, enabled=False,
       description="Control module voltage"),
    _c("VCT_ACT", MODE_DID, 0x16CD, 2, "x*0.0625", "deg", every=4, enabled=False,
       description="Variable cam timing, actual"),
    _c("MAF_V", MODE_DID, 0x1177, 2, "x*0.000244140625", "V", enabled=False,
       description="MAF sensor voltage"),
    _c("HIDET_SW", MODE_DID, 0x1706, 1, bit=0, unit="on/off", every=8, enabled=False,
       description="High detonation switch"),
    _c("IMTV_SW", MODE_DID, 0x160D, 1, bit=1, unit="on/off", every=8, enabled=False,
       description="Intake manifold tuning valve"),
)  # fmt: skip


def builtin_channels() -> list[Channel]:
    """Fresh copies of the built-in catalog (callers may change them)."""
    return [replace(c) for c in BUILTIN_CHANNELS]


# --- the user's channel list --------------------------------------------------


def channels_from_saved(saved: Optional[dict]) -> list[Channel]:
    """Rebuild the user's list: built-ins (with their saved on/off and every-N),
    then custom channels. Built-ins added in a later release appear with their
    default selection; a damaged saved entry is skipped, never fatal."""
    saved = saved if isinstance(saved, dict) else {}
    prefs = saved.get("builtin") if isinstance(saved.get("builtin"), dict) else {}
    channels = builtin_channels()
    for ch in channels:
        pref = prefs.get(ch.name)
        if isinstance(pref, dict):
            if isinstance(pref.get("enabled"), bool):
                ch.enabled = pref["enabled"]
            every = pref.get("every")
            if isinstance(every, int) and 1 <= every <= MAX_EVERY:
                ch.every = every
    names = {c.name for c in channels}
    for d in saved.get("custom") or []:
        if not isinstance(d, dict):
            continue
        try:
            ch = Channel.from_dict(d)
            ch.validate()
        except (TypeError, ChannelError):
            continue
        if ch.name in names:
            continue
        names.add(ch.name)
        channels.append(ch)
    return channels


def channels_to_saved(channels: list[Channel]) -> dict:
    """The storable form of :func:`channels_from_saved`'s list."""
    return {
        "version": 1,
        "builtin": {
            c.name: {"enabled": c.enabled, "every": c.every}
            for c in channels
            if c.builtin
        },
        "custom": [c.to_dict() for c in channels if not c.builtin],
    }


def estimated_rows_per_second(channels: list[Channel]) -> float:
    """Rough sweep rate of the enabled channels at the ECU's reply speed.

    Ignores mode 0x01 packing, so the host engine usually does a bit better.
    """
    ms = sum(REQUEST_COST_MS[c.mode] / c.every for c in channels if c.enabled)
    return 1000.0 / ms if ms > 0 else 0.0
