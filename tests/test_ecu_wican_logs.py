"""WiCAN trip-log sync (#83): shared HTTP layer + incremental log client.

Drives :mod:`src.ecu.wican_http` and :class:`WiCANLogClient` against a tiny
in-process HTTP server emulating the firmware's ``/csv_list`` /
``/download_csv`` / ``/csv_status`` endpoints — real GET round-trips, no
hardware. Covers the happy path plus every way the sync must refuse to be
fooled: partial/mismatched transfers (atomic ``.part`` contract), unsafe
device-supplied names, the still-growing active trip file, clockless-device
name collisions, and quiet handling of empty/unreachable devices.
"""

import json
import socket
import threading
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.wican_http import (
    WiCANHttpError,
    download_to_file,
    get_json,
    sanitize_basename,
)
from src.ecu.wican_logs import WiCANLogClient, WiCANLogsError
from src.utils.workspace import _SUBDIRS

# --- fake device -------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        srv = self.server
        if parsed.path == "/csv_list":
            entries = [
                {
                    "name": name,
                    "size": srv.advertised.get(name, len(data)),
                    "mtime": 1750000000,
                }
                for name, data in srv.files.items()
            ]
            self._json(200, {"files": entries})
        elif parsed.path == "/csv_status":
            if srv.status_error:
                self._json(500, {"error": "boom"})
                return
            self._json(
                200,
                {
                    "session_active": srv.active is not None,
                    "file": srv.active or "",
                    "mode": "auto",
                    "columns": 5,
                },
            )
        elif parsed.path == "/download_csv":
            name = (qs.get("file") or [""])[0]
            data = srv.files.get(name)
            if data is None:
                self._json(404, {"error": "not found"})
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        else:
            self._json(404, {"error": "no route"})

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        pass


@contextmanager
def _device(files=None, active=None, advertised=None):
    """A fake WiCAN serving the csv_logger endpoints on an ephemeral port.

    ``files`` maps name -> bytes (dict order = the device's newest-first);
    ``active`` is the abspath of the open trip file (None = no session);
    ``advertised`` overrides the ``size`` /csv_list reports (to lie).
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.files = dict(files or {})
    httpd.active = active
    httpd.advertised = dict(advertised or {})
    httpd.status_error = False
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        host, port = httpd.server_address
        yield WiCANLogClient(host, http_port=port, timeout_s=5.0), httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)


def _closed_port() -> int:
    """A port nothing is listening on (bound then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _no_part_residue(directory):
    assert list(directory.rglob("*.part")) == []


# --- wican_http: basename sanitization ---------------------------------------


class TestSanitizeBasename:
    @pytest.mark.parametrize(
        "name",
        ["trip_2026-07-10_08-30-00.csv", "unknown_time_123456.csv", "a.b-c_d.csv"],
    )
    def test_valid_names_pass_through(self, name):
        assert sanitize_basename(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            ".",
            "..",
            "a/b.csv",
            "a\\b.csv",
            "../up.csv",
            "..\\up.csv",
            "trip..csv",  # over-strict by design: any '..' is suspect
            "trip:2.csv",
            "trip?.csv",
            "trip\x01.csv",
            " lead.csv",
            "trail.csv ",
        ],
    )
    def test_unsafe_names_rejected(self, name):
        with pytest.raises(WiCANHttpError):
            sanitize_basename(name)


# --- wican_http: atomic download ----------------------------------------------


class TestDownloadToFile:
    def test_happy_path_atomic(self, tmp_path):
        data = b"time,rpm\n1,800\n" * 100
        with _device({"a.csv": data}) as (client, _):
            dest = download_to_file(
                client._url("/download_csv", {"file": "a.csv"}),
                tmp_path / "a.csv",
                expected_size=len(data),
            )
        assert dest.read_bytes() == data
        _no_part_residue(tmp_path)

    def test_size_mismatch_rejected_no_residue(self, tmp_path):
        data = b"x" * 512
        with _device({"a.csv": data}) as (client, _):
            with pytest.raises(WiCANHttpError, match="advertised"):
                download_to_file(
                    client._url("/download_csv", {"file": "a.csv"}),
                    tmp_path / "a.csv",
                    expected_size=len(data) + 5,
                )
        assert not (tmp_path / "a.csv").exists()
        _no_part_residue(tmp_path)

    def test_http_error_no_residue(self, tmp_path):
        with _device({}) as (client, _):
            with pytest.raises(WiCANHttpError, match="404"):
                download_to_file(
                    client._url("/download_csv", {"file": "missing.csv"}),
                    tmp_path / "missing.csv",
                )
        assert list(tmp_path.iterdir()) == []

    def test_unreachable_raises(self, tmp_path):
        port = _closed_port()
        with pytest.raises(WiCANHttpError):
            download_to_file(
                f"http://127.0.0.1:{port}/download_csv?file=a.csv",
                tmp_path / "a.csv",
                timeout_s=2.0,
            )
        assert list(tmp_path.iterdir()) == []

    def test_get_json_unreachable_raises(self):
        port = _closed_port()
        with pytest.raises(WiCANHttpError):
            get_json(f"http://127.0.0.1:{port}/csv_list", timeout_s=2.0)


