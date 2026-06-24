"""Host SD-upload client (Option B Phase 2, host half).

Drives :class:`WiCANSdUploader` against a tiny in-process HTTP server that
emulates the firmware's ``/upload/sd/<name>`` endpoint — a real multipart
round-trip, no hardware. Covers the happy path plus every way the upload must
refuse to be trusted: CRC mismatch, short write, HTTP error, non-JSON reply,
unreachable host, and a bad caller expectation caught before any bytes leave.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.ecu.checksum import crc32
from src.ecu.wican_sd_package import FlashPackage
from src.ecu.wican_sd_upload import (
    WiCANSdUploader,
    WiCANUploadError,
    manifest_json,
)


def _extract_file_bytes(body: bytes, content_type: str) -> bytes:
    """Pull the single uploaded file's raw bytes out of a multipart body."""
    token = content_type.split("boundary=", 1)[1].strip()
    boundary = b"--" + token.encode("ascii")
    parts = body.split(boundary)
    # parts[1] is the file part: b"\r\n<headers>\r\n\r\n<data>\r\n"
    part = parts[1]
    header_end = part.index(b"\r\n\r\n") + 4
    data = part[header_end:]
    if data.endswith(b"\r\n"):
        data = data[:-2]
    return data


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        name = self.path[len("/upload/sd/") :]
        data = _extract_file_bytes(body, self.headers.get("Content-Type", ""))
        # Record what the device actually received for assertions.
        self.server.received.append((name, data))
        status, payload, ctype = self.server.responder(name, data)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence test output
        pass


def _ok_responder(name, data):
    body = json.dumps({"bytes_written": len(data), "crc32": crc32(data)}).encode()
    return 200, body, "application/json"


@contextmanager
def _server(responder=_ok_responder):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.responder = responder
    httpd.received = []
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        host, port = httpd.server_address
        yield WiCANSdUploader(host, http_port=port, timeout_s=5.0), httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)


def _fake_package(image: bytes, name="ID_20260623-1745.bin") -> FlashPackage:
    return FlashPackage(
        image=image,
        manifest={
            "image_len": len(image),
            "image_crc32": crc32(image),
            "staged_filename": name,
        },
    )


class TestUploadBytes:
    def test_roundtrip_verified(self):
        data = bytes(range(256)) * 8  # 2 KB, binary (exercises CRLF in payload)
        with _server() as (up, httpd):
            res = up.upload_bytes("img.bin", data)
        assert res.bytes_written == len(data)
        assert res.crc32 == crc32(data)
        # The device received exactly the bytes we sent, under the right name.
        assert httpd.received == [("img.bin", data)]

    def test_crc_as_hex_string_reply(self):
        def responder(name, data):
            body = json.dumps(
                {"bytes_written": len(data), "crc32": f"0x{crc32(data):08X}"}
            ).encode()
            return 200, body, "application/json"

        with _server(responder) as (up, _):
            res = up.upload_bytes("img.bin", b"hello world")
        assert res.crc32 == crc32(b"hello world")

    def test_crc_mismatch_rejected(self):
        def responder(name, data):
            body = json.dumps(
                {"bytes_written": len(data), "crc32": 0xDEADBEEF}
            ).encode()
            return 200, body, "application/json"

        with _server(responder) as (up, _):
            with pytest.raises(WiCANUploadError, match="CRC mismatch"):
                up.upload_bytes("img.bin", b"payload")

    def test_short_write_rejected(self):
        def responder(name, data):
            body = json.dumps(
                {"bytes_written": len(data) - 1, "crc32": crc32(data)}
            ).encode()
            return 200, body, "application/json"

        with _server(responder) as (up, _):
            with pytest.raises(WiCANUploadError, match="stored"):
                up.upload_bytes("img.bin", b"payload")

    def test_http_error_rejected(self):
        def responder(name, data):
            return 500, b"disk full", "text/plain"

        with _server(responder) as (up, _):
            with pytest.raises(WiCANUploadError, match="HTTP 500"):
                up.upload_bytes("img.bin", b"payload")

    def test_non_json_reply_rejected(self):
        def responder(name, data):
            return 200, b"OK", "text/plain"

        with _server(responder) as (up, _):
            with pytest.raises(WiCANUploadError, match="not JSON"):
                up.upload_bytes("img.bin", b"payload")

    def test_missing_crc_rejected(self):
        def responder(name, data):
            return (
                200,
                json.dumps({"bytes_written": len(data)}).encode(),
                "application/json",
            )

        with _server(responder) as (up, _):
            with pytest.raises(WiCANUploadError, match="crc32"):
                up.upload_bytes("img.bin", b"payload")

    def test_unreachable_host(self):
        # Nothing listening on this port.
        up = WiCANSdUploader("127.0.0.1", http_port=1, timeout_s=2.0)
        with pytest.raises(WiCANUploadError, match="cannot reach"):
            up.upload_bytes("img.bin", b"payload")

    def test_expected_crc_disagreeing_with_data_fails_fast(self):
        up = WiCANSdUploader("127.0.0.1", http_port=9, timeout_s=1.0)
        with pytest.raises(WiCANUploadError, match="expected_crc32"):
            up.upload_bytes("img.bin", b"payload", expected_crc32=0x1234)

    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "../x", ".", ""])
    def test_bad_names_rejected_before_network(self, bad):
        up = WiCANSdUploader("127.0.0.1", http_port=9, timeout_s=1.0)
        with pytest.raises(WiCANUploadError):
            up.upload_bytes(bad, b"x")


class TestUploadPackage:
    def test_upload_package_roundtrip(self):
        image = bytes(range(256)) * 16  # 4 KB
        pkg = _fake_package(image, name="SW-LFDJEA000_20260623-1745.bin")
        with _server() as (up, httpd):
            res = up.upload_package(pkg)
        assert res.name == "SW-LFDJEA000_20260623-1745.bin"
        assert res.bytes_written == len(image)
        assert httpd.received[0][1] == image

    def test_upload_package_detects_internal_inconsistency(self):
        image = b"\x01\x02\x03\x04"
        pkg = FlashPackage(
            image=image,
            manifest={
                "image_len": 999,
                "image_crc32": crc32(image),
                "staged_filename": "x.bin",
            },
        )
        up = WiCANSdUploader("127.0.0.1", http_port=9, timeout_s=1.0)
        with pytest.raises(WiCANUploadError, match="image_len"):
            up.upload_package(pkg)

    def test_upload_manifest_sidecar(self):
        pkg = _fake_package(b"abcd", name="ID_20260623-1745.bin")
        with _server() as (up, httpd):
            res = up.upload_manifest(pkg)
        assert res.name == "ID_20260623-1745.json"
        assert httpd.received[0][0] == "ID_20260623-1745.json"
        assert httpd.received[0][1] == manifest_json(pkg)


def test_manifest_json_deterministic_and_sorted():
    pkg = _fake_package(b"abcd")
    a = manifest_json(pkg)
    b = manifest_json(pkg)
    assert a == b
    decoded = json.loads(a)
    assert decoded["image_len"] == 4
    # sort_keys -> keys appear in sorted order in the raw text
    assert a.decode().index('"image_crc32"') < a.decode().index('"image_len"')
