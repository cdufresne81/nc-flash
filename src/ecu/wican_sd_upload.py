"""Reliable HTTP upload of a staged flash image to the WiCAN SD card (Option B).

The SD-staged flash (``.claude/plans/wican-write-option-b-goal.md``) takes WiFi
*out of the flash loop*: the host uploads the checksum-corrected ROM (+appended
SBL) to the WiCAN's SD card over plain reliable TCP **once**, then the firmware
drives the ECU program sequence locally over CAN. This module is the upload half
— the only WiFi step on the WRITE path, and it is fully verifiable *before* the
ECU is ever touched.

It POSTs a ``multipart/form-data`` body to ``/upload/sd/<name>`` (the file part
carries ``filename=`` — the firmware skips parts without one) and checks the
firmware's ``{bytes_written, crc32}`` reply against the host-computed size + CRC.
A mismatch raises and the caller MUST NOT trigger a flash (safety invariant #5:
the firmware also re-verifies the digest pre-erase, but the host refuses to even
trigger on a bad upload — defense in depth).

Pairs with :mod:`src.ecu.wican_sd_package` (which produces the
:class:`~src.ecu.wican_sd_package.FlashPackage` uploaded here). Headless: standard
library only (``urllib``/``json``/``socket``), no PySide6, no third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .checksum import crc32
from .exceptions import ECUError

logger = logging.getLogger(__name__)

#: Multipart boundary. Fixed (not random) so the module stays deterministic and
#: resume-safe; it only has to not appear in the binary body, and this 32-hex
#: token effectively never does.
_BOUNDARY = "----NCFlashSdUploadBoundary7e3f1a9c"

#: Default per-request timeout. A ~1 MB image uploads in ~1 s (metered), but the
#: device may stall briefly on the SD write, so leave generous headroom.
DEFAULT_TIMEOUT_S = 30.0

#: Endpoint prefix on the firmware (wildcard ``/upload/sd/*``).
UPLOAD_PATH_PREFIX = "/upload/sd/"


class WiCANUploadError(ECUError):
    """An SD upload failed, was unreachable, or did not verify."""


@dataclass(frozen=True)
class UploadResult:
    """What the firmware reported for a stored file, after host verification."""

    name: str
    bytes_written: int
    crc32: int
    raw_response: str


def _coerce_crc32(value) -> Optional[int]:
    """Parse a CRC32 from the firmware reply (int, ``"0x..."``, or bare hex)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16 if text.lower().startswith("0x") else 10) & 0xFFFFFFFF
        except ValueError:
            # Some firmwares emit a bare hex digest with no 0x prefix.
            try:
                return int(text, 16) & 0xFFFFFFFF
            except ValueError:
                return None
    return None


