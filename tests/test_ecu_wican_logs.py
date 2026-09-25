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
import re
import socket
import threading
import urllib.parse
from contextlib import contextmanager
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

from src.ecu.wican_http import (
    WiCANHttpError,
    download_to_file,
    get_json,
    sanitize_basename,
)
from src.ecu import wican_logs
from src.ecu.wican_logs import WiCANLogClient, WiCANLogsError
from src.utils.workspace import _SUBDIRS

# --- fake device -------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        srv = self.server
        if parsed.path == "/csv_list":
            mtimes = getattr(srv, "mtimes", {})
            entries = [
                {
                    "name": name,
                    "size": srv.advertised.get(name, len(data)),
                    "mtime": mtimes.get(name, 1750000000),
                }
                for name, data in srv.files.items()
            ]
            self._json(200, {"files": entries})
        elif parsed.path == "/files":
            self._files_list(srv, qs)
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
            if hasattr(srv, "downloads"):
                srv.downloads.append(name)
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

    # --- sd_filemgr emulation (/files), mirroring sd_filemgr.c ---------------

    def _files_list(self, srv, qs):
        if (qs.get("op") or ["list"])[0] != "list" or (qs.get("path") or [""])[
            0
        ] != "logs":
            self._json(400, {"error": "unexpected request"})
            return
        srv.fs_list_calls = getattr(srv, "fs_list_calls", 0) + 1
        hook = getattr(srv, "fs_list_hook", None)
        if hook is not None:
            hook(srv)  # lets a test change the card between listings
        entries = []
        for name, data in srv.files.items():
            mtime = getattr(srv, "fs_mtimes", {}).get(name, 1750000000)
            item = {"name": name, "mtime": mtime}
            if name in getattr(srv, "dirs", set()):
                item["type"] = "dir"  # a folder that happens to end in .csv
            else:
                item["type"] = "file"
                item["size"] = getattr(srv, "fs_sizes", {}).get(name, len(data))
                if srv.active == f"/sdcard/logs/{name}":
                    item["active"] = True
            entries.append(item)
        self._json(200, {"path": "logs", "sd_mounted": True, "entries": entries})

    def do_POST(self):
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        srv.posts.append(body)
        if urllib.parse.urlsplit(self.path).path != "/files":
            self._json(404, {"error": "no route"})
            return
        path = body.get("path", "")
        if body.get("op") != "delete" or not path.startswith("logs/"):
            self._json(400, {"error": "unexpected request"})
            return
        name = path.removeprefix("logs/")
        forced = getattr(srv, "delete_errors", {}).get(name)
        if forced:
            self._json(forced[0], {"error": forced[1]})
        elif name not in srv.files:
            self._json(404, {"error": "not found"})
        elif srv.active == f"/sdcard/logs/{name}":
            self._json(409, {"error": "file is being written"})
        elif getattr(srv, "delete_reply", None) is not None:
            self._json(200, srv.delete_reply)
        else:
            del srv.files[name]
            self._json(200, {"ok": True, "deleted": 1})

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
    httpd.posts = []  # every POST body received, in order
    httpd.downloads = []  # every /download_csv name served, in order
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

    def test_per_file_log_line_and_run_totals(self, tmp_path, caplog, monkeypatch):
        # #109: each completed file logs its size, time and speed; the result
        # carries the run's byte/time totals for the summary line.
        # Fake clock: Windows' monotonic ticks every ~15.6 ms, so a loopback
        # download can measure exactly 0.0 s and fail the elapsed check.
        ticks = iter(range(1000))
        monkeypatch.setattr(
            wican_logs, "time", SimpleNamespace(monotonic=lambda: next(ticks) * 0.5)
        )
        files = {"b.csv": b"n" * 3000, "a.csv": b"o" * 1000}
        with caplog.at_level("INFO", logger="src.ecu.wican_logs"):
            with _device(files) as (client, _):
                result = client.download_new(tmp_path)
        lines = [
            r.getMessage()
            for r in caplog.records
            if "Downloaded trip" in r.getMessage()
        ]
        assert len(lines) == 2
        assert re.fullmatch(
            r"Downloaded trip log b\.csv \(3 KB in \d+\.\ds, \d+(\.\d)? [KM]B/s\)",
            lines[0],
        )
        assert result.bytes_downloaded == 4000
        assert result.elapsed_s > 0
        # Device lists newest-first (b then a); MLV needs the reverse.
        assert [p.name for p in result.downloaded_oldest_first] == ["a.csv", "b.csv"]

    def test_nothing_downloaded_has_zero_totals(self, tmp_path):
        with _device({}) as (client, _):
            result = client.download_new(tmp_path)
        assert (result.bytes_downloaded, result.elapsed_s) == (0, 0.0)

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

    def test_abort_mid_file_returns_partial_result_not_error(self, tmp_path):
        # A Cancel during a large file lands between CHUNKS, inside
        # download_to_file — that must end the run like a between-files abort
        # (partial result returned), never surface as a transfer error.
        # The abort LATCHES once it sees the in-flight .part (deterministic:
        # it exists the moment the transfer opens), mirroring the production
        # signal — QThread.requestInterruption never un-requests.
        latched = []

        def abort_cb():
            if latched or (tmp_path / "big.csv.part").exists():
                latched.append(True)
            return bool(latched)

        files = {"big.csv": b"x" * (128 * 1024), "next.csv": b"y" * 16}
        with _device(files) as (client, _):
            result = client.download_new(tmp_path, abort_cb=abort_cb)
        assert result.downloaded == []
        assert not (tmp_path / "big.csv").exists()
        assert not (tmp_path / "next.csv").exists()  # run really ended
        _no_part_residue(tmp_path)

    def test_intra_run_collision_never_clobbers(self, tmp_path):
        # Data-loss regression guard: the plan resolves every target against
        # PRE-RUN disk state, so a collision-suffixed name ("t.csv" bumped to
        # "t-2.csv" past a stale local copy) must never land on another remote
        # log's literal name in the same run — the plan reserves each target.
        (tmp_path / "t.csv").write_bytes(b"z" * 100)  # older trip, same name
        files = {"t.csv": b"a" * 200, "t-2.csv": b"b" * 300}
        with _device(files) as (client, _):
            plan = client.plan(tmp_path)
            targets = [target for _, target in plan.to_download]
            assert len(set(targets)) == len(targets) == 2  # distinct targets
            result = client.download_new(tmp_path)
        assert len(result.downloaded) == 2
        # Every byte of both trips is on disk; the stale local copy untouched.
        assert (tmp_path / "t.csv").read_bytes() == b"z" * 100
        on_disk = sorted(p.read_bytes() for p in result.downloaded)
        assert on_disk == [b"a" * 200, b"b" * 300]

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


