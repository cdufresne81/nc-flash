"""Datalog channels: decoding, formula safety, WiCAN firmware rows, persistence."""

import struct

import pytest

from src.core.datalog_channels import (
    MODE_DID,
    MODE_MEM,
    MODE_OBD,
    Channel,
    ChannelError,
    builtin_channels,
    channels_from_saved,
    channels_to_saved,
    compile_formula,
    estimated_rows_per_second,
    to_firmware_row,
)


def _builtin(name):
    return next(c for c in builtin_channels() if c.name == name)


# --- decoding -------------------------------------------------------------


def test_bench_replies_decode_to_the_expected_values():
    # Data bytes after the PID/DID echo, as the bench ECU sent them.
    assert _builtin("RPM").decode(bytes([0x1B, 0x58])) == 1750.0
    assert _builtin("SPARKADV").decode(bytes([0x94])) == 10.0
    eq = _builtin("EQ_RATIO_ACT").decode(bytes.fromhex("80008000"))
    assert eq == pytest.approx(1.0)
    assert _builtin("ECT").decode(bytes([0x00])) == -40.0
    assert _builtin("APP").decode(bytes([0x26])) == pytest.approx(14.9, abs=0.01)
    assert _builtin("FUEL_PW").decode(bytes([0x01, 0x00])) == pytest.approx(2.048)


def test_signed_float_and_bit_channels():
    signed = Channel("S", MODE_DID, 0x1000, size=2, kind="signed")
    assert signed.decode(bytes([0xFF, 0xFE])) == -2.0
    flt = Channel("F", MODE_MEM, 0xFFFFAC18, size=4, kind="float")
    assert flt.decode(struct.pack(">f", 0.875)) == 0.875
    bit = Channel("B", MODE_DID, 0x160D, bit=1)
    assert bit.decode(bytes([0b10])) == 1.0
    assert bit.decode(bytes([0b01])) == 0.0


def test_offset_skips_leading_bytes():
    ch = Channel("X", MODE_DID, 0x1234, size=1, offset=2)
    assert ch.decode(bytes([9, 9, 7])) == 7.0


def test_out_of_range_sample_is_dropped_not_clamped():
    ch = Channel("X", MODE_OBD, 0x0B, min=10, max=20)
    assert ch.decode(bytes([5])) is None
    assert ch.decode(bytes([15])) == 15.0
    assert ch.decode(bytes([25])) is None


def test_short_reply_raises():
    with pytest.raises(ChannelError):
        _builtin("RPM").decode(bytes([1]))


# --- formula safety -------------------------------------------------------


@pytest.mark.parametrize(
    "formula",
    ["x**2", "x//2", "__import__('os')", "abs(x)", "y*2", "x if x else 1", "x.real"],
)
def test_formulas_outside_the_shared_subset_are_refused(formula):
    with pytest.raises(ChannelError):
        compile_formula(formula)


def test_formula_arithmetic_and_divide_by_zero():
    assert compile_formula("-x*0.5+64")(2) == 63.0
    assert compile_formula("(x-40)/2")(50) == 5.0
    assert compile_formula("x/0")(1) != compile_formula("x/0")(1)  # NaN


# --- WiCAN firmware rows --------------------------------------------------


@pytest.mark.parametrize(
    "name,pid,expression",
    [
        # Same PID and an equivalent expression as the firmware's own defaults
        # (main/default_auto_pid.json), which are bench-proven.
        ("LOAD", "01431", "((B3*256)+B4)*0.0039215684"),
        ("MAF", "01101", "((B3*256)+B4)*0.01"),
        ("SPARKADV", "010E1", "(B3*0.5)-64"),
        ("ECT", "01051", "B3-40"),
        ("STFT", "01061", "(B3*0.78125)-100"),
        ("KNOCKR", "2217461", "B4*0.3521126761"),
        ("FUEL_PW", "2214101", "((B4*256)+B5)*0.008"),
        ("VCT_ACT", "2216CD1", "((B4*256)+B5)*0.0625"),
        ("HIDET_SW", "2217061", "B4&1"),
    ],
)
def test_firmware_rows_match_the_firmware_defaults(name, pid, expression):
    row = to_firmware_row(_builtin(name))
    assert row["PID"] == pid
    assert row["Expression"] == expression


def test_firmware_row_fields():
    row = to_firmware_row(_builtin("ECT"))
    assert row["Mode"] == "01"
    assert row["SampleEvery"] == 8
    assert row["MinValue"] == "" and row["MaxValue"] == ""
    assert row["enabled"] is True


