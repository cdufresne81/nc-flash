"""Shared HTTP plumbing for WiCAN device utilities (port-80 REST surface).

The WiCAN PRO exposes several plain-HTTP device services (trip-log download,
SD file management, config) that are fully decoupled from the CAN bus / SLCAN
session / ECU. Per the architecture rule "ONE pipeline copy", the transport
boilerplate those clients share lives here and only here:

- :func:`get_json` — GET a JSON endpoint with typed, contextual errors.
- :func:`download_to_file` — stream a file to disk **atomically**: write to a
  ``.part`` sibling, verify the received byte count against the size the
  device advertised, then ``os.replace`` into place. An interrupted WiFi
  transfer must never leave a corrupt file that looks complete.
- :func:`sanitize_basename` — reject device-supplied names that could escape
  the destination directory (defense-in-depth; the firmware guards too, but a
  clean client never trusts a listing).

Consumers: :mod:`src.ecu.wican_logs` (trip-log sync, issue #83) and the future
SD file browser client (issue #84). The pre-existing flash-path clients
(:mod:`src.ecu.wican_config`, :mod:`src.ecu.wican_sd_upload`) predate this
module and stay untouched — migrating them is a behavior-preserving refactor
gated on a hardware test per ``docs/internal/WICAN_MANUAL_TEST.md``.

Headless: standard library only (``urllib``/``json``/``socket``), no PySide6.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .exceptions import ECUError

logger = logging.getLogger(__name__)

#: Default per-request timeout. This is a socket-level (per-blocking-op)
#: timeout, not a total-transfer cap, so it also suits large file downloads.
DEFAULT_TIMEOUT_S = 10.0

#: Streaming chunk size for downloads.
_CHUNK_SIZE = 64 * 1024

#: Characters rejected in device-supplied basenames: path separators plus the
#: Windows-reserved set, checked on every platform so behavior is deterministic.
_FORBIDDEN_CHARS = set('/\\<>:"|?*')


class WiCANHttpError(ECUError):
    """A WiCAN HTTP device call failed, was unreachable, or did not verify.

    Subclasses :class:`~src.ecu.exceptions.ECUError` so failures flow through
    the unified ECU error handlers.
    """


def sanitize_basename(name: str) -> str:
    """Validate a device-supplied filename for use as a local basename.

    Rejects (raises :class:`WiCANHttpError`) rather than rewrites: a name that
    needs rewriting is evidence of a corrupt or hostile listing, and silently
    "fixing" it would hide that. Callers doing bulk work should catch per-file
    and skip with a warning.
    """
    if not name or name.strip() != name or name in (".", ".."):
        raise WiCANHttpError(f"Invalid device filename {name!r}")
    if ".." in name:
        raise WiCANHttpError(f"Device filename must not contain '..': {name!r}")
    bad = _FORBIDDEN_CHARS.intersection(name)
    if bad or any(ord(c) < 0x20 for c in name):
        raise WiCANHttpError(f"Device filename contains forbidden characters: {name!r}")
    return name


def get_json(url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S):
    """GET *url* and return the parsed JSON body.

    Raises :class:`WiCANHttpError` on any transport failure, HTTP error status
    (``urlopen`` raises for everything outside 2xx), or non-JSON body — always
    with the URL in the message so bulk callers can surface which endpoint
    failed.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise WiCANHttpError(f"GET {url} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, socket.error, OSError) as exc:
        raise WiCANHttpError(f"GET {url} failed: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WiCANHttpError(
            f"GET {url}: device reply was not JSON: {raw[:200]!r}"
        ) from exc


def download_to_file(
    url: str,
    dest: Path,
    *,
    expected_size: Optional[int] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    abort_cb=None,
) -> Path:
    """Stream *url* to *dest* atomically and return *dest*.

    Writes to ``<dest>.part``, then — only after the stream ends AND the byte
    count matches *expected_size* (when given) — renames into place with
    :func:`os.replace`. On any failure the ``.part`` file is removed and
    :class:`WiCANHttpError` raised; *dest* is never created or clobbered by a
    partial transfer.

    ``abort_cb`` (no-arg, returns truthy to abort) is polled between chunks so
    a worker thread can be stopped promptly at app exit; an abort cleans up the
    ``.part`` file and raises like any other failure.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    received = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            with open(part, "wb") as fh:
                while True:
                    if abort_cb is not None and abort_cb():
                        raise WiCANHttpError(f"download of {url} aborted")
                    chunk = resp.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
    except WiCANHttpError:
        _remove_quietly(part)
        raise
    except urllib.error.HTTPError as exc:
        _remove_quietly(part)
        raise WiCANHttpError(f"GET {url} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, socket.error, OSError) as exc:
        _remove_quietly(part)
        raise WiCANHttpError(f"download of {url} failed: {exc}") from exc

    if expected_size is not None and received != expected_size:
        _remove_quietly(part)
        raise WiCANHttpError(
            f"download of {url}: received {received} bytes, device advertised "
            f"{expected_size} — refusing the partial/mismatched transfer"
        )

    os.replace(part, dest)
    return dest


def _remove_quietly(path: Path):
    """Best-effort cleanup of a ``.part`` remnant."""
    try:
        path.unlink()
    except OSError:
        pass
