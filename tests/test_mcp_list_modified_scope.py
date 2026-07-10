"""Tests for McpMixin._api_list_modified (audit C7).

The /api/modified handler used to read change_tracker._pending directly and
re-derive the per-ROM filter with its own path normalization, divergent from
the tracker's public accessor. It now routes through
ChangeTracker.get_pending_changes_for_rom(document's canonical path), so a
request scopes to exactly the requested ROM and never leaks a foreign tab's
pending edits.
"""

from pathlib import Path
from types import SimpleNamespace

from src.ui.mcp_mixin import McpMixin
from src.core.change_tracker import ChangeTracker


def _table(name, address):
    return SimpleNamespace(name=name, address=address)


def _record(tracker, table, rom_path, cells):
    for row, col in cells:
        tracker.record_pending_change(
            table, row, col, 1.0, 2.0, 1, 2, rom_path=rom_path
        )


def test_list_modified_scopes_to_requested_rom():
    tracker = ChangeTracker()
    rom_a = "C:/roms/a.bin"
    rom_b = "C:/roms/b.bin"
    _record(tracker, _table("Fuel", "0x1000"), rom_a, [(0, 0), (0, 1)])
    _record(tracker, _table("Timing", "0x2000"), rom_a, [(0, 0)])
    _record(tracker, _table("Boost", "0x3000"), rom_b, [(0, 0)])  # foreign ROM

    doc = SimpleNamespace(rom_reader=SimpleNamespace(rom_path=Path(rom_a)))
    fake = SimpleNamespace(
        change_tracker=tracker,
        _find_document_by_rom_path=lambda p: doc,
    )

    result = McpMixin._api_list_modified(fake, {"rom_path": rom_a})

    assert result["success"] is True
    by_name = {t["name"]: t["changed_cells"] for t in result["tables"]}
    assert by_name == {"Fuel": 2, "Timing": 1}  # rom_b's "Boost" excluded


def test_list_modified_uses_document_path_not_request_string():
    """Filtering keys off the document's canonical Path tolerates a request
    path whose slashes/case differ from how the tracker keys were created."""
    tracker = ChangeTracker()
    rom = "C:/roms/a.bin"
    _record(tracker, _table("Fuel", "0x1000"), rom, [(0, 0)])

    doc = SimpleNamespace(rom_reader=SimpleNamespace(rom_path=Path(rom)))
    fake = SimpleNamespace(
        change_tracker=tracker,
        _find_document_by_rom_path=lambda p: doc,
    )

    # Request with back-slashes; handler resolves via the document, not the raw string.
    result = McpMixin._api_list_modified(fake, {"rom_path": "C:\\roms\\a.bin"})

    assert result["success"] is True
    assert [t["name"] for t in result["tables"]] == ["Fuel"]


def test_list_modified_unknown_rom_returns_error():
    fake = SimpleNamespace(
        change_tracker=ChangeTracker(),
        _find_document_by_rom_path=lambda p: None,
    )

    result = McpMixin._api_list_modified(fake, {"rom_path": "not/open.bin"})

    assert result["success"] is False
    assert "not open" in result["error"].lower()
