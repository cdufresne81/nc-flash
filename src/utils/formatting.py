"""
Shared formatting utilities for ROM table values.

Consolidates printf-to-Python format conversion, value formatting,
scaling range lookup, and color helpers used across UI, MCP, and
comparison modules.
"""

import math
import re
from typing import Optional

import numpy as np

_PRINTF_PATTERN = re.compile(r"%[-+0 #]*(\d*)\.?(\d*)([diouxXeEfFgGaAcspn%])")

# What a table cell editor accepts keystroke by keystroke: digits, a single
# decimal separator ('.' or ',' — comma is the decimal mark on many keyboard
# layouts) and a leading sign. No letter is typeable at all, so 'nan', 'inf'
# and friends cannot be entered. Sign and decimal separator are required
# alongside the digits: without them negative and fractional table values
# would be impossible to enter.
NUMERIC_INPUT_PATTERN = r"[+-]?\d*[.,]?\d*"

# What counts as a numeric value once committed. Same as the typed form plus
# scientific notation, which arrives via clipboard paste from spreadsheets.
# Deliberately excludes the alphabetic spellings Python's float() accepts
# ('nan', 'inf', 'infinity') and the underscore separator ('1_0').
NUMERIC_TEXT_PATTERN = r"[+-]?(\d+[.,]?\d*|[.,]\d+)([eE][+-]?\d+)?"

_NUMERIC_TEXT_RE = re.compile(rf"^{NUMERIC_TEXT_PATTERN}$")

# Cells whose scaling format is hexadecimal ('%08x', e.g. Tire Size
# Correction) display and copy as hex, so they must be typed and parsed as
# hex too. Reading '02054517' as decimal would silently store 0x001F5975.
# Capped at 16 digits (64 bits): longer text overflows float(). Values that
# do not fit the storage type are still rejected when the cell is written.
# Surrounding spaces are allowed: '%08x' renders space-padded (' 205451a'),
# and the editor opens with that text.
HEX_INPUT_PATTERN = r"\s*(0[xX])?[0-9a-fA-F]{0,16}\s*"
_HEX_TEXT_RE = re.compile(r"^(0[xX])?[0-9a-fA-F]{1,16}$")