def _encode_multipart(field_name: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """Build a single-file ``multipart/form-data`` body. Returns (body, content_type)."""
    pre = (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    post = f"\r\n--{_BOUNDARY}--\r\n".encode("utf-8")
    body = pre + data + post
    return body, f"multipart/form-data; boundary={_BOUNDARY}"


def _sanitize_name(name: str) -> str:
    """Reject path-traversal / separators in a staged filename (host-side guard).

    The firmware also guards, but a clean client never sends a dangerous name.
    """
    if not name or name in (".", ".."):
        raise WiCANUploadError(f"Invalid upload name {name!r}")
    if "/" in name or "\\" in name or ".." in name:
        raise WiCANUploadError(
            f"Upload name must not contain path separators: {name!r}"
        )
    return name


class WiCANSdUploader:
    """Uploads staged flash artifacts to ``/upload/sd/<name>`` and verifies them."""

    def __init__(
        self, host: str, http_port: int = 80, timeout_s: float = DEFAULT_TIMEOUT_S
    ):
        self.host = host
        self.http_port = http_port
        self.timeout_s = timeout_s

    def _url(self, name: str) -> str:
        return f"http://{self.host}:{self.http_port}{UPLOAD_PATH_PREFIX}{name}"

    # --- core primitive -----------------------------------------------------

    def upload_bytes(
        self,
        name: str,
        data: bytes,
        *,
        expected_crc32: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> UploadResult:
        """POST ``data`` to ``/upload/sd/<name>`` and verify the device reply.

        Verifies the firmware's reported ``bytes_written`` equals ``len(data)``
        and, when ``expected_crc32`` is given (or always against our own CRC if
        the device echoes one), that the stored CRC matches. Any mismatch /
        transport error raises :class:`WiCANUploadError` — the upload is NOT
        trusted on a partial or corrupt result.
        """
        name = _sanitize_name(name)
        host_crc = crc32(data)
        if expected_crc32 is not None and expected_crc32 != host_crc:
            # The caller's expectation disagrees with the actual bytes — a packaging
            # bug; never upload bytes whose CRC we can't vouch for.
            raise WiCANUploadError(
                f"expected_crc32 0x{expected_crc32:08X} != actual 0x{host_crc:08X} for {name!r}"
            )

        body, content_type = _encode_multipart("file", name, data)
        req = urllib.request.Request(
            self._url(name),
            data=body,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            method="POST",
        )
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise WiCANUploadError(
                f"upload of {name!r} failed: HTTP {exc.code} from {self.host}: {detail}"
            ) from exc
        except (urllib.error.URLError, socket.error, OSError) as exc:
            raise WiCANUploadError(
                f"upload of {name!r} failed: cannot reach {self.host}: {exc}"
            ) from exc

        if status != 200:
            raise WiCANUploadError(
                f"upload of {name!r} returned HTTP {status} from {self.host}"
            )

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WiCANUploadError(
                f"upload of {name!r}: device reply was not JSON: {raw[:200]!r}"
            ) from exc

        bytes_written = payload.get("bytes_written", payload.get("bytes"))
        if bytes_written is None:
            raise WiCANUploadError(
                f"upload of {name!r}: reply missing bytes_written: {raw[:200]!r}"
            )
        if int(bytes_written) != len(data):
            raise WiCANUploadError(
                f"upload of {name!r}: device stored {bytes_written} bytes, sent {len(data)}"
            )

        dev_crc = _coerce_crc32(payload.get("crc32"))
        if dev_crc is None:
            raise WiCANUploadError(
                f"upload of {name!r}: reply missing/unparseable crc32: {raw[:200]!r}"
            )
        if dev_crc != host_crc:
            raise WiCANUploadError(
                f"upload of {name!r}: CRC mismatch — device 0x{dev_crc:08X} != host 0x{host_crc:08X}"
            )

        logger.info(
            "Uploaded %s: %d bytes, crc32=0x%08X (verified)", name, len(data), host_crc
        )
        return UploadResult(
            name=name, bytes_written=len(data), crc32=host_crc, raw_response=raw
        )

    # --- package helpers ----------------------------------------------------

    def upload_package(self, pkg, *, timeout_s: Optional[float] = None) -> UploadResult:
        """Upload a :class:`FlashPackage`'s staged image and verify it.

        Uses the manifest's own ``image_crc32``/``image_len`` as the expectation,
        so this catches a packaging/transport mismatch end to end.
        """
        m = pkg.manifest
        if len(pkg.image) != m["image_len"]:
            raise WiCANUploadError(
                f"package image_len {m['image_len']} != actual {len(pkg.image)}"
            )
        return self.upload_bytes(
            pkg.staged_filename,
            pkg.image,
            expected_crc32=m["image_crc32"],
            timeout_s=timeout_s,
        )

    def upload_manifest(
        self, pkg, *, timeout_s: Optional[float] = None
    ) -> UploadResult:
        """Upload the manifest as a JSON sidecar next to the staged image.

        Sidecar name = the staged image stem + ``.json`` (e.g.
        ``SW-..._20260623-1745.json``). Whether the firmware reads the plan from
        this sidecar or from the ``W`` trigger command is finalized in Phase 5;
        this provides the sidecar option and verifies its upload either way.
        """
        stem, _ = os.path.splitext(pkg.staged_filename)
        data = manifest_json(pkg)
        return self.upload_bytes(f"{stem}.json", data, timeout_s=timeout_s)


def manifest_json(pkg) -> bytes:
    """Canonical, deterministic JSON encoding of a package manifest (UTF-8)."""
    return json.dumps(pkg.manifest, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