# --- byte progress (plan + per-chunk callbacks) ---------------------------------


class TestDownloadProgress:
    def test_download_to_file_reports_cumulative_bytes(self, tmp_path):
        data = b"x" * (150 * 1024)  # spans multiple 64 KiB chunks
        seen = []
        with _device({"a.csv": data}) as (client, _):
            download_to_file(
                client._url("/download_csv", {"file": "a.csv"}),
                tmp_path / "a.csv",
                expected_size=len(data),
                progress_cb=seen.append,
            )
        assert len(seen) >= 2  # more than one chunk actually reported
        assert seen == sorted(seen)  # cumulative, monotonic
        assert seen[-1] == len(data)

    def test_plan_totals_exclude_all_skips(self, tmp_path):
        # total_bytes must count ONLY what will transfer: not the active trip
        # file, not an already-downloaded copy, not an unsafe device name.
        (tmp_path / "have.csv").write_bytes(b"12345")
        files = {
            "open.csv": b"growing!",
            "have.csv": b"54321",  # same (name, size) -> already downloaded
            "../evil.csv": b"pwn",
            "new.csv": b"fresh data\n" * 3,
        }
        with _device(files, active="/sdcard/logs/open.csv") as (client, _):
            plan = client.plan(tmp_path)
        assert [log.name for log, _ in plan.to_download] == ["new.csv"]
        assert sorted(plan.skipped) == sorted(["../evil.csv", "have.csv", "open.csv"])
        assert plan.total_bytes == len(files["new.csv"])

    def test_download_new_progress_spans_the_whole_run(self, tmp_path):
        files = {"b.csv": b"n" * 3000, "a.csv": b"o" * 1000}
        events = []
        with _device(files) as (client, _):
            client.download_new(
                tmp_path, progress_cb=lambda d, t, n: events.append((d, t, n))
            )
        total = 4000
        # Determinate before the first byte, complete at the end, one shared
        # total throughout, monotonic byte counts, per-file attribution.
        assert events[0] == (0, total, "")
        assert events[-1] == (total, total, "a.csv")
        assert all(t == total for _, t, _ in events)
        dones = [d for d, _, _ in events]
        assert dones == sorted(dones)
        assert (3000, total, "b.csv") in events


