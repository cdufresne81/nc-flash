"""WiCAN fast flash (the firmware's ``K`` command): the dongle rewrites only the
ECU blocks that differ, by itself, from a package uploaded to its SD card.

Contract: the firmware's ``docs/internals/fast_flash.md`` (nc-flash-wican-fw,
``main/ncflash_fastflash.c``). In short:

1. ``GET /host_caps`` must report ``fast_flash`` (and ``ff_live`` for ``L``/``R``).
2. Upload ``<stem>.ffp`` and ``<stem>.json`` with ``POST /upload/sd/<name>``.
3. For ``C`` and ``L`` the PC first brings the ECU to its flash session
   (programming session, security access, generation check, flash counter),
   exactly as before the SD-staged flash. ``R`` needs none.
4. Send ``K<mode><stem>.ffp\\r`` on the SLCAN port (35001) and read the status
   lines until ``NCFFDONE`` or ``FFERR``.

The firmware streams a line at least every 5 s. Before its point of no return
it gives up when the PC does not take a line for 2 s, so the reader here keeps
draining the socket continuously and runs callbacks inline (they must return
quickly). From the point of no return on, the flash finishes without the PC; if
the link drops then, :meth:`WiCANFastFlashClient.run` follows the op through
``GET /fast_flash`` until it ends.

Headless: standard library only.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from .exceptions import ECUError
from .fast_flash_package import MIN_KERNEL_VERSION, FastFlashPackage
from .wican_config import WiCANConfigurator
from .wican_http import WiCANHttpError, get_json
from .wican_sd_upload import UploadResult, WiCANSdUploader
from .wican_transport import WiCANError

logger = logging.getLogger(__name__)

# --- wire constants (must match the firmware) ----------------------------------------

FAST_FLASH_CMD = "K"
MODES = ("D", "C", "L", "R")
LIVE_MODES = ("L", "R")
#: ``/host_caps`` "fast_flash" value this client speaks.
FAST_FLASH_REV = 1
#: Oldest firmware fast-read rev (``ncfr_rev``) shipped with the ``K`` command.
FAST_FLASH_MIN_NCFR_REV = 6
#: Hardware generations the fast-flash kernel is cleared for.
ALLOWED_GENERATIONS = frozenset({"NC2"})

#: No line for this long = the firmware is gone (it sends one every <= 5 s).
IDLE_TIMEOUT_S = 30.0
#: Whole op ceiling as observed by the PC (the firmware bounds each request).
OP_TIMEOUT_S = 45 * 60.0
#: How long the end of an op is followed over HTTP after the stream dropped.
FOLLOW_UP_S = 30 * 60.0
FOLLOW_UP_POLL_S = 2.0
#: How long the follow-up waits for this package's op to show up before it
#: concludes that the ``K`` never ran (the firmware may still be taking it).
NOT_STARTED_GRACE_S = 10.0


# --- typed events ------------------------------------------------------------------


@dataclass(frozen=True)
class FFEvent:
    """One status line. ``kind``: sync step kern probe cmp prog note res done err."""

    kind: str
    raw: str
    fields: Dict[str, str] = field(default_factory=dict)
    #: ``step``: the phase; ``note``: what; ``res``: the JSON text.
    text: str = ""

    def num(self, key: str, default: int = 0, base: int = 10) -> int:
        try:
            return int(self.fields[key], base)
        except (KeyError, ValueError):
            return default


def _blocks(value: str) -> List[int]:
    if not value or value == "-":
        return []
    out = []
    for part in value.split(","):
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def _kv(parts: List[str]) -> Dict[str, str]:
    out = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
    return out


def parse_line(line: str) -> Optional[FFEvent]:
    """A firmware status line as an event; None for anything else (ignored)."""
    line = line.strip()
    if not line:
        return None
    parts = line.split()
    head = parts[0]
    if head == "NCFFSYNC":
        return FFEvent("sync", line)
    if head == "NCFFSTEP":
        return FFEvent("step", line, text=parts[1] if len(parts) > 1 else "")
    if head == "NCFFKERN":
        return FFEvent("kern", line, _kv(parts[1:]))
    if head == "NCFFPROBE":
        return FFEvent("probe", line, _kv(parts[1:]))
    if head == "NCFFCMP":
        return FFEvent("cmp", line, _kv(parts[1:]))
    if head == "NCFFPROG":
        f = _kv(parts[2:])
        if len(parts) > 1 and "/" in parts[1]:
            f["done"], f["total"] = parts[1].split("/", 1)
        return FFEvent("prog", line, f)
    if head == "NCFFNOTE":
        return FFEvent(
            "note", line, _kv(parts[2:]), text=parts[1] if len(parts) > 1 else ""
        )
    if head == "NCFFRES":
        return FFEvent("res", line, text=line[len("NCFFRES") :].strip())
    if head == "NCFFDONE":
        return FFEvent("done", line, _kv(parts[1:]))
    if head == "FFERR":
        return FFEvent("err", line, _kv(parts[1:]))
    return None


# --- what a failure means for the ECU ---------------------------------------------


class EcuState(Enum):
    """What the ECU holds after a failed op, as far as the firmware knows."""

    UNTOUCHED = "untouched"  # runs its application (re-authenticate first)
    NO_APPLICATION = "no_application"  # waits in its boot loader: resume (R)
    MAY_BE_COMPLETE = "may_be_complete"  # st 61: flash again to confirm
    WRONG_ENTRY = "wrong_entry"  # st 62: R now, do NOT cycle the ignition
    BUSY = "busy"  # st 9/14: another op owns the adapter; do not cycle the ignition
    NOT_STARTED = "not_started"  # the adapter has no record of this op
    UNKNOWN = "unknown"


#: Stage -> short cause, as the firmware's FFERR table.
STAGE_TEXT: Dict[int, str] = {
    1: "the package is not on the adapter's SD card",
    2: "the package description (manifest) is missing or unreadable",
    3: "the package breaks a safety rule",
    4: "the package size is wrong",
    5: "the package is damaged (checksum mismatch)",
    6: "the adapter is out of memory",
    7: "this adapter firmware cannot erase (compare-only build)",
    8: "the adapter refused the command",
    9: "another flash or read is using the adapter",
    14: "the adapter is restarting or updating its firmware",
    15: "NC Flash stopped answering the adapter",
    20: "the ECU refused the start of the transfer",
    21: "the ECU refused part of the flash program",
    22: "the upload of the flash program lost its place",
    23: "the flash program did not answer",
    24: "the ECU runs a different flash program version",
    25: "the flash program arrived damaged in the ECU",
    26: "a flash program from an earlier attempt is still running in the ECU",
    30: "the ECU stopped answering during the comparison",
    31: "the ROM's boot block does not match this ECU (wrong generation?)",
    32: "the ECU could not prepare its flash routines",
    33: "the link test to the ECU failed",
    34: "the ECU refused to start erasing (flash counter or flash not ready)",
    40: "an erase failed",
    41: "the ECU refused a write range",
    42: "the ECU refused the data of a block twice, or stopped answering",
    43: "the ECU did not close a write range",
    50: "the ECU stopped answering during the final check",
    51: "a block did not verify",
    60: "the final page did not verify",
    61: "the link was lost while checking the final page",
    62: "the final page is wrong and could not be erased again",
}

#: Stages that come before the op owns the bus (another op may be running).
BUSY_STAGES = frozenset({9, 14})
#: Stages that end before the ECU's flash was touched, when app is not "0".
_UNTOUCHED_STAGES = frozenset({20, 21, 22, 24, 25, 30, 31, 32, 33, 34})


def ecu_state(err: FFEvent) -> EcuState:
    """The ECU's state after ``err`` (an ``FFERR`` event).

    ``app`` is absent on the firmware's early refusals (before the op owns the
    bus): read as unknown ("u")."""
    st = err.num("st")
    ponr = err.fields.get("ponr") == "1"
    app = err.fields.get("app", "u")
    if st == 62:
        return EcuState.WRONG_ENTRY
    if st == 61:
        return EcuState.MAY_BE_COMPLETE
    if ponr or app == "0":
        return EcuState.NO_APPLICATION
    if st in BUSY_STAGES:
        return EcuState.BUSY
    if app == "1" or st < 20 or st in _UNTOUCHED_STAGES:
        return EcuState.UNTOUCHED
    return EcuState.UNKNOWN


def user_message(err: FFEvent, interrupted_before: bool = False) -> str:
    """Plain-language message for an ``FFERR``: the cause and what to do."""
    st = err.num("st")
    cause = STAGE_TEXT.get(st, f"error stage {st}")
    state = ecu_state(err)
    if state is EcuState.WRONG_ENTRY:
        return (
            f"Dynamic Flash failed: {cause}. Do NOT switch the ignition off. Keep "
            "it on and resume the flash now: the ECU may hold a damaged start "
            "address until it is rewritten."
        )
    if state is EcuState.MAY_BE_COMPLETE:
        return (
            f"Dynamic Flash: {cause}. Every block had verified, so the ECU is "
            "probably complete. Flash again to confirm before driving."
        )
    if st == 26:
        return (
            f"Dynamic Flash: {cause}. Resume that flash instead of starting a new one."
        )
    if state is EcuState.NO_APPLICATION:
        return (
            f"Dynamic Flash stopped: {cause}. The ECU has no working program and is "
            "waiting for a flash. Keep the ignition on and resume; if the ECU was "
            "switched off meanwhile, run a normal full flash."
        )
    if state is EcuState.BUSY:
        return (
            f"Dynamic Flash did not start: {cause}. Wait until the adapter has "
            "finished, then try again. Leave the ignition on meanwhile."
        )
    if st == 23 and interrupted_before:
        return (
            f"Dynamic Flash: {cause}. An earlier flash did not finish, so the ECU "
            "probably has no working program: run a normal full flash."
        )
    if state is EcuState.UNTOUCHED:
        return (
            f"Dynamic Flash did not start: {cause}. The ECU was not changed. "
            "Switch the ignition off and on before trying again."
        )
    return f"Dynamic Flash failed: {cause}. The ECU's state is not known: flash again."


class WiCANFastFlashError(WiCANError):
    """A Dynamic Flash op over the WiCAN failed.

    ``event`` is the ``FFERR`` (streamed, or rebuilt from ``GET /fast_flash``
    for THIS package), None when the end could not be learned.
    ``untouched_proof`` is True only on positive evidence that this op left the
    ECU's flash as it was: a streamed ``FFERR`` with ``ponr=0``, or a status of
    this package that failed with ``ponr`` 0 at both levels, the ECU having an
    application. Only then may an archive set aside be put back.
    """

    def __init__(
        self,
        message: str,
        *,
        event: Optional[FFEvent] = None,
        state: EcuState = EcuState.UNKNOWN,
        ponr: Optional[bool] = None,
        untouched_proof: bool = False,
    ):
        super().__init__(message)
        self.event = event
        self.state = state
        self.ponr = ponr
        self.untouched_proof = untouched_proof

    @property
    def stage(self) -> Optional[int]:
        return self.event.num("st") if self.event else None

    @property
    def app(self) -> str:
        """'1' / '0' / 'u': the ECU's entry pointer as the firmware read it."""
        return self.event.fields.get("app", "u") if self.event else "u"

    @property
    def resume_recommended(self) -> bool:
        """The next flash should be a resume (R): the kernel may still run and the
        ECU may have no complete application."""
        return (
            self.state
            in (EcuState.NO_APPLICATION, EcuState.WRONG_ENTRY, EcuState.MAY_BE_COMPLETE)
            or self.stage == 26
            or self.app == "0"
        )

    @property
    def ignition_cycle_ok(self) -> bool:
        """Advising (or offering) an ignition cycle / ECU reset is safe."""
        return self.state is EcuState.UNTOUCHED


