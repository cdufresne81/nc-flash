"""
Storage Type Constants

Shared mappings for ROM binary storage types.
These constants define how to interpret and serialize different data types
found in ECU ROM files.
"""

import math

# Storage type -> struct format character (for struct.pack/unpack)
# Reference: https://docs.python.org/3/library/struct.html#format-characters
STORAGE_TYPE_FORMAT = {
    "uint8": "B",
    "int8": "b",
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "float": "f",
    "double": "d",
}

# Storage type -> byte size
STORAGE_TYPE_BYTES = {
    "uint8": 1,
    "int8": 1,
    "uint16": 2,
    "int16": 2,
    "uint32": 4,
    "int32": 4,
    "float": 4,
    "double": 8,
}

# Integer format char → (min_value, max_value) for pre-pack validation
# Float/double types intentionally omitted (IEEE 754 handles overflow via inf/nan)
STORAGE_TYPE_BOUNDS = {
    "B": (0, 255),
    "b": (-128, 127),
    "H": (0, 65535),
    "h": (-32768, 32767),
    "I": (0, 4294967295),
    "i": (-2147483648, 2147483647),
}

_FLOAT32_MAX = 3.4028234663852886e38


def raw_fits_storage(raw_value: float, storage_type: str) -> bool:
    """True if a raw value can be written to this storage type unchanged
    apart from integer rounding (the same rounding the ROM writer applies).

    Lets editors reject a value before touching any state, instead of the
    writer raising later after part of a bulk change already landed.
    """
    if not math.isfinite(raw_value):
        return False
    fmt = STORAGE_TYPE_FORMAT.get((storage_type or "").lower())
    if fmt in STORAGE_TYPE_BOUNDS:
        lo, hi = STORAGE_TYPE_BOUNDS[fmt]
        return lo <= int(round(raw_value)) <= hi
    if fmt == "f":
        return abs(raw_value) <= _FLOAT32_MAX
    return True


# Default values used when storage type is unknown
DEFAULT_FORMAT_CHAR = "f"
DEFAULT_BYTE_SIZE = 4
