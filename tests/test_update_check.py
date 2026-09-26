"""Tests for the update check / installer download (src/utils/update_check.py)."""

import hashlib
import http.client
import io
import json
import urllib.error

import pytest

from src.utils import update_check as uc
from src.utils.update_check import (
    InstallerAsset,
    UpdateCancelled,
    UpdateError,
    download_installer,
    fetch_latest_release,
    is_newer,
    is_release_build,
    parse_release,
    parse_version,
)

PAYLOAD = b"MZ fake installer " * 10000  # ~180 KB, several 64 KiB chunks
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _release_json(tag="v2.18.0", **overrides):
    version = tag.lstrip("v")
    name = f"NCFlash-{version}-Setup.exe"
    data = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/cdufresne81/nc-flash/releases/tag/{tag}",
        "body": "### Added\n- Things",
        "assets": [
            {
                "name": f"NCFlash-{version}-linux-x86_64.tar.gz",
                "browser_download_url": uc.RELEASE_DOWNLOAD_PREFIX
                + f"{tag}/NCFlash-{version}-linux-x86_64.tar.gz",
                "size": 10,
                "digest": "sha256:" + "a" * 64,
            },
            {
                "name": name,
                "browser_download_url": f"{uc.RELEASE_DOWNLOAD_PREFIX}{tag}/{name}",
                "size": len(PAYLOAD),
                "digest": f"sha256:{PAYLOAD_SHA}",
            },
        ],
    }
    data.update(overrides)
    return data


class _FakeResponse:
    def __init__(self, body: bytes, url="https://objects.example/x", length=None):
        self._stream = io.BytesIO(body)
        self._url = url
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def read(self, n=-1):
        return self._stream.read(n)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(response=None, exc=None, seen=None):
    def urlopen(request, timeout=None):
        if seen is not None:
            seen.append(request)
        if exc is not None:
            raise exc
        return response

    return urlopen


def _asset(size=len(PAYLOAD), sha=PAYLOAD_SHA):
    name = "NCFlash-2.18.0-Setup.exe"
    return InstallerAsset(
        name=name,
        url=f"{uc.RELEASE_DOWNLOAD_PREFIX}v2.18.0/{name}",
        size=size,
        sha256=sha,
    )


