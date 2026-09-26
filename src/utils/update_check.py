"""Check GitHub for a newer NC Flash release and download its installer (issue #104).

Standard library only (no Qt): the UI runs these calls on a worker thread
(``src/ui/app_updater.py``) and unit tests drive them with a fake ``urlopen``.

Trust model:

- The release metadata comes from one unauthenticated HTTPS call to the
  GitHub releases API for this project's repository.
- Only an installer asset named exactly ``NCFlash-<version>-Setup.exe`` and
  hosted under this repository's own release-download URL is ever offered.
- GitHub publishes a SHA-256 ``digest`` for every release asset. An asset
  without one is not offered for install. The download must match that
  digest and the published size, byte for byte, before it is kept.

The digest comes over the same HTTPS channel as the file, so it proves the
download is complete and uncorrupted, not that the release itself is
genuine. The installer is not code-signed yet.
"""

import hashlib
import http.client
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

GITHUB_REPO = "cdufresne81/nc-flash"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"
#: Every installer URL we accept starts with this (followed by ``<tag>/``).
RELEASE_DOWNLOAD_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"

#: The API answer is a few KB; anything far larger is not a release object.
_MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")


class UpdateError(Exception):
    """An update check or download failed. The message is user-facing."""


class UpdateCancelled(UpdateError):
    """The user cancelled a running download."""


@dataclass(frozen=True)
class InstallerAsset:
    """The Windows installer attached to a release."""

    name: str
    url: str
    size: int
    sha256: str  # lowercase hex


@dataclass(frozen=True)
class ReleaseInfo:
    """The latest published release, as far as the updater cares."""

    version: str  # "2.17.0" (no "v")
    tag: str  # "v2.17.0"
    notes: str  # release body (the CHANGELOG section, Markdown)
    page_url: str
    installer: Optional[InstallerAsset]