# --- capability gate ---------------------------------------------------------------


def fast_flash_available(
    caps: Optional[dict],
    mode: str,
    rom_data: Optional[bytes],
    kernel,
) -> Tuple[bool, str]:
    """``(ok, reason)``: may the WiCAN fast flash run ``mode`` for this ROM?

    ``caps`` is the ``/host_caps`` reply (None = unknown/old firmware), ``kernel``
    a :class:`~src.ecu.fast_flash_kernel.LoadedKernel` or None.
    """
    if mode not in MODES:
        return False, f"unknown Dynamic Flash mode {mode!r}"
    if not caps:
        return False, "the adapter does not report its capabilities (older firmware)"
    try:
        rev = int(caps.get("fast_flash", 0))
        ncfr = int(caps.get("ncfr_rev", 0))
        live = int(caps.get("ff_live", 0))
    except (TypeError, ValueError):
        return False, "the adapter reported unreadable capabilities"
    if rev < FAST_FLASH_REV:
        return False, "the adapter firmware has no Dynamic Flash"
    if ncfr < FAST_FLASH_MIN_NCFR_REV:
        return False, "the adapter firmware is too old for Dynamic Flash"
    if mode in LIVE_MODES and live != 1:
        return False, "this adapter firmware can only compare, not flash (test build)"
    if mode == "D":
        return True, ""
    if kernel is None:
        return False, "no Dynamic Flash program is installed"
    version = getattr(getattr(kernel, "pin", None), "version", None)
    if version is not None and version < MIN_KERNEL_VERSION:
        return False, "the installed Dynamic Flash program is too old for the adapter"
    if not rom_data:
        return False, "no ROM to flash"
    try:
        from .rom_utils import rom_hardware_generation

        generation = rom_hardware_generation(rom_data)
    except Exception as e:  # noqa: BLE001 - any unreadable ROM refuses
        return False, f"the ROM's generation could not be read ({e})"
    if generation not in ALLOWED_GENERATIONS:
        return False, f"Dynamic Flash has not been tested on an {generation} yet"
    return True, ""


