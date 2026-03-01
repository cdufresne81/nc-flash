"""
Tests for the Command Server HTTP bridge and API request handling.
"""

import json
import threading
import time
import urllib.request
import urllib.error

import pytest

# ---------------------------------------------------------------------------
# CommandServer unit tests (no Qt dependency — use a simple callback)
# ---------------------------------------------------------------------------


class TestCommandServer:
    """Tests for the HTTP server mechanics (start, stop, routing, errors)."""

    TEST_PORT = 18766  # Different from default 8766 to avoid conflicts with running app

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Import here so tests skip cleanly if PySide6 is not available."""
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QObject

        # Ensure a QApplication exists (needed for QTimer)
        self.app = QApplication.instance() or QApplication([])
        self.parent = QObject()

        from src.api.command_server import CommandServer

        self.CommandServer = CommandServer

    def _make_server(self, callback=None):
        if callback is None:
            callback = lambda req: {"success": True, "echo": req}
        server = self.CommandServer(callback, self.parent, port=self.TEST_PORT)
        return server

    def _post(self, port, path, payload):
        """Helper: POST JSON to the server and return parsed response."""
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def _process_events(self, duration=0.3):
        """Process Qt events so the QTimer can fire."""
        import time
        from PySide6.QtWidgets import QApplication

        end = time.monotonic() + duration
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    def test_start_and_stop(self):
        server = self._make_server()
        assert server.start() is True
        assert server.is_running is True
        assert "127.0.0.1" in server.url
        server.stop()
        assert server.is_running is False

    def test_post_returns_callback_result(self):
        def callback(req):
            return {"success": True, "table": req.get("table_name")}

        server = self._make_server(callback)
        assert server.start()
        try:
            # Run the POST in a thread since we need to process Qt events
            result_holder = [None]
            error_holder = [None]

            def do_post():
                try:
                    result_holder[0] = self._post(
                        self.TEST_PORT, "/api/read-table", {"table_name": "Fuel VE"}
                    )
                except Exception as e:
                    error_holder[0] = e

            t = threading.Thread(target=do_post)
            t.start()
            self._process_events(1.0)
            t.join(timeout=5)

            assert error_holder[0] is None, f"POST failed: {error_holder[0]}"
            assert result_holder[0] is not None
            assert result_holder[0]["success"] is True
            assert result_holder[0]["table"] == "Fuel VE"
        finally:
            server.stop()

    def test_wrong_method_returns_405(self):
        server = self._make_server()
        assert server.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.TEST_PORT}/api/read-table",
                method="GET",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 405
        finally:
            server.stop()

    def test_wrong_path_returns_404(self):
        server = self._make_server()
        assert server.start()
        try:
            result_holder = [None]
            error_holder = [None]

            def do_post():
                try:
                    body = json.dumps({"foo": "bar"}).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{self.TEST_PORT}/api/nonexistent",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=5)
                except urllib.error.HTTPError as e:
                    error_holder[0] = e

            t = threading.Thread(target=do_post)
            t.start()
            self._process_events(0.5)
            t.join(timeout=5)

            assert error_holder[0] is not None
            assert error_holder[0].code == 404
        finally:
            server.stop()

    def test_invalid_json_returns_400(self):
        server = self._make_server()
        assert server.start()
        try:
            result_holder = [None]
            error_holder = [None]

            def do_post():
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{self.TEST_PORT}/api/read-table",
                        data=b"not json at all",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=5)
                except urllib.error.HTTPError as e:
                    error_holder[0] = e

            t = threading.Thread(target=do_post)
            t.start()
            self._process_events(0.5)
            t.join(timeout=5)

            assert error_holder[0] is not None
            assert error_holder[0].code == 400
        finally:
            server.stop()

    def test_callback_exception_returns_error(self):
        def bad_callback(req):
            raise ValueError("Something went wrong")

        server = self._make_server(bad_callback)
        assert server.start()
        try:
            result_holder = [None]
            error_holder = [None]

            def do_post():
                try:
                    result_holder[0] = self._post(
                        self.TEST_PORT, "/api/modified", {"rom_path": "/some/path"}
                    )
                except Exception as e:
                    error_holder[0] = e

            t = threading.Thread(target=do_post)
            t.start()
            self._process_events(1.0)
            t.join(timeout=5)

            assert error_holder[0] is None
            assert result_holder[0] is not None
            assert result_holder[0]["success"] is False
            assert "Something went wrong" in result_holder[0]["error"]
        finally:
            server.stop()

    def test_stop_drains_pending_requests(self):
        """Stopping the server should resolve any pending requests."""
        server = self._make_server()
        assert server.start()
        server.stop()
        assert server.is_running is False


# ---------------------------------------------------------------------------
# RomContext live bridge method tests (mock HTTP)
# ---------------------------------------------------------------------------


class TestRomContextLiveBridge:
    """Tests for RomContext._post_to_app, list_modified_tables, read_live_table, write_table."""

    @pytest.fixture
    def ctx(self, definitions_dir):
        from src.mcp.rom_context import RomContext

        return RomContext(metadata_dir=str(definitions_dir))

    def test_post_to_app_no_workspace(self, ctx, tmp_path, monkeypatch):
        """When workspace.json has no command_api_url, returns error."""
        monkeypatch.setattr("src.mcp.rom_context.get_app_root", lambda: tmp_path)
        result = ctx._post_to_app({"endpoint": "/api/modified", "rom_path": "x"})
        assert result["success"] is False
        assert "not available" in result["error"]

    def test_post_to_app_connection_refused(self, ctx, tmp_path, monkeypatch):
        """When the app is not running, returns connection error."""
        monkeypatch.setattr("src.mcp.rom_context.get_app_root", lambda: tmp_path)
        # Write workspace.json with a URL that won't connect
        workspace = {"command_api_url": "http://127.0.0.1:19999"}
        (tmp_path / "workspace.json").write_text(json.dumps(workspace))

        result = ctx._post_to_app({"endpoint": "/api/modified", "rom_path": "x"})
        assert result["success"] is False
        assert "Cannot connect" in result["error"]

    def test_list_modified_tables_delegates(self, ctx, monkeypatch):
        """list_modified_tables builds the correct payload."""
        captured = []
        monkeypatch.setattr(
            ctx, "_post_to_app", lambda p: (captured.append(p), {"success": True})[1]
        )
        ctx.list_modified_tables("/path/to/rom.bin")
        assert len(captured) == 1
        assert captured[0]["endpoint"] == "/api/modified"
        assert captured[0]["rom_path"] == "/path/to/rom.bin"

    def test_read_live_table_delegates(self, ctx, monkeypatch):
        """read_live_table builds the correct payload."""
        captured = []
        monkeypatch.setattr(
            ctx, "_post_to_app", lambda p: (captured.append(p), {"success": True})[1]
        )
        ctx.read_live_table("/path/to/rom.bin", "Fuel VE")
        assert len(captured) == 1
        assert captured[0]["endpoint"] == "/api/read-table"
        assert captured[0]["table_name"] == "Fuel VE"

    def test_write_table_delegates(self, ctx, monkeypatch):
        """write_table builds the correct payload."""
        captured = []
        monkeypatch.setattr(
            ctx, "_post_to_app", lambda p: (captured.append(p), {"success": True})[1]
        )
        cells = [{"row": 0, "col": 0, "value": 42.5}]
        ctx.write_table("/path/to/rom.bin", "Fuel VE", cells)
        assert len(captured) == 1
        assert captured[0]["endpoint"] == "/api/edit-table"
        assert captured[0]["cells"] == cells