def parse_version(text: str) -> Optional[tuple]:
    """``"v2.17.0"`` / ``"2.17.0"`` -> ``(2, 17, 0)``; anything else -> None.

    Pre-release suffixes (``2.17.0-rc1``) and the source-tree ``"dev"`` are
    deliberately not versions: they are never offered and never compared.
    """
    match = _VERSION_RE.fullmatch((text or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def is_release_build(current_version: str) -> bool:
    """True for a version stamped by the release workflow (not ``"dev"``)."""
    return parse_version(current_version) is not None


def is_newer(release_version: str, current_version: str) -> bool:
    """True when *release_version* is strictly newer than *current_version*.

    False whenever either side is not a plain ``X.Y.Z`` version.
    """
    release = parse_version(release_version)
    current = parse_version(current_version)
    if release is None or current is None:
        return False
    return release > current


def can_self_install() -> bool:
    """Whether this process can run a downloaded installer over itself.

    Only the packaged Windows build: the installer is a Windows ``.exe``, and
    a source checkout is updated with git, not by an installer.
    """
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def parse_release(data: dict) -> ReleaseInfo:
    """Turn a GitHub ``releases/latest`` JSON object into a :class:`ReleaseInfo`.

    Raises :class:`UpdateError` when the object is not a usable release.
    ``installer`` is None when the release has no acceptable installer asset.
    """
    if not isinstance(data, dict):
        raise UpdateError("GitHub returned an unexpected answer")
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("The latest GitHub release is not a final release")
    tag = str(data.get("tag_name") or "")
    parsed = parse_version(tag)
    if parsed is None:
        raise UpdateError(f"The latest release has an unrecognised tag: {tag!r}")
    version = ".".join(str(part) for part in parsed)

    page_url = str(data.get("html_url") or "")
    if not page_url.startswith(RELEASES_PAGE_URL + "/"):
        page_url = RELEASES_PAGE_URL

    return ReleaseInfo(
        version=version,
        tag=tag,
        notes=str(data.get("body") or ""),
        page_url=page_url,
        installer=_find_installer(data.get("assets"), version, tag),
    )


def _find_installer(assets, version: str, tag: str) -> Optional[InstallerAsset]:
    """The ``NCFlash-<version>-Setup.exe`` asset, if it passes every check."""
    expected_name = f"NCFlash-{version}-Setup.exe"
    expected_url = f"{RELEASE_DOWNLOAD_PREFIX}{tag}/{expected_name}"
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict) or asset.get("name") != expected_name:
            continue
        if asset.get("browser_download_url") != expected_url:
            logger.warning("Update: installer asset has an unexpected URL, ignored")
            return None
        digest = _DIGEST_RE.fullmatch(str(asset.get("digest") or ""))
        if digest is None:
            logger.info("Update: installer asset has no SHA-256 digest, ignored")
            return None
        size = asset.get("size")
        if not isinstance(size, int) or size <= 0:
            logger.warning("Update: installer asset has no valid size, ignored")
            return None
        return InstallerAsset(
            name=expected_name, url=expected_url, size=size, sha256=digest.group(1)
        )
    return None


def _user_agent(current_version: str) -> str:
    return f"NCFlash/{current_version} (+https://github.com/{GITHUB_REPO})"


def fetch_latest_release(
    current_version: str, timeout: float = 10.0, urlopen=urllib.request.urlopen
) -> ReleaseInfo:
    """One GET of the latest release. Raises :class:`UpdateError` on any failure."""
    request = urllib.request.Request(
        RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": _user_agent(current_version),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_API_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise UpdateError(
                "GitHub is limiting requests right now. Try again later."
            ) from e
        if e.code == 404:
            raise UpdateError("No published release was found on GitHub.") from e
        raise UpdateError(f"GitHub answered HTTP {e.code}.") from e
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        raise UpdateError(f"Could not reach GitHub ({reason}).") from e
    if len(raw) > _MAX_API_RESPONSE_BYTES:
        raise UpdateError("GitHub returned an unexpected answer")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise UpdateError("GitHub returned an unexpected answer") from e
    return parse_release(data)


def verify_installer(path: Path, asset: InstallerAsset) -> bool:
    """True when *path* already holds exactly the published installer."""
    try:
        if path.stat().st_size != asset.size:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest() == asset.sha256
    except OSError:
        return False


def remove_stale_downloads(dest_dir: Path, keep_name: str = "") -> None:
    """Delete earlier installer downloads (``NCFlash-*-Setup.exe[.part]``).

    Only files matching that pattern inside *dest_dir* are touched.
    """
    try:
        candidates = list(dest_dir.glob("NCFlash-*-Setup.exe*"))
    except OSError:
        return
    for path in candidates:
        if path.name == keep_name or not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as e:
            logger.debug("Update: could not remove old download %s: %s", path, e)


def download_installer(
    asset: InstallerAsset,
    dest_dir: Path,
    current_version: str,
    progress: Optional[Callable[[int, int], None]] = None,
    should_abort: Optional[Callable[[], bool]] = None,
    timeout: float = 30.0,
    urlopen=urllib.request.urlopen,
) -> Path:
    """Download *asset* into *dest_dir* and verify it; return the final path.

    The bytes go to ``<name>.part`` and are renamed only after the size and
    SHA-256 both match what GitHub published, so the final name never holds a
    partial or corrupted file. Any failure removes the ``.part`` file.
    A previous verified download of the same installer is reused.

    Raises :class:`UpdateCancelled` when *should_abort* returns True, and
    :class:`UpdateError` on any other failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / asset.name
    remove_stale_downloads(dest_dir, keep_name=asset.name)
    if verify_installer(final, asset):
        logger.info("Update: reusing the verified download %s", final)
        if progress:
            progress(asset.size, asset.size)
        return final

    part = final.with_name(final.name + ".part")
    request = urllib.request.Request(
        asset.url, headers={"User-Agent": _user_agent(current_version)}
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            sha256 = _stream_to_file(response, part, asset, progress, should_abort)
        if sha256 != asset.sha256:
            raise UpdateError(
                "The downloaded installer failed its checksum check and was "
                "deleted. Try again, or download it from the release page."
            )
        os.replace(part, final)
    except UpdateError:
        _unlink_quietly(part)
        raise
    except urllib.error.HTTPError as e:
        _unlink_quietly(part)
        raise UpdateError(f"The download failed (HTTP {e.code}).") from e
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        _unlink_quietly(part)
        reason = getattr(e, "reason", e)
        raise UpdateError(f"The download failed ({reason}).") from e
    except BaseException:
        _unlink_quietly(part)
        raise
    logger.info("Update: downloaded and verified %s", final)
    return final


def _stream_to_file(
    response, part: Path, asset: InstallerAsset, progress, should_abort
):
    """Write *response* to *part*, enforcing the published size; return its SHA-256."""
    # GitHub redirects to its asset CDN; never follow it off HTTPS.
    final_url = response.geturl() if hasattr(response, "geturl") else ""
    if final_url and not final_url.startswith("https://"):
        raise UpdateError("The download was redirected off HTTPS.")
    length = response.headers.get("Content-Length")
    if length is not None and length.isdigit() and int(length) != asset.size:
        raise UpdateError("The download size does not match the published installer.")

    # read1 returns whatever arrived (up to a chunk); read() would wait for
    # a full 64 KiB, so a trickling link could outlast every timeout.
    read = getattr(response, "read1", response.read)
    digest = hashlib.sha256()
    done = 0
    if progress:
        progress(0, asset.size)
    with open(part, "wb") as out:
        while True:
            if should_abort and should_abort():
                raise UpdateCancelled("Download cancelled.")
            chunk = read(_CHUNK_BYTES)
            if not chunk:
                break
            done += len(chunk)
            if done > asset.size:
                raise UpdateError(
                    "The download is larger than the published installer."
                )
            digest.update(chunk)
            out.write(chunk)
            if progress:
                progress(done, asset.size)
    if done != asset.size:
        raise UpdateError(
            "The download ended early. Check your connection and try again."
        )
    return digest.hexdigest()


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug("Update: could not remove %s: %s", path, e)


def launch_installer(path: Path) -> None:
    """Start the installer as its own process (Windows only).

    ``os.startfile`` goes through the Windows shell, so the installer can ask
    for elevation when the previous install needs it. Raises ``OSError`` if
    the installer could not be started (for example, the user declined UAC).
    """
    # The installer's last page relaunches NC Flash. Tell that new PyInstaller
    # process to start clean instead of inheriting this frozen app's runtime
    # environment (this process exits right after, so nothing else sees it).
    os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    os.startfile(str(path))