def test_firmware_numbers_are_fixed_point_and_no_unary_minus():
    # The firmware parser can't read 1e-05, and has no unary minus.
    ch = Channel("T", MODE_OBD, 0x0C, size=2, formula="-x*0.00001")
    expr = to_firmware_row(ch)["Expression"]
    assert "e" not in expr.lower()
    assert "0.00001" in expr
    assert expr.startswith("(0-")


def test_mode_23_pid_carries_address_and_read_size():
    ch = Channel("VOLEFF", MODE_MEM, 0xFFFFAC18, size=4, kind="float")
    row = to_firmware_row(ch)
    assert row["PID"] == "23FFFFAC1800041"
    assert row["Expression"] == "F2"


def test_bit_channel_inside_a_formula_keeps_the_mask_first():
    ch = Channel("SW", MODE_DID, 0x160D, bit=1, formula="x*100")
    assert to_firmware_row(ch)["Expression"] == "((B4>>1)&1)*100"


def test_channel_past_the_first_frame_is_refused():
    # A mode 0x22 reply carries 4 data bytes in its first frame.
    with pytest.raises(ChannelError):
        Channel("X", MODE_DID, 0x1000, size=4, offset=1).validate()
    with pytest.raises(ChannelError):
        Channel("X", MODE_MEM, 0xFFFF1000, size=4, offset=3).validate()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "A,B"},
        {"name": "X" * 32},
        {"mode": 0x10},
        {"ident": 0x100},
        {"every": 0},
        {"every": 65},
        {"kind": "float"},  # size 1
        {"bit": 8},
        {"min": 5, "max": 1},
        {"name": "Time"},
        {"name": "timestamp_ms"},
        {"mode": 0x22, "ident": 0x1000, "kind": "signed", "size": 3},
        {"mode": 0x23, "ident": 0x00001000, "size": 2},  # ROM, not RAM
        {"mode": 0x23, "ident": 0xFFFF7117, "size": 2},  # misaligned
        {"mode": 0x23, "ident": 0xFFFFAC1A, "size": 4, "kind": "float"},
        {"mode": 0x23, "ident": 0xFFFFBFFF, "size": 2},  # runs past RAM
        {"mode": 0x23, "ident": 0xFFFFE400, "size": 4},  # peripheral registers
    ],
)
def test_invalid_channels(kwargs):
    base = {"name": "OK", "mode": MODE_OBD, "ident": 0x0C}
    base.update(kwargs)
    with pytest.raises(ChannelError):
        Channel(**base).validate()


def test_every_builtin_channel_is_valid_and_has_a_firmware_row():
    for ch in builtin_channels():
        ch.validate()
        to_firmware_row(ch)


# --- the user's list ------------------------------------------------------


def test_saved_list_round_trips_and_keeps_custom_channels():
    channels = builtin_channels()
    channels[0].enabled = False
    channels[1].every = 4
    channels.append(Channel("BRAKE", MODE_MEM, 0xFFFF7116, size=2, every=2))
    rebuilt = channels_from_saved(channels_to_saved(channels))
    assert rebuilt[0].enabled is False
    assert rebuilt[1].every == 4
    custom = rebuilt[-1]
    assert (custom.name, custom.mode, custom.ident, custom.builtin) == (
        "BRAKE",
        MODE_MEM,
        0xFFFF7116,
        False,
    )


def test_damaged_or_duplicate_saved_entries_are_skipped():
    saved = {
        "builtin": {"RPM": {"enabled": "yes", "every": 999}},
        "custom": [
            {"name": "RPM", "mode": 0x22, "ident": 1},  # clashes with a built-in
            {"name": "BAD", "mode": 0x99, "ident": 1},
            "junk",
            {"name": "GOOD", "mode": 0x22, "ident": 0x1234},
        ],
    }
    channels = channels_from_saved(saved)
    rpm = channels[0]
    assert rpm.enabled is True and rpm.every == 1  # bad prefs ignored
    assert [c.name for c in channels if not c.builtin] == ["GOOD"]
    assert channels_from_saved(None) == builtin_channels()


def test_rate_estimate_counts_slow_services():
    fast = [Channel("A", MODE_OBD, 0x0C)] * 10
    slow = [Channel("B", MODE_DID, 0x1746)] * 2
    assert estimated_rows_per_second(fast) > 80
    assert estimated_rows_per_second(slow) == pytest.approx(62.5)
    assert estimated_rows_per_second([]) == 0.0
