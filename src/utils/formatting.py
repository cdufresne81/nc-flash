"""
Shared formatting utilities for ROM table values.

Consolidates printf-to-Python format conversion, value formatting,
scaling range lookup, and color helpers used across UI, MCP, and
comparison modules.
"""

import re

import numpy as np

_PRINTF_PATTERN = re.compile(r"%[-+0 #]*(\d*)\.?(\d*)([diouxXeEfFgGaAcspn%])")


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


def format_value(value: float, format_spec: str) -> str:
    """Format a value using a Python format spec with error handling."""
    try:
        return f"{value:{format_spec}}"
    except (ValueError, TypeError):
        return f"{value:.2f}"


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
