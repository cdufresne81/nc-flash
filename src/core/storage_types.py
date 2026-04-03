"""
Storage Type Constants

Shared mappings for ROM binary storage types.
These constants define how to interpret and serialize different data types
found in ECU ROM files.
"""

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

# Default values used when storage type is unknown
DEFAULT_FORMAT_CHAR = "f"
DEFAULT_BYTE_SIZE = 4