# --- WiCANLogClient.download_new ----------------------------------------------


class TestDownloadNew:
    def test_downloads_all_new_files(self, tmp_path):
        files = {"b.csv": b"newer\n" * 10, "a.csv": b"older\n" * 20}
        with _device(files) as (client, _):
            result = client.download_new(tmp_path)
        assert [p.name for p in result.downloaded] == ["b.csv", "a.csv"]
        assert result.skipped == []
        assert (tmp_path / "a.csv").read_bytes() == files["a.csv"]
        assert (tmp_path / "b.csv").read_bytes() == files["b.csv"]
        _no_part_residue(tmp_path)

    def test_rerun_is_incremental(self, tmp_path):
        files = {"a.csv": b"data\n" * 5}
        with _device(files) as (client, _):
            client.download_new(tmp_path)
            result = client.download_new(tmp_path)
        assert result.downloaded == []
        assert result.skipped == ["a.csv"]

    def test_active_file_skipped_then_downloaded_after_close(self, tmp_path):
        files = {"open.csv": b"growing\n", "done.csv": b"closed trip\n"}
        with _device(files, active="/sdcard/logs/open.csv") as (client, httpd):
            first = client.download_new(tmp_path)
            assert [p.name for p in first.downloaded] == ["done.csv"]
            assert first.skipped == ["open.csv"]
            # Trip closes -> next run picks it up.
            httpd.active = None
            second = client.download_new(tmp_path)
        assert [p.name for p in second.downloaded] == ["open.csv"]
        assert "done.csv" in second.skipped

    def test_skip_active_false_downloads_open_file(self, tmp_path):
        with _device({"open.csv": b"x"}, active="/sdcard/logs/open.csv") as (
            client,
            _,
        ):
            result = client.download_new(tmp_path, skip_active=False)
        assert [p.name for p in result.downloaded] == ["open.csv"]

    def test_status_failure_fails_run_rather_than_guess(self, tmp_path):
        # If the active file can't be determined, downloading a growing CSV is
        # the risk -> the run must raise, not guess.
        with _device({"a.csv": b"x"}) as (client, httpd):
            httpd.status_error = True
            with pytest.raises(WiCANHttpError):
                client.download_new(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_advertised_size_lie_aborts_but_keeps_prior_files(self, tmp_path):
        files = {"good.csv": b"fine\n" * 4, "bad.csv": b"short"}
        with _device(files, advertised={"bad.csv": 9999}) as (client, _):
            with pytest.raises(WiCANHttpError):
                client.download_new(tmp_path)
            # The file downloaded before the failure survives (idempotent rerun).
            assert (tmp_path / "good.csv").read_bytes() == files["good.csv"]
            assert not (tmp_path / "bad.csv").exists()
            _no_part_residue(tmp_path)

    def test_unsafe_device_name_skipped_not_written(self, tmp_path):
        dest = tmp_path / "logs"
        dest.mkdir()
        with _device({"../evil.csv": b"pwn", "ok.csv": b"fine"}) as (client, _):
            result = client.download_new(dest)
        assert "../evil.csv" in result.skipped
        assert [p.name for p in result.downloaded] == ["ok.csv"]
        # Nothing escaped the destination directory.
        assert list(tmp_path.iterdir()) == [dest]
        assert sorted(p.name for p in dest.iterdir()) == ["ok.csv"]

    def test_clockless_collision_lands_with_suffix(self, tmp_path):
        # A pre-existing local file with the same name but different size is a
        # DIFFERENT trip (clockless device reuses names across reboots).
        (tmp_path / "unknown_time_42.csv").write_bytes(b"previous boot's trip\n")
        remote = b"this boot's trip\n" * 3
        with _device({"unknown_time_42.csv": remote}) as (client, _):
            first = client.download_new(tmp_path)
            assert [p.name for p in first.downloaded] == ["unknown_time_42-2.csv"]
            assert (tmp_path / "unknown_time_42-2.csv").read_bytes() == remote
            # Original untouched.
            assert (
                tmp_path / "unknown_time_42.csv"
            ).read_bytes() == b"previous boot's trip\n"
            # Rerun: the suffixed copy satisfies (name, size) -> skip.
            second = client.download_new(tmp_path)
        assert second.downloaded == []
        assert second.skipped == ["unknown_time_42.csv"]

    def test_same_name_same_size_is_already_downloaded(self, tmp_path):
        data = b"12345"
        (tmp_path / "a.csv").write_bytes(data)
        with _device({"a.csv": b"54321"}) as (client, _):  # same size, diff bytes
            result = client.download_new(tmp_path)
        assert result.downloaded == []
        assert result.skipped == ["a.csv"]
        assert (tmp_path / "a.csv").read_bytes() == data  # never clobbered

    def test_abort_between_files_keeps_partial_result(self, tmp_path):
        # abort_cb is polled at the top of each file AND between download
        # chunks. Keying it on "a.csv exists" lets file a complete (only its
        # .part exists mid-download), then aborts before b starts.
        files = {"a.csv": b"first\n" * 4, "b.csv": b"second\n" * 4}
        with _device(files) as (client, _):
            result = client.download_new(
                tmp_path, abort_cb=lambda: (tmp_path / "a.csv").exists()
            )
        assert [p.name for p in result.downloaded] == ["a.csv"]
        assert not (tmp_path / "b.csv").exists()
        _no_part_residue(tmp_path)

    def test_abort_immediately_downloads_nothing(self, tmp_path):
        with _device({"a.csv": b"data"}) as (client, _):
            result = client.download_new(tmp_path, abort_cb=lambda: True)
        assert result.downloaded == []
        assert list(tmp_path.iterdir()) == []

    def test_empty_device_is_quiet(self, tmp_path):
        with _device({}) as (client, _):
            result = client.download_new(tmp_path)
        assert result.downloaded == []
        assert result.skipped == []

    def test_unreachable_device_raises_ecu_error(self, tmp_path):
        client = WiCANLogClient("127.0.0.1", http_port=_closed_port(), timeout_s=2.0)
        with pytest.raises(WiCANHttpError):
            client.download_new(tmp_path)

    def test_malformed_list_raises(self, tmp_path):
        class _BadHandler(_Handler):
            def do_GET(self):
                self._json(200, {"nope": True})

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _BadHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            host, port = httpd.server_address
            client = WiCANLogClient(host, http_port=port, timeout_s=5.0)
            with pytest.raises(WiCANLogsError, match="malformed"):
                client.download_new(tmp_path)
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=2)


