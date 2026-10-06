"""
Tests for drag-and-drop ROM file support on the main window.
"""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from PySide6.QtCore import QUrl, QMimeData, Qt
from PySide6.QtWidgets import QApplication

from main import MainWindow, _DropOverlayWidget

# Ensure a QApplication exists (required for any Qt widget tests)
_app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# _DropOverlayWidget
# ---------------------------------------------------------------------------


class TestDropOverlayWidget:
    """Test the translucent overlay widget used during drag-over."""

    def test_overlay_creates_and_paints(self):
        """Overlay can be created and paints without error."""
        overlay = _DropOverlayWidget()
        overlay.resize(400, 300)
        overlay.show()
        overlay.repaint()  # force paintEvent
        overlay.hide()
        overlay.deleteLater()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mime_data(file_paths):
    """Create a QMimeData with file URLs."""
    mime = QMimeData()
    urls = [QUrl.fromLocalFile(str(p)) for p in file_paths]
    mime.setUrls(urls)
    return mime


class _FakeSelf:
    """Minimal stand-in for MainWindow so we can test _get_drop_file_paths."""

    _DROP_EXTENSIONS = MainWindow._DROP_EXTENSIONS


class TestGetDropFilePaths:
    """Test the helper that filters drop MIME data for valid ROM files."""

    def test_accepts_bin_files(self):
        mime = _make_mime_data(["/tmp/test.bin"])
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert paths == ["/tmp/test.bin"]

    def test_accepts_rom_files(self):
        mime = _make_mime_data(["/tmp/test.rom"])
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert paths == ["/tmp/test.rom"]

    def test_case_insensitive_extension(self):
        mime = _make_mime_data(["/tmp/test.BIN", "/tmp/test.Rom"])
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert len(paths) == 2

    def test_rejects_invalid_extensions(self):
        mime = _make_mime_data(["/tmp/test.txt", "/tmp/test.exe", "/tmp/readme.md"])
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert paths == []

    def test_mixed_valid_and_invalid(self):
        mime = _make_mime_data(["/tmp/a.bin", "/tmp/b.txt", "/tmp/c.rom"])
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert paths == ["/tmp/a.bin", "/tmp/c.rom"]

    def test_no_urls(self):
        mime = QMimeData()
        mime.setText("just text")
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert paths == []

    def test_empty_urls(self):
        mime = QMimeData()
        mime.setUrls([])
        paths = MainWindow._get_drop_file_paths(_FakeSelf(), mime)
        assert paths == []


class TestDropExtensions:
    """Verify the extension set matches the File > Open dialog."""

    def test_bin_in_extensions(self):
        assert ".bin" in MainWindow._DROP_EXTENSIONS

    def test_rom_in_extensions(self):
        assert ".rom" in MainWindow._DROP_EXTENSIONS

    def test_no_unexpected_extensions(self):
        # .bin and .rom open; a dropped Romdrop .patch file gets the
        # "install it with Romdrop" answer instead of "unknown ROM".
        assert MainWindow._DROP_EXTENSIONS == {".bin", ".rom", ".patch"}