def parse_numeric_text(text: str) -> Optional[float]:
    """Parse user-entered text into a finite float, or None if not numeric.

    This is the single gate for text entering table data (cell edits, axis
    edits, clipboard paste). It rejects anything float() would accept but a
    ROM cannot hold: 'nan', 'inf', '-infinity', '1_0'. A comma is accepted as
    the decimal separator and normalized to a point.
    """
    if text is None:
        return None
    stripped = text.strip()
    if not _NUMERIC_TEXT_RE.match(stripped):
        return None
    try:
        value = float(stripped.replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def is_hex_format(format_spec: str) -> bool:
    """True if a Python format spec renders values as hexadecimal."""
    return bool(format_spec) and format_spec[-1] in "xX"


def parse_cell_text(text: str, format_spec: str) -> Optional[float]:
    """Parse cell text according to the format the cell is displayed in.

    Hex-formatted cells take hex text (optional '0x' prefix), so a value
    copied from one hex cell pastes back unchanged. Every other cell goes
    through parse_numeric_text.
    """
    if not is_hex_format(format_spec):
        return parse_numeric_text(text)
    if text is None:
        return None
    stripped = text.strip()
    if not _HEX_TEXT_RE.match(stripped):
        return None
    return float(int(stripped, 16))


def printf_to_python_format(printf_format: str) -> str:
    """Convert printf-style format (e.g. '%0.2f') to Python format spec (e.g. '.2f')."""
    if not printf_format:
        return ".2f"
    match = _PRINTF_PATTERN.match(printf_format)
    if not match:
        return ".2f"
    width = match.group(1)
    precision = match.group(2)
    specifier = match.group(3)
    result = ""
    if width:
        result += width
    if precision:
        result += f".{precision}"
    result += specifier
    return result


_INT_SPECIFIERS = re.compile(r"[diouxX]$")


def format_value(value: float, format_spec: str) -> str:
    """Format a value using a Python format spec with error handling."""
    try:
        if _INT_SPECIFIERS.search(format_spec):
            return f"{int(round(value)):{format_spec}}"
        return f"{value:{format_spec}}"
    except (ValueError, TypeError):
        return f"{value:.2f}"


def _get_format_precision(format_spec: str) -> int:
    """Extract the number of decimal places from a Python format spec.

    Returns 0 for integer specifiers (d, x, etc.) and specs with no precision.
    """
    m = re.match(r".*?\.(\d+)[fFeEgG]", format_spec)
    if m:
        return int(m.group(1))
    # Integer specifiers or no decimal point
    return 0


def get_effective_decimal_places(value: float, max_decimals: int) -> int:
    """Count the meaningful decimal places of a float, up to max_decimals.

    Formats the value to max_decimals places, then strips trailing zeros
    to find the effective precision.

    Examples (max_decimals=2):
        12.11 -> 2,  12.10 -> 1,  12.00 -> 0
    """
    if max_decimals <= 0:
        return 0
    formatted = f"{value:.{max_decimals}f}"
    if "." not in formatted:
        return 0
    decimals = formatted.split(".")[1]
    # Strip trailing zeros
    stripped = decimals.rstrip("0")
    return len(stripped)


def round_one_level_coarser(value: float, format_spec: str) -> float:
    """Round a value one decimal level coarser than its current effective precision.

    Uses the format spec to determine the maximum possible precision, then
    detects the value's effective precision and rounds to one level less.

    Examples (format_spec='.2f'):
        12.11 -> 12.1,  12.10 -> 12.0,  12.00 -> 12.0 (no change)
    """
    max_decimals = _get_format_precision(format_spec)
    effective = get_effective_decimal_places(value, max_decimals)
    if effective <= 0:
        return value
    return round(value, effective - 1)


def get_scaling_range(rom_definition, scaling_name: str):
    """Get (min, max) from a scaling definition, or None if not defined.

    Args:
        rom_definition: RomDefinition instance (or None)
        scaling_name: Name of the scaling to look up (or None)

    Returns:
        Tuple of (min, max) or None if scaling has no valid range.
    """
    if not rom_definition or not scaling_name:
        return None
    scaling = rom_definition.get_scaling(scaling_name)
    if not scaling:
        return None
    if scaling.min == 0 and scaling.max == 0:
        return None
    if scaling.min == scaling.max:
        return None
    return (scaling.min, scaling.max)


def get_scaling_format(rom_definition, scaling_name: str) -> str:
    """Get Python format spec for a scaling name.

    Args:
        rom_definition: RomDefinition instance (or None)
        scaling_name: Name of the scaling to look up (or None)

    Returns:
        Python format spec string (defaults to '.2f').
    """
    if not rom_definition or not scaling_name:
        return ".2f"
    scaling = rom_definition.get_scaling(scaling_name)
    if not scaling or not scaling.format:
        return ".2f"
    return printf_to_python_format(scaling.format)


def all_nan(arr) -> bool:
    """Check if a numpy array is entirely NaN (float arrays only)."""
    try:
        return bool(np.all(np.isnan(arr)))
    except (TypeError, ValueError):
        return False


def get_axis_format(rom_definition, table, axis_type) -> str:
    """Get Python format spec for a table's axis.

    Args:
        rom_definition: RomDefinition instance
        table: Table with axis definitions
        axis_type: AxisType enum value

    Returns:
        Python format spec string (defaults to '.2f').
    """
    axis_table = table.get_axis(axis_type)
    if axis_table and axis_table.scaling:
        return get_scaling_format(rom_definition, axis_table.scaling)
    return ".2f"