# --- result --------------------------------------------------------------------------


@dataclass
class FastFlashOutcome:
    """A finished op."""

    mode: str
    done: FFEvent
    events: List[FFEvent]
    #: The result as JSON text (the ``NCFFRES`` payload, or the ``result`` of
    #: ``GET /fast_flash`` re-encoded as JSON).
    result: Optional[str] = None
    #: True when the end was learned from ``GET /fast_flash`` after the link dropped.
    via_status: bool = False
    #: The firmware saw the ECU answer its final ``11 01`` (``NCFFNOTE reset ok``).
    #: False when it did not, or when the end was learned over HTTP (not known).
    restart_confirmed: bool = False

    @property
    def nothing_to_do(self) -> bool:
        return self.done.fields.get("nothing") == "1"


ProgressFn = Callable[[str, float], None]
EventFn = Callable[[FFEvent], None]


def _json_text(obj) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def _untouched_proof(mode: str, err: FFEvent) -> bool:
    """Positive evidence that THIS op left the ECU's flash as it was.

    ``ponr=0``, not a refusal before the op owned the bus (st 9/14), and the ECU
    holding an application: ``app`` not "0" in L. In R the ECU was already left
    by an earlier op, so only a read of a programmed entry pointer (``app=1``)
    proves the archive still describes it."""
    if err.fields.get("ponr") != "0" or err.num("st") in BUSY_STAGES:
        return False
    app = err.fields.get("app", "u")
    return app == "1" if mode == "R" else app != "0"