class TestClassify:
    def test_every_remote_log_gets_a_status(self, tmp_path):
        # classify is the ONE home of the per-file decisions: the table view
        # and plan() must agree because both derive from it.
        (tmp_path / "have.csv").write_bytes(b"12345")
        files = {
            "open.csv": b"growing!",
            "have.csv": b"54321",  # same (name, size) -> already downloaded
            "../evil.csv": b"pwn",
            "new.csv": b"fresh data\n" * 3,
        }
        with _device(files, active="/sdcard/logs/open.csv") as (client, _):
            entries = client.classify(tmp_path)
        by_name = {e.log.name: e for e in entries}
        assert len(entries) == len(files)  # nothing dropped, device order kept
        assert [e.log.name for e in entries] == list(files)
        assert by_name["open.csv"].status == wican_logs.STATUS_ACTIVE
        assert by_name["have.csv"].status == wican_logs.STATUS_DOWNLOADED
        assert by_name["../evil.csv"].status == wican_logs.STATUS_UNSAFE_NAME
        assert by_name["new.csv"].status == wican_logs.STATUS_NEW
        # target is the chosen local path for NEW entries, absent otherwise.
        assert by_name["new.csv"].target == tmp_path / "new.csv"
        assert all(
            e.target is None for e in entries if e.status != wican_logs.STATUS_NEW
        )

    def test_plan_is_a_projection_of_classify(self, tmp_path):
        (tmp_path / "have.csv").write_bytes(b"12345")
        files = {
            "have.csv": b"54321",
            "b.csv": b"n" * 3000,
            "a.csv": b"o" * 1000,
        }
        with _device(files) as (client, _):
            entries = client.classify(tmp_path)
            plan = client.plan(tmp_path)
        new = [e for e in entries if e.status == wican_logs.STATUS_NEW]
        assert plan.to_download == [(e.log, e.target) for e in new]
        assert plan.skipped == ["have.csv"]
        assert plan.total_bytes == sum(e.log.size for e in new)


# --- opt-in delete after download (#112) ------------------------------------


def _deleted_paths(httpd):
    return [b["path"] for b in httpd.posts if b.get("op") == "delete"]


def _only_single_file_deletes(httpd):
    """Every delete ever sent names ONE file directly under logs/ — never a
    folder, never the logs root (a folder delete is recursive on the device)."""
    for body in httpd.posts:
        assert set(body) == {"op", "path"}
        assert body["op"] == "delete"
        head, _, name = body["path"].partition("/")
        assert head == "logs" and name and "/" not in name


def _sync_and_clean(client, dest, keep_newest=0):
    result = client.download_new(dest)
    return result, client.delete_verified(
        dest, result.verified, keep_newest=keep_newest
    )


