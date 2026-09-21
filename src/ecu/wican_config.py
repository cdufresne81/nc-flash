"""
WiCAN device HTTP helpers — host capability contract + datalog coordination.

The WiCAN PRO fork NC Flash targets has exactly one mode and one CAN socket:
the always-on coexistence SLCAN listener on
:data:`~src.ecu.constants.WICAN_DEDICATED_SLCAN_PORT`. There is no top-level
``protocol`` config key, no stock SLCAN server on port 35000, and no
"switch the adapter into SLCAN mode and reboot it" path — so the host never
writes the device's config, and nothing here can strand a device the way the
old protocol-switch could (#92).

What remains is read-only or advisory:

  * :class:`WiCANConfigurator` — read-only HTTP: ``GET /host_caps`` (the
    firmware commits to that endpoint as a host contract; it is small, constant,
    and free of the cleartext WiFi/MQTT passwords the full config blob carries)
    and ``GET /load_config`` for bench diagnostics. It never WRITES.
  * :class:`WiCANDatalogClient` — the ``/datalog`` coordination endpoint that
    pauses/resumes the datalogger and holds the refcounted bus reservation
    around a flash. Every call here is failure-tolerant by contract; the
    firmware ``FLASH_ACTIVE_BIT`` interlock is the real brick-safety guarantee.

This is a core ECU module and MUST stay headless: standard library only
(``urllib.request``, ``json``, ``re``, ``time``, ``logging``). No PySide6, no
third-party imports.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .constants import (
    DATALOG_KEEPALIVE_INTERVAL_S,
    PRE_SESSION_SETTLE_S,
)
from .exceptions import ECUError

logger = logging.getLogger(__name__)


class WiCANConfigError(ECUError):
    """Raised on WiCAN HTTP-config read failures.

    Subclasses :class:`~src.ecu.exceptions.ECUError` so it is caught by the
    same unified handlers as the rest of the ECU stack.
    """

    pass


#: Sidecar home, under the user profile rather than the OS temp dir. A temp
#: clean would otherwise silently destroy the only record of a datalogger this
#: host left parked (#92). The app already creates this directory for its logs.
_SIDECAR_DIRNAME = ".nc-flash"


def _sidecar_dir() -> str:
    """Directory holding the host-keyed sidecars, created on demand.

    Degrades to the OS temp dir if the home directory cannot be used, so a
    locked-down machine behaves as it did before rather than failing outright.
    """
    try:
        path = os.path.join(os.path.expanduser("~"), _SIDECAR_DIRNAME)
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:  # pragma: no cover - unwritable home
        return tempfile.gettempdir()


def _sanitize_host(host: object) -> str:
    """Host reduced to a filesystem-safe token (``192.168.1.9`` -> ``192_168_1_9``)."""
    return re.sub(r"[^A-Za-z0-9]", "_", str(host))


def _host_keyed_temp_path(host: object, prefix: str) -> str:
    """Path of a host-keyed JSON sidecar.

    Keyed by ``host`` so concurrent runs against different devices never clobber
    each other, and stable across runs so a NEXT run can detect and recover a
    datalogger this host left parked. ``prefix`` namespaces the file
    (``wican_datalog`` for the datalog-pause breadcrumb).
    """
    return os.path.join(_sidecar_dir(), f"{prefix}_{_sanitize_host(host)}.json")


class WiCANConfigurator:
    """Read the WiCAN's host-capability contract over HTTP.

    Deliberately read-only. The host never writes the device's stored config:
    the firmware has one mode, so there is nothing to switch and nothing that
    could leave the device stranded in a mode the user did not choose.

    NOTE this class has NO production callers. ``host_caps`` is the contract the
    firmware commits to, and ``read_config_raw`` is bench diagnostic surface that
    issue #99 requires be kept; both are exercised by tests. Do not go looking
    for the caller — there isn't one — and do not delete either method on that
    basis (``read_config_raw`` was already deleted once and had to be restored).
    """

    def __init__(
        self,
        host: str,
        http_port: int = 80,
        timeout_s: float = 8.0,
    ) -> None:
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s

    # -- URL helpers -------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.http_port}{path}"

    def read_config_raw(self, timeout_s: Optional[float] = None) -> str:
        """GET ``/load_config`` and return the raw response text.

        Read-only, and the ONLY caller-visible route to the full config blob.
        Prefer :meth:`host_caps` wherever it will do: this blob carries the
        user's WiFi/MQTT passwords in CLEARTEXT and is expensive for the device
        to assemble while it is busy. Nothing in the app calls this today — it
        is kept as diagnostic surface for bench work.

        :param timeout_s: optional per-call socket timeout override; defaults to
            ``self.timeout_s``.
        :raises WiCANConfigError: on any HTTP/socket error or non-200 status.
        """
        url = self._url("/load_config")
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise WiCANConfigError(
                f"GET /load_config failed: HTTP {exc.code} from {self.host}"
            ) from exc
        except (urllib.error.URLError, socket.error, OSError) as exc:
            raise WiCANConfigError(
                f"GET /load_config failed: cannot reach {self.host}: {exc}"
            ) from exc

        if status != 200:
            raise WiCANConfigError(
                f"GET /load_config returned HTTP {status} from {self.host}"
            )
        return body.decode("utf-8", errors="replace")

    def host_caps(self, timeout_s: Optional[float] = None) -> Optional[dict]:
        """``GET /host_caps`` -> ``{"ncfr_rev": int, "protocol": str}``, or ``None``.

        A small, constant, secret-free reply that tells a host tool what this
        boot is and can do. Preferred over ``/load_config`` for capability and
        "is this device stranded?" checks, because the config blob carries the
        user's WiFi passwords in cleartext and is expensive for the device to
        assemble while it is busy.

        Returns ``None`` — never raises — when the endpoint is absent (every
        build in the field before it shipped answers 404), unreachable, or
        unparseable. Callers MUST treat ``None`` as "unknown, fall back", never
        as evidence of old firmware.

        Note ``protocol`` is the STORED mode, not the running one: a restore
        targets what was written, and under SmartConnect the two differ.
        """
        try:
            with urllib.request.urlopen(
                self._url("/host_caps"),
                timeout=self.timeout_s if timeout_s is None else timeout_s,
            ) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read()
        except Exception as exc:
            logger.debug("GET /host_caps on %s unavailable: %s", self.host, exc)
            return None
        if status != 200:
            return None
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


#: Default per-call timeout (s) for the ``/datalog`` coordination endpoint. Short:
#: the endpoint is local + synchronous, and EVERY failure soft-degrades to "rely on
#: the firmware FLASH_ACTIVE_BIT interlock", never aborts a flash.
DATALOG_TIMEOUT_S = 5.0


class WiCANDatalogClient:
    """REST client for the no-reboot coexistence ``/datalog`` endpoint (firmware #36.C).

    Pauses/resumes the WiCAN datalogger around a flash WITHOUT the reboot-switch.
    Deliberately INDEPENDENT of :class:`WiCANConfigurator`: keyed only on ``host``,
    fired at the flash boundary, with its own host-keyed crash-recovery sidecar
    (separate file from the protocol-recovery sidecar so the two never collide).

    BRICK-SAFETY CONTRACT: the firmware ``FLASH_ACTIVE_BIT`` interlock is the real
    guarantee; this REST pause is an *advisory* pre-park that also stops SD logging.
    So every method here is FAILURE-TOLERANT — a 404/405 (a port-only ``NCFRv6``
    build with no ``/datalog``), a timeout, or an unreachable device is logged and
    swallowed, returning ``None``, NEVER raised into the flash path. A broken
    ``/datalog`` must not abort a flash. (Decouples the ``/datalog`` capability from
    the dedicated-port rev gate, per the #36 audit.)
    """

    def __init__(
        self,
        host: str,
        http_port: int = 80,
        timeout_s: float = DATALOG_TIMEOUT_S,
        keepalive_interval_s: float = DATALOG_KEEPALIVE_INTERVAL_S,
    ) -> None:
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s
        self._keepalive_interval_s = keepalive_interval_s
        # Dead-man's-switch lease tokens issued by the firmware (None on a pre-deadman
        # build, which simply has no lease/reaper — we degrade to a plain pause/resume).
        self._park_token: Optional[int] = None
        self._claim_token: Optional[int] = None
        # Keepalive daemon: renews BOTH leases while a pause/claim is held so the
        # firmware reaper never false-expires under a present host. Stopped via an
        # explicit Event (NOT GC/__del__) so a leaked thread can't pin a lease, and
        # registered with atexit as the final backstop.
        self._ka_lock = threading.Lock()
        self._ka_thread: Optional[threading.Thread] = None
        self._ka_stop: Optional[threading.Event] = None
        self._atexit_registered = False
        # Refcounted bus reservation (claim + pause). The whole-session reservation
        # (held connect->disconnect) and the flash fence share ONE owner: the real
        # bus_claim()+pause() fire once on the 0->1 transition, bus_release()+resume()
        # once on the 1->0, so nesting can never double-claim the single-owner lease.
        # RLock: the transition work runs under the lock and must be atomic w.r.t.
        # another thread's acquire/release (connect on the UI thread vs the flash
        # worker thread).
        self._reserve_lock = threading.RLock()
        self._reserve_depth = 0

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.http_port}{path}"

    @staticmethod
    def _op_path(op: str, **params: object) -> str:
        """Build a ``/datalog?op=<op>[&k=v...]`` path, omitting None-valued params."""
        query = f"/datalog?op={op}"
        for key, value in params.items():
            if value is not None:
                query += f"&{key}={value}"
        return query

    # -- Crash-recovery sidecar (host-keyed; distinct from protocol recovery) --

    @property
    def recovery_path(self) -> str:
        """Stable, host-keyed path of the datalog-pause breadcrumb file."""
        return _host_keyed_temp_path(self.host, "wican_datalog")

    def _mark_stopped(self) -> None:
        """Record (before the pause request) that THIS host paused the datalogger,
        plus whatever lease tokens we currently hold, so a hard kill mid-flash is
        reconciled on the next connect. Best-effort; never fails a flash."""
        try:
            with open(self.recovery_path, "w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "host": self.host,
                            "datalog_stopped": True,
                            "park_token": self._park_token,
                            "claim_token": self._claim_token,
                        }
                    )
                )
        except OSError as exc:  # sidecar is best-effort; never fail a flash over it
            logger.debug("datalog sidecar write failed (non-fatal): %s", exc)

    def _read_breadcrumb(self) -> Optional[dict]:
        """Return THIS host's datalog-pause breadcrumb dict, or ``None``."""
        try:
            with open(self.recovery_path, "r", encoding="utf-8") as fh:
                data = json.loads(fh.read())
        except (OSError, ValueError, TypeError):
            return None
        if (
            isinstance(data, dict)
            and data.get("host") == self.host
            and bool(data.get("datalog_stopped"))
        ):
            return data
        return None

    def _is_marked_stopped(self) -> bool:
        """True iff a breadcrumb for THIS host says the datalogger was left paused."""
        return self._read_breadcrumb() is not None

    def clear_stopped(self) -> None:
        """Best-effort delete of the datalog breadcrumb (ignore if missing)."""
        try:
            os.unlink(self.recovery_path)
        except OSError:
            pass

    # -- HTTP (soft-degrading) --------------------------------------------------

    def _request_ex(self, method: str, path: str):
        """Issue ``method path`` -> ``(status, data)``.

        ``status`` is the HTTP status code if the device answered (200/404/409/500…)
        or ``None`` if it was unreachable / timed out. ``data`` is the parsed JSON
        dict on a 200-with-JSON, else ``None``. NEVER raises — every failure degrades
        so a broken ``/datalog`` can never abort a flash. The status is surfaced so a
        caller can treat a 409 ("already auto-reaped by the firmware reaper") as
        success rather than a failure.
        """
        url = self._url(path)
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            logger.info(
                "%s %s -> HTTP %s (datalog unsupported/stale; firmware interlock holds)",
                method,
                path,
                exc.code,
            )
            return exc.code, None
        except (urllib.error.URLError, socket.error, OSError) as exc:
            logger.info(
                "%s %s failed (%s); relying on firmware interlock", method, path, exc
            )
            return None, None
        if status != 200:
            logger.info(
                "%s %s -> HTTP %s; relying on firmware interlock", method, path, status
            )
            return status, None
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            logger.info("%s %s returned non-JSON; ignoring", method, path)
            return status, None
        return status, (data if isinstance(data, dict) else None)

    def _request(self, method: str, path: str) -> Optional[dict]:
        """Issue ``method path``; return the parsed JSON dict, else ``None`` (any
        non-200 / failure). Thin wrapper over :meth:`_request_ex`."""
        _status, data = self._request_ex(method, path)
        return data

    # -- Dead-man's-switch lease: pause / claim / keepalive / resume ------------

    def pause(self) -> Optional[dict]:
        """Pause the datalogger for a flash; return the state dict or ``None``.

        Writes the crash-recovery breadcrumb BEFORE the request (so a hard kill
        between here and :meth:`resume` is reconciled on the next connect), captures
        the firmware's park-lease token, and starts the keepalive daemon so the lease
        cannot false-expire while we hold the pause (e.g. across a multi-minute read).
        """
        self._mark_stopped()
        state = self._request("POST", self._op_path("pause"))
        if state is not None:
            tok = state.get("park_token")
            self._park_token = tok if isinstance(tok, int) else None
            self._mark_stopped()  # rewrite now that we know the token
            self._ensure_keepalive()
        return state

    def bus_claim(self) -> Optional[dict]:
        """Claim the CAN bus for the WHOLE host-driven programming window.

        Raised BEFORE the UDS auth handshake and held until :meth:`bus_release` (after
        ``fast_write`` returns). This is the brick fence the codec's ``FLASH_ACTIVE_BIT``
        does NOT cover: while the host drives the 0x10/0x27 session the firmware sets
        ``HOST_BUS_CLAIM_BIT`` so the reaper can never auto-resume the poller into a
        live, security-unlocked session. Captures the claim token + starts keepalive.
        Soft-degrading: a pre-deadman build (404) returns ``None`` and the flash
        proceeds on the ``FLASH_ACTIVE_BIT`` interlock alone.
        """
        state = self._request("POST", self._op_path("bus_claim"))
        if state is not None:
            tok = state.get("claim_token")
            self._claim_token = tok if isinstance(tok, int) else None
            self._mark_stopped()
            self._ensure_keepalive()
        return state

    def bus_release(self) -> Optional[dict]:
        """Release the host bus-claim after ``fast_write`` returns (or fails).

        Token-matched on the firmware; a 409 (already reaped) is treated as success.
        Does NOT stop the keepalive while a park is still held — :meth:`resume` does.
        """
        _status, state = self._request_ex(
            "POST", self._op_path("bus_release", token=self._claim_token)
        )
        self._claim_token = None  # released (200) or already-reaped (409): drop it
        self._maybe_stop_keepalive()
        return state

    def resume(self) -> Optional[dict]:
        """Resume the datalogger; clear the breadcrumb + stop the keepalive.

        Token-matched on the firmware. A 409 means the reaper already auto-resumed
        (the host had been gone) — treated as success. We always drop our local park
        state and stop the keepalive, but clear the breadcrumb only when the firmware
        actually answered (200 resumed / 409 already-done); a genuine unreachable or
        timeout (status ``None``) leaves the breadcrumb so the NEXT connect's
        reconcile retries.
        """
        status, state = self._request_ex(
            "POST", self._op_path("resume", token=self._park_token)
        )
        self._park_token = None
        self._maybe_stop_keepalive()
        if status in (200, 409):  # resumed, or reaper already did -> breadcrumb done
            self.clear_stopped()  # a 404/500/timeout leaves it for the next reconcile
        return state

    # -- Refcounted whole-session bus reservation -------------------------------

    @contextlib.contextmanager
    def reserved(self):
        """Hold a refcounted bus reservation (claim + pause) for the ``with`` block.

        Reference-counted so the whole-session reservation (held from connect to
        disconnect) and the flash ``_datalog_fence`` can NEST without a double-claim:
        the real :meth:`bus_claim` + :meth:`pause` (and the poll-task settle) fire once
        on the 0->1 transition, and :meth:`bus_release` + :meth:`resume` once on the
        1->0. Soft-degrading — on a device with no ``/datalog`` the underlying ops are
        no-ops and only the depth counter moves.
        """
        self.acquire_bus()
        try:
            yield
        finally:
            self.release_bus()

    def acquire_bus(self) -> None:
        """Raise (or re-enter) the host bus reservation; see :meth:`reserved`.

        Only the FIRST acquire claims the bus, parks the datalogger, and settles long
        enough for the firmware poll task to actually park before the first ECU
        contact. Deeper acquires just bump the refcount — the bus is already ours.
        """
        with self._reserve_lock:
            self._reserve_depth += 1
            if self._reserve_depth == 1:
                self.bus_claim()
                self.pause()
                # Let the firmware poll task observe the park and stop driving the bus
                # before the caller's first Tester-Present / auth handshake.
                time.sleep(PRE_SESSION_SETTLE_S)

    def release_bus(self) -> None:
        """Drop one bus-reservation ref; the LAST release frees the bus.

        Mirrors :meth:`bus_claim`/:meth:`pause` ordering on the way out
        (``bus_release`` then ``resume``) so the keepalive daemon stops only once both
        leases are dropped. A release with no outstanding acquire is a no-op.
        """
        with self._reserve_lock:
            if self._reserve_depth == 0:
                return
            self._reserve_depth -= 1
            if self._reserve_depth == 0:
                self.bus_release()
                self.resume()

    def get_state(self) -> Optional[dict]:
        """``GET /datalog`` -> live coexistence state dict (or ``None``).

        Deadman build: ``{ok, flash_active, datalog_parked, host_bus_claimed,
        manual_mode, park_token, lease_ttl_ms, claim_ttl_ms, bus_idle_ms,
        stuck_flash_alarm}``.
        """
        return self._request("GET", "/datalog")

    # -- Keepalive daemon -------------------------------------------------------

    def _ensure_keepalive(self) -> None:
        """Start the keepalive daemon if not already running (idempotent)."""
        with self._ka_lock:
            if self._ka_thread is not None and self._ka_thread.is_alive():
                return
            self._ka_stop = threading.Event()
            self._ka_thread = threading.Thread(
                target=self._keepalive_loop,
                args=(self._ka_stop,),
                name=f"wican-datalog-keepalive-{self.host}",
                daemon=True,
            )
            self._ka_thread.start()
            if not self._atexit_registered:
                atexit.register(self._stop_keepalive)
                self._atexit_registered = True

    def _maybe_stop_keepalive(self) -> None:
        """Stop the keepalive once BOTH leases have been released."""
        if self._park_token is None and self._claim_token is None:
            self._stop_keepalive()

    def _stop_keepalive(self) -> None:
        """Signal + join the keepalive daemon (bounded). Safe to call repeatedly."""
        with self._ka_lock:
            stop, thread = self._ka_stop, self._ka_thread
            self._ka_stop, self._ka_thread = None, None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.timeout_s + 1.0)

    def _send_keepalive(self) -> None:
        """One keepalive POST renewing whichever leases we currently hold."""
        if self._park_token is None and self._claim_token is None:
            return
        self._request(
            "POST",
            self._op_path(
                "keepalive",
                park_token=self._park_token,
                claim_token=self._claim_token,
            ),
        )

    def _keepalive_loop(self, stop: threading.Event) -> None:
        """Renew the leases every ``keepalive_interval_s`` until signalled to stop.

        ``Event.wait`` returns True at once when stopped (prompt teardown) and False
        on each interval tick, when we fire a renewal.
        """
        while not stop.wait(self._keepalive_interval_s):
            try:
                self._send_keepalive()
            except Exception as exc:  # never let the daemon die on a transient error
                logger.debug("datalog keepalive tick failed (non-fatal): %s", exc)

    def close(self) -> None:
        """Release everything: stop the keepalive daemon and drop local lease state.

        Called from session teardown / window close so a disconnected flasher never
        leaves a host thread renewing a lease. Does NOT itself POST resume (the worker
        ``finally`` / firmware reaper own that) — it just stops our renewals so the
        lease can legitimately expire if it was somehow left armed.
        """
        self._park_token = None
        self._claim_token = None
        self._stop_keepalive()

    def reconcile(self) -> None:
        """At connect: resume a datalogger left paused by a prior (crashed) run.

        No-op unless THIS host has a breadcrumb. Token-aware + brick-safe:

          * If the device reports an ACTIVE flash OR an outstanding host bus-claim, a
            programming session is live (this instance OR a second one) — LEAVE it
            paused; its owner (or the firmware reaper) resumes it. This replaces the
            old, INCORRECT belief that the pre-flash pause window is harmless to
            resume into: the auth window is exactly the unfenced-by-FLASH_ACTIVE_BIT
            danger zone, now covered by ``host_bus_claimed``.
          * Otherwise resume with the breadcrumb's park token. A 409 means the
            firmware reaper already auto-resumed after we vanished — treated as
            success. Fully soft-degrading; a genuine failure leaves the breadcrumb.
        """
        crumb = self._read_breadcrumb()
        if crumb is None:
            return
        state = self.get_state()
        if state is not None and (
            state.get("flash_active") or state.get("host_bus_claimed")
        ):
            logger.info(
                "datalog reconcile skipped: a flash/claim is active "
                "(this or another NC Flash instance owns the bus)"
            )
            return
        tok = crumb.get("park_token")
        self._park_token = tok if isinstance(tok, int) else None
        logger.info(
            "datalog reconcile: resuming a datalogger left paused by a prior run"
        )
        self.resume()
