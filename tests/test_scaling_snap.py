"""Display -> raw on scalings whose frexpr only approximates toexpr (issue 172).

The fan ECT threshold scaling uses a fitted cubic both ways. Below about
75 C its frexpr lands on raw counts that read back far from what was typed
(60 typed, 29 stored). from_display now picks the raw count whose display is
closest to the typed value, and leaves exact (linear) scalings alone.
"""

import glob
import os

import numpy as np
import pytest

from src.core.definition_parser import load_definition
from src.core.rom_definition import Scaling
from src.core.rom_reader import ScalingConverter, _forward_table

FAN_ECT = Scaling(
    name="FAN_ECT",
    units="",
    toexpr="(180-(1.42*x)+(0.00765*x^2)-(0.0000163*x^3))",
    frexpr="(1005-(18.6*x) + (0.125*x^2) - (0.0002978*x^3))+0.5",
    format="%d",
    min=0,
    max=0,
    inc=1,
    storagetype="uint16",
    endian="big",
)


def _scaling(toexpr, frexpr, storagetype):
    return Scaling("s", "", toexpr, frexpr, "%0.2f", 0, 0, 1, storagetype, "big")


def _shown(conv, raw):
    return float(conv.to_display(float(raw)))


def _half_step(conv, raw):
    return (
        max(
            abs(_shown(conv, raw + 1) - _shown(conv, raw)),
            abs(_shown(conv, raw) - _shown(conv, raw - 1)),
        )
        / 2
    )


@pytest.mark.parametrize("typed", [50, 60, 64, 70, 75, 90, 110, 120, 150])
def test_fan_ect_typed_value_reads_back_within_half_a_step(typed):
    conv = ScalingConverter(FAN_ECT)
    raw = round(conv.from_display(float(typed)))
    assert abs(_shown(conv, raw) - typed) <= _half_step(conv, raw) + 1e-9


def test_fan_ect_60_no_longer_stores_29():
    conv = ScalingConverter(FAN_ECT)
    raw = round(conv.from_display(60.0))
    assert raw != 275  # what frexpr alone gives: reads back as 29 C
    assert abs(_shown(conv, raw) - 60) < 0.5


def test_fan_ect_every_stored_value_round_trips_exactly():
    """Undo, interpolation rollback and copy convert a displayed value back
    to raw: every raw count must come back as itself."""
    conv = ScalingConverter(FAN_ECT)
    misses = [
        r for r in range(0, 700) if round(conv.from_display(_shown(conv, r))) != r
    ]
    assert misses == []


def test_fan_ect_array_conversion_matches_scalar():
    conv = ScalingConverter(FAN_ECT)
    typed = np.array([50.0, 60.0, 90.0, 110.0])
    raws = conv.from_display(typed)
    assert isinstance(raws, np.ndarray)
    assert [round(r) for r in raws] == [round(conv.from_display(t)) for t in typed]


def test_value_beyond_the_scaling_is_still_rejected_not_clamped():
    """Above 180 C nothing reads back close: the out-of-range raw from frexpr
    is passed on unchanged so the caller refuses it."""
    conv = ScalingConverter(FAN_ECT)
    raw = conv.from_display(200.0)
    assert raw < 0


@pytest.mark.parametrize("typed", [127.75, 128.0, 300.0, -0.3, -1.0])
def test_exact_scaling_out_of_range_is_not_clamped(typed):
    conv = ScalingConverter(_scaling("x*0.5", "x/0.5", "uint8"))
    assert conv.from_display(typed) == typed / 0.5


def test_non_finite_values_pass_through():
    conv = ScalingConverter(FAN_ECT)
    assert np.isnan(conv.from_display(float("nan")))


def test_float_tables_are_untouched():
    conv = ScalingConverter(_scaling("x*2+1", "(x-1)/3", "float"))
    assert conv.from_display(7.0) == 2.0  # deliberately wrong frexpr, kept


def test_forward_table_matches_to_display():
    conv = ScalingConverter(FAN_ECT)
    lo, displays = _forward_table(conv._toexpr, "uint16")
    for raw in (0, 1, 77, 186, 275, 699, 65535):
        assert displays[raw - lo] == pytest.approx(_shown(conv, raw), rel=1e-12)


_METADATA = os.path.join(os.path.dirname(__file__), "..", "examples", "metadata")


def _bundled_linear_scalings():
    seen = {}
    for path in sorted(glob.glob(os.path.join(_METADATA, "*.xml"))):
        try:
            definition = load_definition(path)
        except Exception:
            continue
        for sc in definition.scalings.values():
            key = (sc.toexpr, sc.frexpr, sc.storagetype.lower())
            if key[2] in ("uint8", "int8", "uint16", "int16") and "^" not in key[0]:
                seen.setdefault(key, sc)
    return list(seen.values())


@pytest.mark.parametrize(
    "scaling", _bundled_linear_scalings(), ids=lambda s: f"{s.toexpr}|{s.storagetype}"
)
def test_bundled_exact_scalings_give_the_same_raw_as_before(scaling):
    conv = ScalingConverter(scaling)
    for raw in list(range(0, 300)) + list(range(0, 65536, 251)):
        shown = _shown(conv, raw)
        for typed in (shown, shown + 1e-3, shown * 1.0003 + 0.01):
            before = conv._eval_expr(conv._frexpr, None, typed, "from display")
            assert round(conv.from_display(typed)) == round(before)