# --- settings / workspace plumbing ---------------------------------------------


@pytest.fixture
def _settings():
    """AppSettings over an in-memory store (never touches real QSettings)."""
    from src.utils.settings import AppSettings

    store = {}

    def value(key, default=None, type=None):  # noqa: A002 - QSettings signature
        return store.get(key, default)

    with patch("src.utils.settings.QSettings") as qs:
        inst = MagicMock()
        inst.value = value
        inst.setValue = lambda key, val: store.__setitem__(key, val)
        qs.return_value = inst
        yield AppSettings(), store


class TestLogsPlumbing:
    def test_logs_is_a_workspace_subdir(self):
        assert "logs" in _SUBDIRS

    def test_logs_directory_defaults_under_workspace(self, _settings):
        settings, _ = _settings
        from pathlib import Path

        assert Path(settings.get_logs_directory()).name == "logs"

    def test_auto_download_defaults_on_and_round_trips(self, _settings):
        settings, store = _settings
        assert settings.get_wican_auto_download_logs() is True
        settings.set_wican_auto_download_logs(False)
        assert store["ecu/wican_auto_download_logs"] is False

    def test_is_wican_adapter_tracks_adapter_selection(self, _settings):
        # The single predicate for WiCAN-only affordances — callers never
        # compare the raw adapter string.
        settings, _ = _settings
        assert settings.is_wican_adapter() is False  # j2534 is the default
        settings.set_ecu_adapter("wican")
        assert settings.is_wican_adapter() is True
        settings.set_ecu_adapter("j2534")
        assert settings.is_wican_adapter() is False