class TestDeleteVerified:
    def test_this_runs_downloads_are_deleted_and_kept_locally(self, tmp_path):
        files = {"b.csv": b"newer\n" * 10, "a.csv": b"older\n" * 20}
        with _device(files) as (client, httpd):
            _, cleanup = _sync_and_clean(client, tmp_path)
            assert httpd.files == {}
            _only_single_file_deletes(httpd)
            # Oldest first, and no second fetch for this run's own downloads.
            assert _deleted_paths(httpd) == ["logs/a.csv", "logs/b.csv"]
            assert httpd.downloads == ["b.csv", "a.csv"]
        assert sorted(cleanup.deleted) == ["a.csv", "b.csv"]
        for name, data in files.items():
            assert (tmp_path / name).read_bytes() == data

    def test_keep_newest_n_stay_on_the_card(self, tmp_path):
        files = {"c.csv": b"3\n", "b.csv": b"22\n", "a.csv": b"111\n"}  # newest first
        with _device(files) as (client, httpd):
            _, cleanup = _sync_and_clean(client, tmp_path, keep_newest=2)
            assert list(httpd.files) == ["c.csv", "b.csv"]
        assert cleanup.deleted == ["a.csv"]

    def test_keep_more_than_on_card_deletes_nothing(self, tmp_path):
        with _device({"a.csv": b"x\n"}) as (client, httpd):
            _sync_and_clean(client, tmp_path, keep_newest=5)
            assert httpd.posts == []

    def test_active_trip_is_never_deleted(self, tmp_path):
        files = {"live.csv": b"growing\n", "old.csv": b"done\n"}
        with _device(files, active="/sdcard/logs/live.csv") as (client, httpd):
            # skip_active=False: even a local copy of the live file is no proof.
            result = client.download_new(tmp_path, skip_active=False)
            cleanup = client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert "live.csv" in httpd.files
            assert _deleted_paths(httpd) == ["logs/old.csv"]
        assert cleanup.deleted == ["old.csv"]

    def test_never_downloaded_trip_is_never_deleted(self, tmp_path):
        # No local copy and not downloaded this run: nothing proves it.
        with _device({"a.csv": b"only on the card\n"}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.posts == [] and "a.csv" in httpd.files
        assert cleanup.deleted == []

    def test_earlier_download_is_byte_verified_before_delete(self, tmp_path):
        data = b"time,rpm\n1,800\n"
        (tmp_path / "old.csv").write_bytes(data)  # an earlier run's copy
        with _device({"old.csv": data}) as (client, httpd):
            _, cleanup = _sync_and_clean(client, tmp_path)
            assert httpd.downloads == ["old.csv"]  # the proof re-fetch
            assert httpd.files == {}
        assert cleanup.deleted == ["old.csv"] and cleanup.rescued == []
        assert (tmp_path / "old.csv").read_bytes() == data
        assert sorted(p.name for p in tmp_path.iterdir()) == ["old.csv"]

    def test_dated_earlier_download_is_proven_by_its_head(self, tmp_path):
        # A dated name comes from a set clock: a same-size local copy whose
        # first 64 KB match is proof — a partial fetch, not a full re-download.
        data = b"time,rpm" + bytes([10]) + b"1,800" + bytes([10])
        names = ["20260920_17h20m22s.csv", "20261101_01h30m00s_1.csv"]
        for name in names:
            (tmp_path / name).write_bytes(data)
        with _device({name: data for name in names}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert sorted(httpd.downloads) == sorted(names)  # one head each
            assert httpd.files == {}
        assert sorted(cleanup.deleted) == sorted(names)

    def test_dated_trip_from_this_run_still_needs_an_unchanged_pair(self, tmp_path):
        # The fast path is for EARLIER runs only: a dated trip downloaded this
        # run whose card entry changed (same size, new mtime) is kept.
        name = "20260920_17h20m22s.csv"
        with _device({name: b"abc" + bytes([10])}) as (client, httpd):
            result = client.download_new(tmp_path)
            httpd.mtimes = {name: 1760000000}
            cleanup = client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert httpd.posts == []
        assert cleanup.deleted == []

    def test_dated_trip_matches_a_same_size_suffixed_copy(self, tmp_path):
        name = "20260920_17h20m22s.csv"
        data = b"the trip" + bytes([10])
        (tmp_path / name).write_bytes(b"a different size")
        (tmp_path / "20260920_17h20m22s-2.csv").write_bytes(data)
        with _device({name: data}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.downloads == [name]  # the head check only
        assert cleanup.deleted == [name]
        assert cleanup.rescued == []

    def test_dated_name_reused_by_another_trip_is_rescued(self, tmp_path):
        # The clock revisited a second after an earlier delete: a new trip
        # took the old name AND ended at the same size. Its first rows differ,
        # so the head check fails and the full check saves it before delete.
        name = "20261101_01h30m00s.csv"
        old_trip = b"t,rpm" + bytes([10]) + b"1,800" + bytes([10])
        new_trip = b"t,rpm" + bytes([10]) + b"9,999" + bytes([10])
        assert len(old_trip) == len(new_trip)
        (tmp_path / name).write_bytes(old_trip)
        with _device({name: new_trip}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.downloads == [name, name]  # head, then full proof
            assert httpd.files == {}
        rescued = tmp_path / "20261101_01h30m00s-2.csv"
        assert cleanup.rescued == [rescued]
        assert rescued.read_bytes() == new_trip
        assert (tmp_path / name).read_bytes() == old_trip

    def test_dated_head_check_uses_only_the_first_64_kb(self, tmp_path):
        name = "20260920_17h20m22s.csv"
        data = bytes(range(256)) * 1024  # 256 KB
        (tmp_path / name).write_bytes(data)
        with _device({name: data}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.downloads == [name]
        assert cleanup.deleted == [name]
        assert sorted(p.name for p in tmp_path.iterdir()) == [name]  # no temp

    def test_dated_head_fetch_failure_keeps_the_trip(self, tmp_path):
        name = "20260920_17h20m22s.csv"
        (tmp_path / name).write_bytes(b"12345")
        # Listed at 5 bytes, but the device serves fewer: a short head.
        with _device({name: b"123"}, advertised={name: 5}) as (client, httpd):
            httpd.fs_sizes = {name: 5}
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.posts == []
        assert cleanup.deleted == []

    def test_dated_trip_without_a_same_size_copy_is_kept(self, tmp_path):
        name = "20260920_17h20m22s.csv"
        (tmp_path / name).write_bytes(b"short")
        with _device({name: b"a longer trip"}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.posts == [] and httpd.downloads == []
        assert cleanup.deleted == []

    @pytest.mark.parametrize(
        "name",
        [
            "unknown_time_5000.csv",  # clockless: the name can be reused
            "19700101_00h00m05s.csv",  # a clock the firmware would call unset
            "20261301_00h00m00s.csv",  # not a real date
            "20260920_17h20m22s.CSV",  # not the firmware's spelling
            "20260920_17h20m22s-2.csv",  # a local collision suffix, not _N
        ],
    )
    def test_other_names_still_get_the_byte_check(self, tmp_path, name):
        data = b"x,y" + bytes([10])
        (tmp_path / name).write_bytes(data)
        with _device({name: data}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.downloads == [name]  # the proof re-fetch
        assert cleanup.deleted == [name]

    def test_clockless_name_and_size_reuse_is_rescued_not_lost(self, tmp_path):
        # The #112 trap: after a reboot a clockless device reuses
        # unknown_time_<ms>.csv for a NEW trip (fopen "w"), same size as the
        # old local copy. Name + size says "downloaded"; it never was.
        old_trip = b"time,rpm\n1,800\n"
        new_trip = b"time,rpm\n9,999\n"  # same length, different trip
        assert len(old_trip) == len(new_trip)
        name = "unknown_time_5000.csv"
        (tmp_path / name).write_bytes(old_trip)
        with _device({name: new_trip}) as (client, httpd):
            result, cleanup = _sync_and_clean(client, tmp_path)
            assert result.downloaded == []  # classify saw "already downloaded"
            assert httpd.files == {}  # deleted only AFTER it was saved
        assert (tmp_path / name).read_bytes() == old_trip
        rescued = tmp_path / "unknown_time_5000-2.csv"
        assert cleanup.rescued == [rescued]
        assert rescued.read_bytes() == new_trip
        assert cleanup.deleted == [name]

    def test_backlog_reverify_failure_keeps_the_trip(self, tmp_path):
        (tmp_path / "old.csv").write_bytes(b"12345\n")
        # Listed at 6 bytes, but the device serves only 5: the proof fetch fails.
        with _device({"old.csv": b"1234\n"}, advertised={"old.csv": 6}) as (
            client,
            httpd,
        ):
            httpd.fs_sizes = {"old.csv": 6}
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.downloads == ["old.csv"]
            assert httpd.posts == []
        assert cleanup.deleted == []
        assert sorted(p.name for p in tmp_path.iterdir()) == ["old.csv"]  # no temp

    def test_folder_named_like_a_trip_is_never_deleted(self, tmp_path):
        data = b"x\n"
        (tmp_path / "trap.csv").write_bytes(data)
        with _device({"trap.csv": data}) as (client, httpd):
            httpd.dirs = {"trap.csv"}
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.posts == [] and httpd.downloads == []
        assert cleanup.deleted == []

    def test_size_disagreement_between_listings_is_kept(self, tmp_path):
        with _device({"a.csv": b"abc\n"}) as (client, httpd):
            result = client.download_new(tmp_path)
            httpd.fs_sizes = {"a.csv": 999}  # the file manager sees another size
            cleanup = client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert httpd.posts == []
        assert cleanup.deleted == []

    def test_trip_changed_since_download_is_kept(self, tmp_path):
        with _device({"a.csv": b"abc\n"}) as (client, httpd):
            result = client.download_new(tmp_path)
            httpd.mtimes = {"a.csv": 1760000000}  # rewritten after the download
            cleanup = client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert httpd.posts == []
        assert cleanup.deleted == []

    def test_local_copy_gone_or_truncated_is_kept(self, tmp_path):
        files = {"b.csv": b"bbbb\n", "a.csv": b"aaaa\n"}
        with _device(files) as (client, httpd):
            result = client.download_new(tmp_path)
            (tmp_path / "a.csv").unlink()
            (tmp_path / "b.csv").write_bytes(b"bb")
            cleanup = client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert httpd.posts == []
        assert cleanup.deleted == []

    def test_unsafe_name_is_never_sent(self, tmp_path):
        with _device({"../evil.csv": b"x\n", "ok.csv": b"y\n"}) as (client, httpd):
            _, cleanup = _sync_and_clean(client, tmp_path)
            _only_single_file_deletes(httpd)
            assert _deleted_paths(httpd) == ["logs/ok.csv"]
        assert cleanup.deleted == ["ok.csv"]

    def test_delete_failure_is_recorded_and_the_pass_continues(self, tmp_path):
        files = {"b.csv": b"b\n", "a.csv": b"a\n"}
        with _device(files) as (client, httpd):
            httpd.delete_errors = {"a.csv": (500, "delete failed")}
            _, cleanup = _sync_and_clean(client, tmp_path)
            assert list(httpd.files) == ["a.csv"]
        assert cleanup.deleted == ["b.csv"]
        assert [n for n, _ in cleanup.failed] == ["a.csv"]
        assert "HTTP 500" in cleanup.failed[0][1]

    def test_abort_stops_the_pass(self, tmp_path):
        files = {"b.csv": b"b\n", "a.csv": b"a\n"}
        with _device(files) as (client, httpd):
            result = client.download_new(tmp_path)
            cleanup = client.delete_verified(
                tmp_path, result.verified, keep_newest=0, abort_cb=lambda: True
            )
            assert httpd.posts == []
        assert cleanup.cancelled is True

    def test_cancelled_download_is_flagged(self, tmp_path):
        with _device({"a.csv": b"a\n"}) as (client, _):
            result = client.download_new(tmp_path, abort_cb=lambda: True)
        assert result.cancelled is True and result.verified == []

    def test_unreadable_file_listing_deletes_nothing(self, tmp_path):
        class _NoFilesHandler(_Handler):
            def _files_list(self, srv, qs):
                self._json(404, {"error": "not found"})

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NoFilesHandler)
        httpd.files = {"a.csv": b"a\n"}
        httpd.active = None
        httpd.advertised = {}
        httpd.status_error = False
        httpd.posts = []
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            host, port = httpd.server_address
            client = WiCANLogClient(host, http_port=port, timeout_s=5.0)
            result = client.download_new(tmp_path)
            with pytest.raises(WiCANHttpError):
                client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert httpd.posts == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=2)

    def test_names_the_download_cannot_address_are_never_deleted(self, tmp_path):
        # The download query-encodes a space as "+" and the firmware never
        # decodes it, while the delete path goes out verbatim: for such a name
        # the file downloaded and the file deleted could differ.
        data = b"x\n"
        (tmp_path / "a b.csv").write_bytes(data)
        with _device({"a b.csv": data}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            with pytest.raises(WiCANLogsError, match="unsupported characters"):
                client.delete_log("a b.csv")
            assert httpd.posts == [] and httpd.downloads == []
        assert cleanup.deleted == []

    def test_entry_changed_before_the_post_is_kept(self, tmp_path):
        # Proving earlier trips can take minutes; if the card changes in the
        # meantime (a rebooted clockless device reusing the name), the entry
        # re-read right before the POST no longer matches and nothing is sent.
        files = {"a.csv": b"abc\n"}
        with _device(files) as (client, httpd):
            result = client.download_new(tmp_path)

            def rewrite_after_first_listing(srv):
                if srv.fs_list_calls > 1:
                    srv.fs_mtimes = {"a.csv": 1760000000}

            httpd.fs_list_hook = rewrite_after_first_listing
            cleanup = client.delete_verified(tmp_path, result.verified, keep_newest=0)
            assert httpd.posts == [] and "a.csv" in httpd.files
        assert cleanup.deleted == []

    def test_keep_newest_uses_timestamps_not_listing_order(self, tmp_path):
        # Past its sort buffer the firmware lists entries unsorted: the kept
        # trips must be the newest by timestamp, whatever the order.
        files = {"old.csv": b"1\n", "new.csv": b"22\n", "mid.csv": b"333\n"}
        with _device(files) as (client, httpd):
            httpd.mtimes = {"old.csv": 100, "mid.csv": 200, "new.csv": 300}
            _, cleanup = _sync_and_clean(client, tmp_path, keep_newest=2)
            assert sorted(httpd.files) == ["mid.csv", "new.csv"]
        assert cleanup.deleted == ["old.csv"]

    def test_delete_reply_without_ok_is_a_failure(self, tmp_path):
        with _device({"a.csv": b"a\n"}) as (client, httpd):
            httpd.delete_reply = {"ok": False}
            _, cleanup = _sync_and_clean(client, tmp_path)
        assert cleanup.deleted == []
        assert [n for n, _ in cleanup.failed] == ["a.csv"]

    def test_rescue_compares_every_byte_of_a_large_trip(self, tmp_path):
        # Differs only past the first 64 KiB compare chunk.
        old_trip = bytearray(b"r" * 300_000)
        new_trip = bytearray(old_trip)
        new_trip[200_000] = ord("X")
        name = "unknown_time_7000.csv"
        (tmp_path / name).write_bytes(bytes(old_trip))
        with _device({name: bytes(new_trip)}) as (client, httpd):
            cleanup = client.delete_verified(tmp_path, (), keep_newest=0)
            assert httpd.files == {}
        rescued = tmp_path / "unknown_time_7000-2.csv"
        assert cleanup.rescued == [rescued]
        assert rescued.read_bytes() == bytes(new_trip)
        assert (tmp_path / name).read_bytes() == bytes(old_trip)

    def test_negative_keep_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            WiCANLogClient("127.0.0.1").delete_verified(tmp_path, (), keep_newest=-1)


class TestPostJson:
    def test_http_error_carries_the_device_reason(self):
        with _device({"a.csv": b"a\n"}, active="/sdcard/logs/a.csv") as (client, _):
            with pytest.raises(WiCANHttpError, match="409.*being written"):
                client.delete_log("a.csv")

    def test_delete_log_refuses_unsafe_names_without_sending(self):
        with _device({}) as (client, httpd):
            for bad in ("", "..", "sub/x.csv", "../x.csv"):
                with pytest.raises(WiCANHttpError):
                    client.delete_log(bad)
            assert httpd.posts == []


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

    def test_is_wican_adapter_tracks_adapter_selection(self, _settings):
        # The single predicate for WiCAN-only affordances — callers never
        # compare the raw adapter string.
        settings, _ = _settings
        assert settings.is_wican_adapter() is False  # j2534 is the default
        settings.set_ecu_adapter("wican")
        assert settings.is_wican_adapter() is True
        settings.set_ecu_adapter("j2534")
        assert settings.is_wican_adapter() is False

    def test_auto_check_defaults_on_and_round_trips(self, _settings):
        settings, _ = _settings
        assert settings.get_wican_auto_download_logs() is True
        settings.set_wican_auto_download_logs(False)
        assert settings.get_wican_auto_download_logs() is False

    def test_delete_after_download_defaults_off_and_round_trips(self, _settings):
        settings, _ = _settings
        assert settings.get_wican_delete_logs_after_download() is False
        assert settings.get_wican_keep_newest_logs() == 5
        settings.set_wican_delete_logs_after_download(True)
        settings.set_wican_keep_newest_logs(2)
        assert settings.get_wican_delete_logs_after_download() is True
        assert settings.get_wican_keep_newest_logs() == 2
        settings.set_wican_keep_newest_logs(-3)
        assert settings.get_wican_keep_newest_logs() == 0
