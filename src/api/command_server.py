"""
Command Server — HTTP bridge from MCP server to Qt main thread.

Runs an HTTP server on a daemon thread (127.0.0.1:8766) that accepts
JSON POST requests and dispatches them to a callback on the Qt main
thread via a queue + QTimer polling pattern.

Endpoints:
    POST /api/read-table   — read live in-memory table values
    POST /api/modified     — list tables with unsaved modifications
    POST /api/edit-table   — write values through the app's editing pipeline
"""

import json
import logging
import queue
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Dict, Any

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

VALID_ENDPOINTS = {"/api/read-table", "/api/modified", "/api/edit-table"}


class _RequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that queues requests for Qt main-thread processing."""

    def do_POST(self):
        if self.path not in VALID_ENDPOINTS:
            self._send_json(404, {"success": False, "error": f"Not found: {self.path}"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, {"success": False, "error": "Empty request body"})
            return

        try:
            body = self.rfile.read(content_length)
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json(400, {"success": False, "error": f"Invalid JSON: {e}"})
            return

        payload["endpoint"] = self.path

        # Place request on queue and wait for Qt main thread to process it
        result_container: list = []
        event = threading.Event()
        self.server._request_queue.put((payload, event, result_container))

        if not event.wait(timeout=10):
            self._send_json(504, {"success": False, "error": "Request timed out"})
            return

        result = result_container[0] if result_container else {"success": False, "error": "No response"}
        self._send_json(200, result)

    def do_GET(self):
        self._send_json(405, {"success": False, "error": "Use POST"})

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default stderr logging — use our logger instead."""
        logger.debug("CommandServer: %s", format % args)


class CommandServer:
    """HTTP command API that bridges MCP server requests to the Qt main thread.

    Args:
        callback: Function called on the Qt main thread with a request dict,
                  returns a response dict. This is MainWindow._handle_api_request.
        parent: QObject parent for the QTimer (usually MainWindow).
    """

    PORT = 8766

    def __init__(self, callback: Callable[[dict], dict], parent: QObject, port: int | None = None):
        self._callback = callback
        self._parent = parent
        self._port = port or self.PORT
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._timer: QTimer | None = None
        self._request_queue: queue.Queue = queue.Queue()

    def start(self) -> bool:
        """Start the HTTP server. Returns False if the port is in use."""
        if self._server is not None:
            return True

        try:
            self._server = HTTPServer(("127.0.0.1", self._port), _RequestHandler)
            self._server._request_queue = self._request_queue
        except OSError as e:
            logger.error(f"CommandServer failed to bind port {self._port}: {e}")
            self._server = None
            return False

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        # QTimer polls the queue from the Qt main thread
        self._timer = QTimer(self._parent)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll_queue)
        self._timer.start()

        logger.info(f"CommandServer started on http://127.0.0.1:{self._port}")
        return True

    def stop(self):
        """Stop the HTTP server and queue timer."""
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

        if self._server is not None:
            self._server.shutdown()
            self._server = None

        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

        # Drain any pending requests
        while not self._request_queue.empty():
            try:
                payload, event, result_container = self._request_queue.get_nowait()
                result_container.append({"success": False, "error": "Server shutting down"})
                event.set()
            except queue.Empty:
                break

        logger.info("CommandServer stopped")

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def _poll_queue(self):
        """Process pending requests on the Qt main thread."""
        while not self._request_queue.empty():
            try:
                payload, event, result_container = self._request_queue.get_nowait()
            except queue.Empty:
                break

            try:
                result = self._callback(payload)
            except Exception as e:
                logger.exception(f"CommandServer callback error: {e}")
                result = {"success": False, "error": str(e)}

            result_container.append(result)
            event.set()
