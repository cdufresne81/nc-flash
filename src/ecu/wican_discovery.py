"""WiCAN mDNS auto-discovery (optional, host-side).

Browses the local network for WiCAN PRO adapters that advertise the
``_wican._tcp`` mDNS service (see firmware ``wc_mdns.c``) so the user does not
have to hardcode the adapter's DHCP-assigned IP. The device publishes a stable
``device_id`` / ``mac`` in its TXT record, which lets us persist *identity* and
re-resolve the *current* IP on each connect — robust to DHCP lease changes.

DEPENDENCY BOUNDARY — this module is import-light: only the Python standard
library is imported at module load. The third-party ``zeroconf`` package is
imported *lazily* inside :func:`_browse`, so merely importing this module (or
the wider ECU stack) never requires zeroconf to be installed. The brick-critical
transport/config modules (:mod:`src.ecu.wican_transport`,
:mod:`src.ecu.wican_config`) stay stdlib-only and never import this module on
their hot path. Discovery is a connect-/settings-time convenience only.

THREADING — :func:`discover` blocks for up to ``timeout_s`` while it listens for
mDNS responses (a zeroconf browser thread runs underneath; all shared state is
lock-guarded). Prefer a worker thread. It is, however, acceptable to call it
synchronously behind a wait cursor for a *user-initiated* one-shot scan — the
settings "Scan" button does this, the same way the dialog's "Test Connection"
performs synchronous network I/O, and the WiCAN connect path already blocks far
longer on the adapter's SLCAN-mode reboot. The connect-time
:func:`resolve_host_for_device_id` early-exits the instant the device is seen,
so the online case returns sub-second.

Observed live-device facts that shaped this module (deployed firmware, browsed
2026-06-24):
  * The record resolves to the IPv4 address, port 80, and TXT keys
    ``device_id`` + ``mac`` (both stable identifiers).
  * The ``firmware`` / ``hardware`` / ``version`` TXT keys may be EMPTY on a
    deployed build — they are treated as strictly optional here.
  * Enumerating peers can surface a malformed record that makes zeroconf raise
    ``BadTypeInNameException``; every per-peer resolve is wrapped so one bad
    neighbour never aborts the scan.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .exceptions import ECUError

logger = logging.getLogger(__name__)

#: mDNS service type advertised by the WiCAN firmware (see ``wc_mdns.c``).
SERVICE_TYPE = "_wican._tcp.local."

#: Default listen window. mDNS is push-based; a few seconds is plenty on a LAN.
DEFAULT_TIMEOUT_S = 4.0


class DiscoveryUnavailable(ECUError):
    """Raised when mDNS discovery cannot run because ``zeroconf`` is absent.

    Subclasses :class:`~src.ecu.exceptions.ECUError` so callers can catch it
    with the same unified handlers as the rest of the ECU stack. The UI uses
    this to fall back to manual IP entry with an actionable message.
    """


@dataclass(frozen=True)
class WiCANDevice:
    """A WiCAN adapter discovered on the local network."""

    name: str
    host: str
    port: int
    hostname: str
    device_id: Optional[str] = None
    mac: Optional[str] = None
    firmware: Optional[str] = None
    hardware: Optional[str] = None
    addresses: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stable_id(self) -> Optional[str]:
        """Identifier that survives DHCP IP changes (``mac`` preferred)."""
        return self.mac or self.device_id

    @property
    def label(self) -> str:
        """One-line label for a device-picker list."""
        ident = self.device_id or self.mac or "unknown id"
        return f"{self.host}  ({self.name} · {ident})"


def zeroconf_available() -> bool:
    """True if the optional ``zeroconf`` dependency can be imported.

    Lets the UI decide whether to offer the "Scan" affordance without forcing
    the dependency. Never raises.
    """
    try:
        import zeroconf  # noqa: F401
    except Exception:  # pragma: no cover - import failure path
        return False
    return True


def _normalize_txt(properties) -> dict[str, Optional[str]]:
    """Normalise a zeroconf TXT mapping (``bytes`` keys/values) to ``str``.

    Empty values are mapped to ``None`` by the caller; here we just decode.
    """
    out: dict[str, Optional[str]] = {}
    for key, value in (properties or {}).items():
        k = (
            key.decode("utf-8", "replace")
            if isinstance(key, (bytes, bytearray))
            else str(key)
        )
        if isinstance(value, (bytes, bytearray)):
            v: Optional[str] = value.decode("utf-8", "replace")
        elif value is None:
            v = None
        else:
            v = str(value)
        out[k] = v
    return out


def _parse_service_info(name: str, info) -> Optional[WiCANDevice]:
    """Build a :class:`WiCANDevice` from a resolved zeroconf ``ServiceInfo``.

    Pure and duck-typed (no zeroconf import) so it is unit-testable with a tiny
    fake. Returns ``None`` for an unresolved record or one with no usable
    address. Prefers IPv4; falls back to whatever ``parsed_addresses`` yields.
    """
    if info is None:
        return None
    try:
        parsed = list(info.parsed_addresses())
    except Exception:  # pragma: no cover - defensive
        parsed = []
    ipv4 = tuple(a for a in parsed if ":" not in a)
    addrs = ipv4 or tuple(parsed)
    if not addrs:
        logger.debug("WiCAN mDNS record %r has no usable address; skipping", name)
        return None

    txt = _normalize_txt(getattr(info, "properties", None))
    # Empty-string TXT values are as good as absent (deployed firmware leaves
    # firmware/hardware blank) — collapse them to None.
    get = lambda key: (txt.get(key) or None)  # noqa: E731

    instance = name.split(".")[0] if name else (getattr(info, "server", "") or "WiCAN")
    return WiCANDevice(
        name=instance,
        host=addrs[0],
        port=int(getattr(info, "port", 0) or 0),
        hostname=(getattr(info, "server", "") or "").rstrip("."),
        device_id=get("device_id"),
        mac=get("mac"),
        firmware=get("firmware"),
        hardware=get("hardware"),
        addresses=addrs,
    )


def _wait_for_browse(done, timeout_s: float, cancel_event=None) -> None:
    """Block until ``done`` fires, ``cancel_event`` fires, or ``timeout_s`` elapses.

    When no ``cancel_event`` is supplied this is exactly ``done.wait(timeout_s)``
    (the connect-time early-exit path, kept byte-for-byte). With a cancel event
    it polls both on a short tick so a user "Cancel" returns promptly instead of
    blocking the worker for the full window. Uses a monotonic deadline so the
    total wait never drifts past ``timeout_s``.
    """
    if cancel_event is None:
        done.wait(timeout_s)
        return
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if done.wait(min(0.1, remaining)):
            return  # early-exit predicate fired
        if cancel_event.is_set():
            return  # user cancelled


def _browse(
    timeout_s: float, stop_when=None, cancel_event=None
) -> list[tuple[str, object]]:
    """Listen for ``_wican._tcp`` records for up to ``timeout_s`` seconds.

    Returns ``(service_name, ServiceInfo|None)`` pairs. ``zeroconf`` is imported
    here and *only* here, so importing this module stays dependency-free. Each
    per-peer ``get_service_info`` is wrapped: a malformed neighbour record
    (``BadTypeInNameException`` et al.) is logged and skipped, never fatal.

    :param stop_when: optional predicate called with the live ``{name: info}``
        dict after each resolve; returning truthy ends the listen window early.
        Lets connect-time resolution return as soon as the target device is
        seen instead of always waiting the full timeout. A raising predicate is
        swallowed (it must never break the listener thread).
    :param cancel_event: optional ``threading.Event``; when set, the listen
        window returns early (with whatever was collected so far). Lets a
        user-facing scan be cancelled without waiting out the full timeout.
    :raises ImportError: if ``zeroconf`` is not installed (mapped to
        :class:`DiscoveryUnavailable` by :func:`discover`).
    """
    import threading

    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    # zeroconf dispatches add/remove callbacks from its own browser thread while
    # the calling thread reads the result — guard every access to ``collected``
    # with a lock and only ever hand the predicate / the caller a *snapshot*, so
    # no one iterates a dict another thread may mutate.
    collected: dict[str, object] = {}
    lock = threading.Lock()
    resolve_ms = max(1, int(timeout_s * 1000))
    done = threading.Event()

    class _Listener(ServiceListener):
        def add_service(self, zc, type_, name):  # noqa: D401
            try:
                info = zc.get_service_info(type_, name, timeout=resolve_ms)
            except Exception as exc:  # malformed peer / bad name / timeout
                logger.debug("WiCAN mDNS resolve failed for %r: %s", name, exc)
                return
            with lock:
                collected[name] = info
                snapshot = dict(collected)
            if stop_when is not None:
                try:
                    if stop_when(snapshot):
                        done.set()
                except Exception:  # a bad predicate must not kill discovery
                    logger.debug("WiCAN mDNS stop predicate raised", exc_info=True)

        # A record refresh is just a re-resolve.
        update_service = add_service

        def remove_service(self, zc, type_, name):
            with lock:
                collected.pop(name, None)

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, _Listener())
        _wait_for_browse(done, timeout_s, cancel_event)
    finally:
        zc.close()
    with lock:
        return list(collected.items())


def discover(
    timeout_s: float = DEFAULT_TIMEOUT_S, cancel_event=None
) -> list[WiCANDevice]:
    """Discover WiCAN adapters on the LAN. Blocks ~``timeout_s`` seconds.

    De-duplicates by stable id (mac/device_id) and sorts by host for a stable
    UI ordering. Call off the UI thread.

    :param cancel_event: optional ``threading.Event``; when set, the scan
        returns early with whatever adapters were found so far. Lets a
        user-facing "Scan" be cancelled without waiting out the full window.
    :raises DiscoveryUnavailable: if the optional ``zeroconf`` package is not
        installed.
    """
    try:
        raw = _browse(timeout_s, cancel_event=cancel_event)
    except ImportError as exc:
        raise DiscoveryUnavailable(
            "mDNS discovery needs the 'zeroconf' package. Install it "
            "(pip install zeroconf) or enter the WiCAN IP manually."
        ) from exc

    devices: list[WiCANDevice] = []
    seen: set[str] = set()
    for name, info in raw:
        dev = _parse_service_info(name, info)
        if dev is None:
            continue
        key = dev.stable_id or dev.host
        if key in seen:
            continue
        seen.add(key)
        devices.append(dev)

    devices.sort(key=lambda d: d.host)
    logger.info("WiCAN mDNS discovery found %d device(s)", len(devices))
    return devices


def _id_matches(dev: WiCANDevice, target: str, target_nocolon: str) -> bool:
    """True if ``dev`` matches a lowercased target id/mac (colon-insensitive)."""
    candidates = {
        (dev.device_id or "").lower(),
        (dev.mac or "").lower(),
        (dev.mac or "").replace(":", "").lower(),
        (dev.device_id or "").replace(":", "").lower(),
    }
    candidates.discard("")
    return target in candidates or target_nocolon in candidates


#: Re-resolve budget for the fallback path — shared by every consumer that
#: talks to a stored adapter identity (ECU connect, trip-log sync).
RESOLVE_FALLBACK_TIMEOUT_S = 3.0


def resolve_host_with_fallback(
    device_id: str, fallback_host: str, timeout_s: float = RESOLVE_FALLBACK_TIMEOUT_S
) -> str:
    """Best-effort: the current IP for ``device_id``, else ``fallback_host``.

    THE single copy of the re-resolve fallback policy: no stored identity,
    discovery unavailable, device offline, ambiguous identity, or any error
    all return ``fallback_host`` — a caller is never blocked or broken by
    discovery. Never raises. A caller that caches a fresh IP (the ECU connect
    path) compares the return value against ``fallback_host`` itself.
    """
    if not device_id:
        return fallback_host
    try:
        resolved = resolve_host_for_device_id(device_id, timeout_s=timeout_s)
    except Exception as e:  # never let discovery break the caller
        logger.debug("WiCAN mDNS re-resolve failed (%s); using stored host", e)
        return fallback_host
    if not resolved:
        logger.debug(
            "WiCAN %s not found via mDNS; using stored host %s",
            device_id,
            fallback_host,
        )
        return fallback_host
    return resolved


def resolve_host_for_device_id(
    device_id: str, timeout_s: float = DEFAULT_TIMEOUT_S
) -> Optional[str]:
    """Return the current IP of the adapter whose id/mac matches ``device_id``.

    Used at connect time to re-resolve a stored identity to its live DHCP
    address. Matches against ``device_id`` and ``mac`` (with/without colons).
    Returns ``None`` if discovery is unavailable or no match is found — the
    caller then falls back to the stored static IP.

    Stops listening the instant the target is seen (via the ``stop_when``
    early-exit), so the common "device is online" case resolves in well under
    the full ``timeout_s`` rather than always blocking for the whole window.

    SAFETY — identity is normally a globally-unique MAC, so exactly one adapter
    answers. If the scan nonetheless sees the identity at **more than one
    distinct IP** (only possible with cloned/duplicated MACs), this refuses to
    guess which one is meant — returning ``None`` so the caller falls back to
    the user's stored static host rather than risk talking to the wrong ECU.
    """
    if not device_id:
        return None

    target = device_id.strip().lower()
    target_nocolon = target.replace(":", "")

    def _seen(collected) -> bool:
        # ``collected`` is a private snapshot from _browse — safe to iterate.
        for info in collected.values():
            dev = _parse_service_info("", info)
            if dev is not None and _id_matches(dev, target, target_nocolon):
                return True
        return False

    try:
        raw = _browse(timeout_s, stop_when=_seen)
    except ImportError:
        return None

    matching_hosts = {
        dev.host
        for name, info in raw
        if (dev := _parse_service_info(name, info)) is not None
        and _id_matches(dev, target, target_nocolon)
    }
    if len(matching_hosts) == 1:
        return next(iter(matching_hosts))
    if len(matching_hosts) > 1:
        logger.warning(
            "WiCAN identity %r resolved to %d distinct hosts (%s); refusing to "
            "guess which adapter is meant — falling back to the stored host",
            device_id,
            len(matching_hosts),
            sorted(matching_hosts),
        )
    return None
