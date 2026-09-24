"""End-to-end tests for the MCP server's network transports.

These spawn the real MCP server subprocess and talk to it with the official
MCP SDK client, so they catch transport/URL/config drift that unit tests on
RomContext can't:

- The app launch path (McpMixin._start_mcp_server) starts a Streamable HTTP
  server that a client can initialize, list tools on, and call a tool through.
- .mcp.json points Claude Code at the same URL the app serves.
- The deprecated SSE transport still works from the CLI (kept one release).
- DNS-rebinding protection rejects a foreign Host header.

The workspace dir is redirected to tmp_path so get_workspace reads a file we
wrote, never the developer's real workspace.json.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from src.ui.mcp_mixin import McpMixin

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "get_workspace",
    "get_rom_info",
    "list_tables",
    "write_table",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(f"MCP server exited early (rc={proc.returncode}):\n{err}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"MCP server did not listen on port {port} within {timeout}s")


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """Redirect get_user_data_dir() (inherited by the subprocess) to tmp_path."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    data_dir = tmp_path / "NCFlash"
    data_dir.mkdir()
    marker = {"open_roms": [], "active_rom": "C:/transport-test-marker.bin"}
    (data_dir / "workspace.json").write_text(json.dumps(marker), encoding="utf-8")
    return marker


async def _exercise(session: ClientSession) -> tuple[set, dict]:
    await session.initialize()
    tools = {t.name for t in (await session.list_tools()).tools}
    result = await session.call_tool("get_workspace", {})
    assert not result.isError, result
    return tools, json.loads(result.content[0].text)


async def _via_streamable_http(url: str):
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            return await _exercise(session)


async def _via_sse(url: str):
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            return await _exercise(session)


class _FakeMainWindow(McpMixin):
    """Just enough MainWindow for _start/_stop_mcp_server to run for real."""

    def __init__(self, port: int, log_path: Path):
        self.MCP_HTTP_PORT = port
        self.MCP_LOG_PATH = log_path
        self.MCP_URL = f"http://127.0.0.1:{port}/mcp"
        self._mcp_process = None
        self.ui_states = []

        class _Settings:
            def get_metadata_directory(self):
                return str(REPO_ROOT / "metadata")

        self.settings = _Settings()

    # The command API bridge and Qt UI are out of scope for a transport test.
    def _start_command_server(self):
        pass

    def _stop_command_server(self):
        pass

    def _update_mcp_ui(self, running: bool):
        self.ui_states.append(running)

    def _write_workspace_state(self):
        pass


def test_mcp_json_matches_app_url():
    """Claude Code's .mcp.json must point at the URL the app actually serves."""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    entry = config["mcpServers"]["nc-flash"]
    assert entry["type"] == "http"
    assert entry["url"] == McpMixin.MCP_URL
    assert McpMixin.MCP_URL.endswith("/mcp")
    assert McpMixin.MCP_TRANSPORT == "streamable-http"


def test_app_launch_serves_streamable_http(workspace_dir, tmp_path):
    """The app's own start path yields a working Streamable HTTP server, and
    a fresh client still works after a server restart (stateless mode)."""
    win = _FakeMainWindow(_free_port(), tmp_path / "mcp-server.log")
    try:
        win._start_mcp_server()
        assert win._is_mcp_running(), "MCP subprocess failed to start"
        _wait_for_port(win.MCP_HTTP_PORT, win._mcp_process)

        tools, workspace = anyio.run(_via_streamable_http, win.MCP_URL)
        assert EXPECTED_TOOLS <= tools
        assert workspace == workspace_dir

        # Restart: a new client must connect without stale-session errors.
        win._stop_mcp_server()
        assert not win._is_mcp_running()
        win._start_mcp_server()
        _wait_for_port(win.MCP_HTTP_PORT, win._mcp_process)
        tools, _ = anyio.run(_via_streamable_http, win.MCP_URL)
        assert EXPECTED_TOOLS <= tools
    finally:
        win._stop_mcp_server()
    assert win.ui_states == [True, False, True, False]


async def _many_calls(url: str, n: int):
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for _ in range(n):
                with anyio.fail_after(10):
                    result = await session.call_tool("get_workspace", {})
                assert not result.isError, result


def test_app_launched_server_survives_many_requests(workspace_dir, tmp_path):
    """Regression: the server logs to stderr on every request. If the app's
    launch path gives it a pipe nobody drains, the pipe fills after a few
    dozen calls and the server's event loop blocks in logging — wedging it
    for every client. Found live on Sep 24, 2026."""
    win = _FakeMainWindow(_free_port(), tmp_path / "mcp-server.log")
    try:
        win._start_mcp_server()
        _wait_for_port(win.MCP_HTTP_PORT, win._mcp_process)
        anyio.run(_many_calls, win.MCP_URL, 300)
    finally:
        win._stop_mcp_server()
    # The log went to the file instead of an undrained pipe.
    assert "CallToolRequest" in win.MCP_LOG_PATH.read_text(errors="replace")


def _spawn_cli(transport: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "src.mcp.server", "--transport", transport]
        + ["--port", str(port)],
        cwd=str(REPO_ROOT),
        stderr=subprocess.PIPE,
    )


def _stop(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def test_deprecated_sse_transport_still_works(workspace_dir):
    port = _free_port()
    proc = _spawn_cli("sse", port)
    try:
        _wait_for_port(port, proc)
        tools, workspace = anyio.run(_via_sse, f"http://127.0.0.1:{port}/sse")
        assert EXPECTED_TOOLS <= tools
        assert workspace == workspace_dir
    finally:
        _stop(proc)


def test_streamable_http_rejects_foreign_host_header():
    """DNS-rebinding guard: a browser page on another origin must not be able
    to drive write_table through the localhost server."""
    port = _free_port()
    proc = _spawn_cli("streamable-http", port)
    try:
        _wait_for_port(port, proc)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        url = f"http://127.0.0.1:{port}/mcp"

        ok = httpx.post(url, json=body, headers=headers, timeout=10)
        assert ok.status_code == 200, ok.text

        evil = httpx.post(
            url, json=body, headers={**headers, "Host": "evil.example"}, timeout=10
        )
        assert evil.status_code == 421, evil.text
    finally:
        _stop(proc)