# --- versions --------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("v2.17.0", (2, 17, 0)),
        ("2.17.0", (2, 17, 0)),
        (" v10.0.1 ", (10, 0, 1)),
        ("dev", None),
        ("0.0.0-dev", None),
        ("v2.17.0-rc1", None),
        ("v2.17", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


def test_is_newer_compares_numerically():
    assert is_newer("2.10.0", "2.9.9")
    assert is_newer("v3.0.0", "2.99.99")
    assert not is_newer("2.17.0", "2.17.0")
    assert not is_newer("2.16.0", "2.17.0")


def test_is_newer_false_for_non_release_versions():
    assert not is_newer("2.18.0", "dev")
    assert not is_newer("2.18.0-rc1", "2.17.0")


def test_is_release_build():
    assert is_release_build("2.17.0")
    assert not is_release_build("dev")
    assert not is_release_build("0.0.0-dev")


# --- parse_release ---------------------------------------------------------


def test_parse_release_picks_the_windows_installer():
    release = parse_release(_release_json())
    assert release.version == "2.18.0"
    assert release.tag == "v2.18.0"
    assert release.notes.startswith("### Added")
    assert release.page_url.endswith("/releases/tag/v2.18.0")
    assert release.installer == _asset()


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_parse_release_rejects_non_final_releases(flag):
    with pytest.raises(UpdateError):
        parse_release(_release_json(**{flag: True}))


def test_parse_release_rejects_unrecognised_tag():
    with pytest.raises(UpdateError):
        parse_release(_release_json(tag="nightly"))


def test_parse_release_rejects_non_object():
    with pytest.raises(UpdateError):
        parse_release(["not", "a", "release"])


def test_installer_ignored_when_url_points_elsewhere():
    data = _release_json()
    data["assets"][1][
        "browser_download_url"
    ] = "https://evil.example/NCFlash-2.18.0-Setup.exe"
    assert parse_release(data).installer is None


def test_installer_ignored_when_url_is_another_tag():
    data = _release_json()
    data["assets"][1]["browser_download_url"] = (
        uc.RELEASE_DOWNLOAD_PREFIX + "v2.17.0/NCFlash-2.18.0-Setup.exe"
    )
    assert parse_release(data).installer is None


@pytest.mark.parametrize("digest", [None, "", "md5:abc", "sha256:XYZ", "sha256:ab"])
def test_installer_ignored_without_a_valid_sha256(digest):
    data = _release_json()
    data["assets"][1]["digest"] = digest
    assert parse_release(data).installer is None


@pytest.mark.parametrize("size", [0, -1, None, "123"])
def test_installer_ignored_without_a_valid_size(size):
    data = _release_json()
    data["assets"][1]["size"] = size
    assert parse_release(data).installer is None


def test_installer_absent_when_release_has_none():
    data = _release_json()
    data["assets"] = data["assets"][:1]
    assert parse_release(data).installer is None


def test_foreign_page_url_falls_back_to_releases_page():
    release = parse_release(_release_json(html_url="https://evil.example/"))
    assert release.page_url == uc.RELEASES_PAGE_URL


# --- fetch_latest_release --------------------------------------------------


def test_fetch_latest_release_parses_answer_and_sends_user_agent():
    seen = []
    body = json.dumps(_release_json()).encode()
    release = fetch_latest_release(
        "2.17.0", urlopen=_opener(_FakeResponse(body), seen=seen)
    )
    assert release.version == "2.18.0"
    assert seen[0].full_url == uc.RELEASES_API_URL
    assert "NCFlash/2.17.0" in seen[0].get_header("User-agent")


@pytest.mark.parametrize("code, fragment", [(403, "limiting"), (404, "No published")])
def test_fetch_latest_release_http_errors(code, fragment):
    exc = urllib.error.HTTPError(uc.RELEASES_API_URL, code, "x", {}, None)
    with pytest.raises(UpdateError, match=fragment):
        fetch_latest_release("2.17.0", urlopen=_opener(exc=exc))


def test_fetch_latest_release_offline():
    exc = urllib.error.URLError("no route to host")
    with pytest.raises(UpdateError, match="Could not reach GitHub"):
        fetch_latest_release("2.17.0", urlopen=_opener(exc=exc))


def test_fetch_latest_release_bad_json():
    with pytest.raises(UpdateError):
        fetch_latest_release("2.17.0", urlopen=_opener(_FakeResponse(b"<html>")))


# --- download_installer ----------------------------------------------------


def test_download_verifies_and_renames(tmp_path):
    calls = []
    path = download_installer(
        _asset(),
        tmp_path,
        "2.17.0",
        progress=lambda done, total: calls.append((done, total)),
        urlopen=_opener(_FakeResponse(PAYLOAD, length=len(PAYLOAD))),
    )
    assert path == tmp_path / "NCFlash-2.18.0-Setup.exe"
    assert path.read_bytes() == PAYLOAD
    assert not (tmp_path / "NCFlash-2.18.0-Setup.exe.part").exists()
    assert calls[0] == (0, len(PAYLOAD))
    assert calls[-1] == (len(PAYLOAD), len(PAYLOAD))


def test_download_checksum_mismatch_leaves_nothing(tmp_path):
    with pytest.raises(UpdateError, match="checksum"):
        download_installer(
            _asset(sha="0" * 64),
            tmp_path,
            "2.17.0",
            urlopen=_opener(_FakeResponse(PAYLOAD)),
        )
    assert list(tmp_path.iterdir()) == []


def test_download_short_body_is_rejected(tmp_path):
    with pytest.raises(UpdateError, match="ended early"):
        download_installer(
            _asset(),
            tmp_path,
            "2.17.0",
            urlopen=_opener(_FakeResponse(PAYLOAD[:-1])),
        )
    assert list(tmp_path.iterdir()) == []


def test_download_oversized_body_is_rejected(tmp_path):
    with pytest.raises(UpdateError, match="larger"):
        download_installer(
            _asset(),
            tmp_path,
            "2.17.0",
            urlopen=_opener(_FakeResponse(PAYLOAD + b"extra")),
        )
    assert list(tmp_path.iterdir()) == []


def test_download_content_length_mismatch_is_rejected(tmp_path):
    with pytest.raises(UpdateError, match="size"):
        download_installer(
            _asset(),
            tmp_path,
            "2.17.0",
            urlopen=_opener(_FakeResponse(PAYLOAD, length=5)),
        )
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_redirect_off_https(tmp_path):
    with pytest.raises(UpdateError, match="HTTPS"):
        download_installer(
            _asset(),
            tmp_path,
            "2.17.0",
            urlopen=_opener(_FakeResponse(PAYLOAD, url="http://plain.example/x")),
        )
    assert list(tmp_path.iterdir()) == []


def test_download_cancel_removes_partial_file(tmp_path):
    calls = {"n": 0}

    def should_abort():
        calls["n"] += 1
        return calls["n"] > 1  # after the first chunk

    with pytest.raises(UpdateCancelled):
        download_installer(
            _asset(),
            tmp_path,
            "2.17.0",
            should_abort=should_abort,
            urlopen=_opener(_FakeResponse(PAYLOAD)),
        )
    assert list(tmp_path.iterdir()) == []


def test_download_network_error_becomes_update_error(tmp_path):
    with pytest.raises(UpdateError, match="download failed"):
        download_installer(
            _asset(),
            tmp_path,
            "2.17.0",
            urlopen=_opener(exc=urllib.error.URLError("reset")),
        )


def test_download_reuses_a_verified_copy(tmp_path):
    (tmp_path / "NCFlash-2.18.0-Setup.exe").write_bytes(PAYLOAD)

    def must_not_download(*a, **k):
        raise AssertionError("should reuse the verified file")

    path = download_installer(_asset(), tmp_path, "2.17.0", urlopen=must_not_download)
    assert path.read_bytes() == PAYLOAD


def test_download_replaces_a_corrupt_copy(tmp_path):
    (tmp_path / "NCFlash-2.18.0-Setup.exe").write_bytes(b"x" * len(PAYLOAD))
    path = download_installer(
        _asset(), tmp_path, "2.17.0", urlopen=_opener(_FakeResponse(PAYLOAD))
    )
    assert path.read_bytes() == PAYLOAD


def test_download_removes_older_installers_only(tmp_path):
    (tmp_path / "NCFlash-2.17.0-Setup.exe").write_bytes(b"old")
    (tmp_path / "NCFlash-2.16.0-Setup.exe.part").write_bytes(b"old part")
    (tmp_path / "unrelated.txt").write_bytes(b"keep me")
    download_installer(
        _asset(), tmp_path, "2.17.0", urlopen=_opener(_FakeResponse(PAYLOAD))
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "NCFlash-2.18.0-Setup.exe",
        "unrelated.txt",
    ]


def test_fetch_latest_release_truncated_answer():
    exc = http.client.IncompleteRead(b"{")
    with pytest.raises(UpdateError, match="Could not reach GitHub"):
        fetch_latest_release("2.17.0", urlopen=_opener(exc=exc))


def test_download_truncated_transfer_becomes_update_error(tmp_path):
    exc = http.client.IncompleteRead(b"partial")
    with pytest.raises(UpdateError, match="download failed"):
        download_installer(_asset(), tmp_path, "2.17.0", urlopen=_opener(exc=exc))


class _TrickleResponse(_FakeResponse):
    """read() would block for a full chunk on a slow link; read1 must be used."""

    def read(self, n=-1):
        raise AssertionError("download must use read1, not a blocking read")

    def read1(self, n=-1):
        return self._stream.read(min(n, 1000))  # small pieces, like a slow link


def test_download_uses_read1_so_a_slow_link_cannot_block_a_chunk(tmp_path):
    path = download_installer(
        _asset(), tmp_path, "2.17.0", urlopen=_opener(_TrickleResponse(PAYLOAD))
    )
    assert path.read_bytes() == PAYLOAD
