"""Host-side SD-staged flash packaging (Option B Phase 3).

Proves :func:`build_flash_package` produces a self-consistent, firmware-ready
package whose bytes match an *independent* recompute of the known-good
``FlashManager._flash_rom_inner`` host prep — so the staged flash is byte-identical
to a J2534 flash. The pure helpers (filename / sanitisation / arg validation) are
tested without ``_secure``; the full-build tests skip when the security IP is
absent (it is required to produce real SBL bytes).
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from src.ecu.checksum import correct_rom_checksums, crc32
from src.ecu.constants import ROM_FLASH_START_MIN, ROM_SIZE, SBL_SIZE
from src.ecu.exceptions import FlashError, ROMValidationError, SecureModuleNotAvailable
from src.ecu.rom_utils import calculate_flash_start_index, detect_vehicle_generation
from src.ecu.wican_sd_package import (
    MANIFEST_VERSION,
    SECURE_MODULE_AVAILABLE,
    STAGED_IMAGE_LEN,
    FlashPackage,
    _STAGED_STEM_MAX,
    _sanitize_filename_stem,
    build_flash_package,
    staged_filename,
)

_ROM_PATH = Path(__file__).resolve().parent.parent / "examples" / "lf9veb.bin"

requires_secure = pytest.mark.skipif(
    not SECURE_MODULE_AVAILABLE, reason="_secure module (SBL IP) not installed"
)


@pytest.fixture(scope="module")
def rom() -> bytes:
    return _ROM_PATH.read_bytes()


# --- Pure helpers (no _secure needed) ---------------------------------------


class TestStagedFilename:
    def test_format_is_name_then_yyyymmdd_hhmm(self):
        # Convention: <source-name>_<YYYYMMDD>_<HHMM>.bin (underscore-joined).
        name = staged_filename("SW-LFDJEA000", datetime(2026, 6, 23, 17, 45))
        assert name == "SW-LFDJEA000_20260623_1745.bin"

    def test_named_after_display_file_drops_extension(self):
        # The full NC Flash filename becomes the stem (its .bin is dropped, the
        # staged .bin re-appended) — no bare-timestamp-only name.
        name = staged_filename("My Stage 2 Tune.bin", datetime(2026, 6, 23, 17, 45))
        assert name == "My_Stage_2_Tune_20260623_1745.bin"

    def test_minute_precision_no_seconds(self):
        name = staged_filename("X", datetime(2026, 1, 2, 3, 4, 59))
        assert name == "X_20260102_0304.bin"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SW-LFDJEA000", "SW-LFDJEA000"),
            # Accents transliterate to ASCII (recognisable), spaces -> "_".
            ("Tune éà v2.bin", "Tune_ea_v2"),
            # Real-world example: accents + spaces + a meaningful decimal point.
            ("Testé AFR à 12.5.bin", "Teste_AFR_a_12.5"),
            # A doubled dot is collapsed (a single "." is kept) so the upload's
            # ".." path-traversal guard never rejects a legitimate ROM name.
            ("Stage..2.bin", "Stage.2"),
            ("café crème", "cafe_creme"),
            ("Ñoño", "Nono"),
            # Spaces / path separators / weird chars all collapse safely.
            ("a/b\\c:d", "c_d"),  # basename only, ":" -> "_"
            ("  spaced  ", "spaced"),
            ("my  tune", "my_tune"),  # collapsed run
            ("..trav..", "trav"),
            ("", "ecu_rom"),
            ("///", "ecu_rom"),
            ("éàç.bin", "eac"),  # all-accent stem still non-empty
            ("名前.bin", "ecu_rom"),  # no ASCII transliteration -> default
        ],
    )
    def test_sanitize_filename_stem(self, raw, expected):
        result = _sanitize_filename_stem(raw)
        assert result == expected
        # Invariants the transport relies on: pure ASCII, no spaces/separators.
        assert result.isascii()
        assert " " not in result and "/" not in result and "\\" not in result

    def test_sanitize_caps_length(self):
        stem = _sanitize_filename_stem("A" * 200 + ".bin")
        assert len(stem) == _STAGED_STEM_MAX
        assert stem == "A" * _STAGED_STEM_MAX

    def test_collision_differs_by_minute(self):
        a = staged_filename("ID", datetime(2026, 6, 23, 17, 45))
        b = staged_filename("ID", datetime(2026, 6, 23, 17, 46))
        assert a != b


# --- Argument validation (reaches the secure guard first if absent) ---------


@requires_secure
class TestArgValidation:
    def test_invalid_flash_type(self, rom):
        with pytest.raises(FlashError, match="Invalid flash_type"):
            build_flash_package(rom, flash_type="sideways")

    def test_wrong_rom_size(self):
        with pytest.raises(ROMValidationError):
            build_flash_package(b"\x00" * 1234, flash_type="full")

    def test_dynamic_requires_archive(self, rom):
        with pytest.raises(ROMValidationError, match="archive"):
            build_flash_package(rom, flash_type="dynamic", archive_data=None)

    def test_dynamic_identical_roms(self, rom):
        with pytest.raises(ROMValidationError, match="identical"):
            build_flash_package(rom, flash_type="dynamic", archive_data=rom)


@pytest.mark.skipif(
    SECURE_MODULE_AVAILABLE, reason="only meaningful when _secure is absent"
)
def test_build_without_secure_raises():
    with pytest.raises(SecureModuleNotAvailable):
        build_flash_package(b"\x00" * ROM_SIZE, flash_type="full")


# --- Full build, cross-checked against an independent recompute --------------


@requires_secure
class TestFullFlashPackage:
    def test_manifest_and_layout(self, rom):
        from src.ecu import _secure

        pkg = build_flash_package(
            rom,
            flash_type="full",
            rom_id="SW-LFDJEA000",
            when=datetime(2026, 6, 23, 17, 45),
        )
        m = pkg.manifest

        assert isinstance(pkg, FlashPackage)
        assert m["manifest_version"] == MANIFEST_VERSION
        assert m["flash_type"] == "full"
        assert m["generation"] == "NC2"
        assert m["cal_id"] == "LF9VEB"
        assert m["flash_start_index"] == ROM_FLASH_START_MIN
        assert m["sbl_offset"] == ROM_SIZE
        assert m["sbl_len"] == SBL_SIZE
        assert m["program_offset"] == ROM_FLASH_START_MIN
        assert m["program_len"] == ROM_SIZE - ROM_FLASH_START_MIN
        assert m["image_len"] == STAGED_IMAGE_LEN == len(pkg.image)
        assert m["staged_filename"] == "SW-LFDJEA000_20260623_1745.bin"
        assert "diff_offset" not in m  # full flash has no diff

        # Independent recompute of the exact flash_manager host prep.
        buf = bytearray(rom)
        correct_rom_checksums(buf)
        gen = detect_vehicle_generation(rom)
        sbl = _secure.get_sbl_data(ROM_FLASH_START_MIN, gen)
        assert pkg.corrected_rom == bytes(buf)
        assert pkg.sbl == sbl
        assert pkg.program == bytes(buf[ROM_FLASH_START_MIN:])
        assert pkg.image == bytes(buf) + sbl

    def test_digests_self_consistent(self, rom):
        pkg = build_flash_package(rom, flash_type="full")
        m = pkg.manifest
        assert m["image_sha256"] == hashlib.sha256(pkg.image).hexdigest()
        assert m["image_crc32"] == crc32(pkg.image)
        assert m["rom_sha256"] == hashlib.sha256(pkg.corrected_rom).hexdigest()

    def test_deterministic(self, rom):
        when = datetime(2026, 6, 23, 17, 45)
        a = build_flash_package(rom, flash_type="full", rom_id="ID", when=when)
        b = build_flash_package(rom, flash_type="full", rom_id="ID", when=when)
        assert a.image == b.image
        assert a.manifest == b.manifest

    def test_rom_id_defaults_to_cal_id(self, rom):
        pkg = build_flash_package(
            rom, flash_type="full", when=datetime(2026, 6, 23, 17, 45)
        )
        # cal_id of the example ROM is LF9VEB.
        assert pkg.manifest["rom_id"] == "LF9VEB"
        assert pkg.staged_filename == "LF9VEB_20260623_1745.bin"

    def test_source_name_drives_staged_filename(self, rom):
        # The staged file is named after the ROM shown in NC Flash (spaces +
        # accents made transport-safe); rom_id stays the manifest identity.
        pkg = build_flash_package(
            rom,
            flash_type="full",
            rom_id="SW-LFDJEA000",
            source_name="Miata éà Stage 2.bin",
            when=datetime(2026, 6, 23, 17, 45),
        )
        assert pkg.staged_filename == "Miata_ea_Stage_2_20260623_1745.bin"
        assert pkg.staged_filename.isascii()  # transport requires pure ASCII
        assert pkg.manifest["rom_id"] == "SW-LFDJEA000"  # identity unchanged

    def test_blank_source_name_falls_back_to_label(self, rom):
        pkg = build_flash_package(
            rom,
            flash_type="full",
            rom_id="SW-LFDJEA000",
            source_name="   ",
            when=datetime(2026, 6, 23, 17, 45),
        )
        assert pkg.staged_filename == "SW-LFDJEA000_20260623_1745.bin"


# --- Dynamic flash: only the changed region streams --------------------------


@requires_secure
class TestDynamicFlashPackage:
    @pytest.mark.parametrize(
        "flip_at,expected_start",
        [
            (0x2500, 0x2000),  # low region -> 0x1000-aligned, clamped to min
            (0x3500, 0x3000),  # low region -> 0x1000-aligned
            (0x50000, 0x40000),  # high region -> 0x20000-aligned
        ],
    )
    def test_changed_region_drives_flash_start(self, rom, flip_at, expected_start):
        new = bytearray(rom)
        new[flip_at] ^= 0xFF  # guaranteed-different byte at a known offset
        new = bytes(new)

        pkg = build_flash_package(new, flash_type="dynamic", archive_data=rom)
        m = pkg.manifest

        assert m["flash_type"] == "dynamic"
        assert m["diff_offset"] == flip_at
        assert calculate_flash_start_index(flip_at) == expected_start
        assert m["flash_start_index"] == expected_start
        assert m["program_offset"] == expected_start
        assert m["program_len"] == ROM_SIZE - expected_start

        # The streamed program slice is exactly the corrected ROM tail.
        buf = bytearray(new)
        correct_rom_checksums(buf)
        assert pkg.program == bytes(buf[expected_start:])
        assert pkg.image == bytes(buf) + pkg.sbl