def _status_err(st: dict, name: str) -> FFEvent:
    """A ``failed`` status of THIS package as an FFERR event (app from the result)."""
    e = st.get("err") or {}
    res = st.get("result") or {}
    app = res.get("app", "u") if isinstance(res, dict) else "u"

    def i(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    ponr = "1" if (i(e.get("ponr")) or i(st.get("ponr"))) else "0"
    return FFEvent(
        "err",
        f"(GET /fast_flash: {name} failed)",
        {
            "st": str(i(e.get("st"))),
            "nrc": f"{i(e.get('nrc')) & 0xFF:02X}",
            "a": f"{i(e.get('a')):06X}",
            "blk": str(i(e.get("blk"))),
            "ponr": ponr,
            "app": str(app),
        },
    )


class WiCANFastFlashClient:
    """Talks to one WiCAN for the fast flash.

    ``transport`` is an open :class:`~src.ecu.wican_transport.WiCANTransport`
    on the SLCAN port (35001). The caller holds the bus reservation
    (``WiCANDatalogClient.reserved()``) around :meth:`run`, as for the SD-staged
    flash, and authenticates the ECU before ``C``/``L``.
    """

    def __init__(
        self,
        transport,
        *,
        http_port: int = 80,
        uploader: Optional[WiCANSdUploader] = None,
        configurator: Optional[WiCANConfigurator] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._transport = transport
        self._host = getattr(transport, "host", None)
        self._http_port = http_port
        self._uploader = uploader or WiCANSdUploader(self._host, http_port=http_port)
        self._config = configurator or WiCANConfigurator(
            self._host, http_port=http_port
        )
        self._clock = clock
        self._sleep = sleep

    # --- HTTP ------------------------------------------------------------------------

    def caps(self) -> Optional[dict]:
        """``GET /host_caps`` (None = unknown)."""
        return self._config.host_caps()

    def status(self, timeout_s: float = 5.0) -> dict:
        """``GET /fast_flash``. Raises :class:`WiCANHttpError` on any failure."""
        url = f"http://{self._host}:{self._http_port}/fast_flash"
        try:
            st = get_json(url, timeout_s=timeout_s)
        except WiCANHttpError:
            raise
        except (http.client.HTTPException, OSError, ValueError) as e:
            # get_json does not wrap a reply cut short (IncompleteRead) and friends
            raise WiCANHttpError(f"GET {url} failed: {e}") from e
        if not isinstance(st, dict):
            raise WiCANHttpError(f"GET {url}: not a JSON object")
        return st

    def upload(self, pkg: FastFlashPackage) -> Tuple[UploadResult, UploadResult]:
        """Upload the package and its manifest; each is CRC-verified by the device."""
        a = self._uploader.upload_bytes(
            pkg.name, pkg.data, expected_crc32=pkg.manifest["package_crc32"]
        )
        b = self._uploader.upload_bytes(pkg.manifest_name, pkg.manifest_bytes)
        return a, b

    # --- the op ----------------------------------------------------------------------

    def run(
        self,
        name: str,
        mode: str,
        *,
        on_event: Optional[EventFn] = None,
        progress: Optional[ProgressFn] = None,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        op_timeout_s: float = OP_TIMEOUT_S,
        follow_up_s: float = FOLLOW_UP_S,
    ) -> FastFlashOutcome:
        """Send ``K<mode><name>`` and follow the op to its end.

        Returns on ``NCFFDONE``. Raises :class:`WiCANFastFlashError` on
        ``FFERR`` or when the op's end cannot be learned. ``name`` must be unique
        per op (``staged_name`` is): the HTTP follow-up only accepts a status
        that names this package and mode.
        """
        if mode not in MODES:
            raise WiCANFastFlashError(f"unknown Dynamic Flash mode {mode!r}")
        events: List[FFEvent] = []
        state = {"result": None, "restart": False, "write": False}

        def emit(ev: FFEvent) -> None:
            events.append(ev)
            if ev.kind == "step" and ev.text == "write":
                state["write"] = True
            if ev.kind == "res":
                state["result"] = ev.text
            if ev.kind == "note" and ev.text == "reset":
                state["restart"] = "ok" in ev.raw.split()
            if on_event:
                on_event(ev)
            if progress:
                msg, pct = _progress_of(ev)
                if msg:
                    progress(msg, pct)

        ended_box: List[FFEvent] = []

        def on_line(line: str) -> bool:
            ev = parse_line(line)
            if ev is None:
                return False
            emit(ev)
            if ev.kind in ("done", "err"):
                ended_box.append(ev)
                return True
            return False

        command = f"{FAST_FLASH_CMD}{mode}{name}\r".encode("ascii")
        try:
            # The one socket read loop lives in the transport (fast_flash).
            self._transport.fast_flash(
                command,
                on_line,
                idle_timeout_s=idle_timeout_s,
                op_timeout_s=op_timeout_s,
                clock=self._clock,
            )
        except WiCANFastFlashError:
            raise
        except WiCANError as e:
            logger.warning("Dynamic Flash stream lost (%s); following it over HTTP", e)
        ended = ended_box[0] if ended_box else None
        if ended is not None and ended.kind == "err" and ended.num("st") in BUSY_STAGES:
            # Refused before the op owned the bus. If the op owning it is THIS
            # package (our own earlier K, its stream lost), follow it.
            try:
                st = self.status()
            except WiCANHttpError:
                st = {}
            if st.get("state") == "running" and st.get("name") == name:
                ended = None
            elif st.get("state") == "running":
                other = st.get("name") or "another package"
                raise WiCANFastFlashError(
                    f"Dynamic Flash did not start: the adapter is still running an "
                    f"earlier Dynamic Flash ({other}). Wait for it to finish (the "
                    "adapter's status page shows it) and leave the ignition on.",
                    event=ended,
                    state=EcuState.BUSY,
                    ponr=False,
                )
        if ended is None:
            return self._follow_up(name, mode, events, state, follow_up_s)
        return self._finish(mode, ended, events, state)

    def _finish(self, mode, ended: FFEvent, events, state) -> FastFlashOutcome:
        if ended.kind == "done":
            return FastFlashOutcome(
                mode,
                ended,
                events,
                state["result"],
                restart_confirmed=bool(state["restart"]),
            )
        interrupted = False
        if ended.num("st") == 23:
            try:
                interrupted = self.status().get("state") == "interrupted"
            except WiCANHttpError:
                pass
        es = ecu_state(ended)
        # The FFERR's own ponr decides (it supersedes "NCFFSTEP write", which the
        # firmware also sends before a refused first erase, st 34).
        ponr = ended.fields.get("ponr") == "1"
        proof = _untouched_proof(mode, ended)
        raise WiCANFastFlashError(
            user_message(ended, interrupted) + f" (adapter: {ended.raw})",
            event=ended,
            state=es,
            ponr=ponr,
            untouched_proof=proof,
        )

    def _lost(self, message: str, state: EcuState, ponr: Optional[bool]) -> None:
        raise WiCANFastFlashError(message, state=state, ponr=ponr)

    def _follow_up(self, name, mode, events, state, follow_up_s) -> FastFlashOutcome:
        """The stream dropped (or never started): learn the end of THIS op from
        ``GET /fast_flash``. A status that names another package, or ``idle``,
        is never taken for this op's result."""
        deadline = self._clock() + follow_up_s
        grace_end = self._clock() + NOT_STARTED_GRACE_S
        last_error = None
        seen_running = False
        lost_msg = (
            "Lost contact with the adapter during Dynamic Flash, and the adapter "
            "no longer has a record of it (it restarted, or the flash never "
            "started). The ECU's state is not known: keep the ignition on, check "
            "the adapter, and run a normal full flash."
        )
        not_started_msg = (
            "Dynamic Flash did not start: the adapter has no record of this "
            "package's flash. The ECU was probably not changed, but this cannot "
            "be confirmed: switch the ignition off and on, then run a normal flash."
        )
        while self._clock() < deadline:
            try:
                st = self.status()
            except WiCANHttpError as e:
                last_error = e
                self._sleep(FOLLOW_UP_POLL_S)
                continue
            s = st.get("state")
            ours = st.get("name") == name and st.get("mode") == mode
            if s == "running" and ours:
                seen_running = True
                self._sleep(FOLLOW_UP_POLL_S)
                continue
            if s == "done" and ours:
                res = st.get("result")
                nothing = isinstance(res, dict) and bool(res.get("nothing"))
                done = FFEvent(
                    "done",
                    f"(GET /fast_flash: {name} done)",
                    {
                        "mode": mode,
                        "nothing": "1" if nothing else "0",
                        "rewritten": str(st.get("done", 0)),
                    },
                )
                return FastFlashOutcome(mode, done, events, _json_text(res), True)
            if s == "failed" and ours:
                err = _status_err(st, name)
                es = ecu_state(err)
                proof = _untouched_proof(mode, err)
                raise WiCANFastFlashError(
                    user_message(err) + f" (adapter: {err.raw})",
                    event=err,
                    state=es,
                    ponr=err.fields["ponr"] == "1",
                    untouched_proof=proof,
                )
            if s == "interrupted":
                who = st.get("name") or "an earlier package"
                raise WiCANFastFlashError(
                    "The adapter restarted during Dynamic Flash"
                    + ("" if st.get("name") == name else f" (of {who})")
                    + ". The ECU probably has no working program: keep the ignition "
                    "on and resume; if the ECU was switched off, run a normal full "
                    "flash.",
                    state=EcuState.NO_APPLICATION,
                    ponr=True,
                )
            if s == "running" and not seen_running:
                # Another package's op owns the adapter and the ECU: this K never
                # ran, and an ignition cycle now would cut that op short.
                other = st.get("name") or "another package"
                raise WiCANFastFlashError(
                    f"Dynamic Flash did not start: the adapter is still running an "
                    f"earlier Dynamic Flash ({other}). Wait for it to finish (the "
                    "adapter's status page shows it) and leave the ignition on.",
                    state=EcuState.BUSY,
                    ponr=False,
                )
            if seen_running:
                # The op was running and is gone (dongle reset without a journal,
                # or another op took over): never guess.
                self._lost(lost_msg, EcuState.UNKNOWN, None)
            # idle, or another package's state: this K never ran (after a grace,
            # in case the firmware is only now taking it).
            if self._clock() >= grace_end:
                self._lost(not_started_msg, EcuState.NOT_STARTED, None)
            self._sleep(FOLLOW_UP_POLL_S)
        self._lost(
            "Lost contact with the adapter during Dynamic Flash and could not learn "
            "how it ended. Keep the ignition on and check the adapter; the ECU may "
            "be waiting for a flash." + (f" ({last_error})" if last_error else ""),
            EcuState.UNKNOWN,
            None,
        )
        raise AssertionError("not reached")  # pragma: no cover


def _progress_of(ev: FFEvent) -> Tuple[str, float]:
    """Message + percent for the progress bar (0 = no change)."""
    if ev.kind == "step":
        return {
            "kernel": ("Starting the flash program in the ECU...", 10.0),
            "compare": ("Comparing the ECU with the ROM...", 20.0),
            "write": ("Writing the changed blocks...", 25.0),
            "verify": ("Verifying every block...", 92.0),
            "commit": ("Finishing...", 96.0),
            "reset": ("Restarting the ECU...", 98.0),
        }.get(ev.text, ("", 0.0))
    if ev.kind == "prog":
        done, total = ev.num("done"), ev.num("total")
        if total:
            return (
                f"Writing block {done + 1 if done < total else done} of {total}...",
                25.0 + 65.0 * done / total,
            )
    if ev.kind == "done":
        return "Done", 100.0
    return "", 0.0


def interrupted_before(client: WiCANFastFlashClient) -> bool:
    """True when ``GET /fast_flash`` says an earlier op did not finish."""
    try:
        return client.status().get("state") == "interrupted"
    except (ECUError, ValueError, AttributeError):
        return False


# --- the app's driver ---------------------------------------------------------------

#: Firmware step -> flash state shown in the progress page.
_STEP_STATE = {
    "kernel": "UPLOADING_KERNEL",
    "compare": "COMPARING",
    "write": "PROGRAMMING",
    "verify": "VERIFYING",
    "commit": "COMMITTING",
    "reset": "RESETTING",
}


class WiCANFastFlasher:
    """Dynamic Flash by block compare over the WiCAN: the driver the window's
    worker calls (same ``fast_flash`` surface as :class:`FlashManager`).

    Composes the SD-staged flasher for what both paths share: the link and
    battery gate, the bus reservation (``_datalog_fence``) and, before ``C``/``L``,
    the ECU session steps (authentication, generation check, flash counter).
    ``R`` (resume) needs no session: the firmware finds its kernel running.
    """

    def __init__(
        self,
        transport,
        *,
        datalog=None,
        source_name: Optional[str] = None,
        client: Optional[WiCANFastFlashClient] = None,
        sd_flasher=None,
    ):
        if sd_flasher is None:
            from .wican_sd_flash import WiCANSdFlasher

            sd_flasher = WiCANSdFlasher(
                transport, datalog=datalog, source_name=source_name
            )
        self._sd = sd_flasher
        self._client = client or WiCANFastFlashClient(transport)

    def set_unverified_generation_prompt(self, prompt) -> None:
        self._sd.set_unverified_generation_prompt(prompt)

    def set_generation_verified_hook(self, hook) -> None:
        self._sd.set_generation_verified_hook(hook)

    def interrupted_before(self) -> bool:
        """``GET /fast_flash`` says an earlier op did not finish."""
        return interrupted_before(self._client)

    def fast_flash(
        self,
        rom_data: bytes,
        archive_path: Optional[str] = None,
        progress_cb=None,
        *,
        kernel,
        resume: bool = False,
    ) -> FastFlashOutcome:
        """Upload the package, prepare the ECU (``L``) and run the op.

        Raises :class:`FastFlashUnavailable` (nothing sent to the ECU) when the
        adapter or ROM can't use it, :class:`ROMValidationError` ("identical")
        when nothing differed, :class:`WiCANFastFlashError` when the op failed.
        The archive is set aside before any contact (``L`` and ``R``) and written
        again only after the firmware reports THIS package's op done. It is put
        back only on proof that the ECU's flash was not touched: a failure before
        the ``K`` command went out, or an error with ``untouched_proof``.
        """
        from types import SimpleNamespace

        from .constants import ROM_FLASH_START_MIN
        from .exceptions import FastFlashUnavailable, FlashError, ROMValidationError
        from .fast_flash import ENTRY_POINTER, _is_blank
        from .fast_flash_package import package_image
        from .flash_manager import (
            FlashProgress,
            FlashState,
            _restore_archive,
            _set_archive_aside,
        )
        from .flash_prep import prepare_flash_image

        mode = "R" if resume else "L"

        def notify(state, message: str, percent: float) -> None:
            if progress_cb:
                progress_cb(
                    FlashProgress(state=state, percent=percent, message=message)
                )

        notify(FlashState.PREPARING_SBL, "Checking the adapter...", 1.0)
        ok, reason = fast_flash_available(self._client.caps(), mode, rom_data, kernel)
        if not ok:
            raise FastFlashUnavailable(
                f"Dynamic Flash can't compare blocks over this WiCAN: {reason}. "
                "Nothing was changed."
            )
        # Prepared ONCE: the package and the archive are the same bytes.
        corrected, _sbl, _gen, _corr = prepare_flash_image(
            rom_data, ROM_FLASH_START_MIN
        )
        if _is_blank(corrected[ENTRY_POINTER : ENTRY_POINTER + 4]):
            raise FlashError(
                "This ROM has no start address (0xFFFFC is blank): the ECU could "
                "never start it. Nothing was sent to the ECU."
            )
        version = kernel.pin.version if kernel.pin is not None else MIN_KERNEL_VERSION
        pkg = package_image(corrected, kernel.data, version)

        aside = _set_archive_aside(archive_path)
        state = {"state": FlashState.PREPARING_SBL, "k_sent": False}

        def on_event(ev: FFEvent) -> None:
            if ev.kind == "step":
                name = _STEP_STATE.get(ev.text)
                if name:
                    state["state"] = getattr(FlashState, name)

        def progress(message: str, percent: float) -> None:
            notify(state["state"], message, percent)  # quick: the PC must keep reading

        try:
            with self._sd._datalog_fence():
                self._sd._safeguards._gate()
                notify(
                    FlashState.PREPARING_SBL,
                    "Uploading the Dynamic Flash package...",
                    4.0,
                )
                self._client.upload(pkg)
                if mode != "R":
                    self._sd._authenticate_ecu(flash_counter=False)
                    self._sd._check_ecu_generation(
                        SimpleNamespace(corrected_rom=corrected)
                    )
                    self._sd._check_flash_counter()
                state["k_sent"] = True
                outcome = self._client.run(
                    pkg.name, mode, on_event=on_event, progress=progress
                )
        except BaseException as e:
            untouched = not state["k_sent"] or bool(
                getattr(e, "untouched_proof", False)
            )
            if aside is not None and untouched:
                _restore_archive(aside, archive_path)
            raise
        if archive_path:
            try:
                from pathlib import Path

                Path(archive_path).write_bytes(bytes(corrected))
            except OSError as e:  # pragma: no cover - non-fatal
                logger.warning("Archive save failed (non-fatal): %s", e)
        if outcome.nothing_to_do:
            raise ROMValidationError(
                "ROMs are identical: the ECU already holds this ROM (checked block "
                "by block by the WiCAN). Nothing was written."
            )
        notify(FlashState.COMPLETE, "Flash complete!", 100.0)
        return outcome
