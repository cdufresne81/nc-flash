"""
ROM Binary File Reader

Reads binary ROM files using ROM definition metadata.
Extracts and scales table data based on definitions.
"""

import ast
import re
import struct
import os
import numpy as np
from pathlib import Path
from typing import Optional, Union
import logging
from simpleeval import simple_eval

from .rom_definition import RomDefinition, Table, Scaling, TableType, TableLayout, AxisType
from .storage_types import STORAGE_TYPE_FORMAT, DEFAULT_FORMAT_CHAR
from .exceptions import (
    RomFileNotFoundError,
    RomReadError,
    RomWriteError,
    ScalingConversionError,
    ScalingNotFoundError,
)

logger = logging.getLogger(__name__)


def _convert_expr_to_python(expr: str) -> str:
    """
    Convert expression from calculator notation to Python notation.

    Replaces ^ with ** for exponentiation since many ROM definition
    files use ^ (calculator-style) but Python uses ** for power.
    """
    # Replace ^ with ** for exponentiation
    # This handles patterns like x^2, x^3, (expr)^2, etc.
    return re.sub(r"\^", "**", expr)


def _is_safe_numpy_expr(expr: str) -> bool:
    """
    Validate that an expression is safe to evaluate with numpy arrays.

    Only allows arithmetic on 'x' with numeric constants. Rejects
    anything that could be dangerous (function calls, attribute access,
    imports, etc.).

    Returns True if the expression uses only safe AST nodes.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return False

    _SAFE_NODES = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        # Binary operators
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.FloorDiv,
        ast.Mod,
        # Unary operators
        ast.UAdd,
        ast.USub,
        # Literals and names
        ast.Constant,
        ast.Name,
        # Parenthesised sub-expressions (implicit in BinOp nesting)
    )

    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            return False
        # Only allow 'x' as a variable name
        if isinstance(node, ast.Name) and node.id != "x":
            return False

    return True


def _compile_numpy_expr(expr: str):
    """
    Compile a scaling expression into a code object for vectorized numpy evaluation.

    The expression must pass _is_safe_numpy_expr validation. If it does, it is
    compiled once and can be evaluated many times with different numpy arrays
    bound to 'x'.

    Returns:
        A compiled code object, or None if the expression cannot be vectorized.
    """
    if not expr:
        return None
    if not _is_safe_numpy_expr(expr):
        logger.debug(f"Expression not safe for numpy vectorization: {expr}")
        return None
    try:
        return compile(expr, "<scaling>", "eval")
    except SyntaxError:
        logger.debug(f"Failed to compile expression for numpy: {expr}")
        return None


class ScalingConverter:
    """
    Converts values between raw binary and display using scaling expressions
    """

    def __init__(self, scaling: Scaling):
        """
        Initialize converter with a scaling definition

        Args:
            scaling: Scaling definition with conversion expressions
        """
        self.scaling = scaling
        # Pre-convert expressions to Python syntax
        self._toexpr = (
            _convert_expr_to_python(scaling.toexpr)
            if scaling.toexpr
            else scaling.toexpr
        )
        self._frexpr = (
            _convert_expr_to_python(scaling.frexpr)
            if scaling.frexpr
            else scaling.frexpr
        )
        # Pre-compile numpy-vectorizable code objects (None if not vectorizable)
        self._toexpr_compiled = _compile_numpy_expr(self._toexpr)
        self._frexpr_compiled = _compile_numpy_expr(self._frexpr)

    def to_display(
        self, raw_value: Union[float, np.ndarray]
    ) -> Union[float, np.ndarray]:
        """
        Convert raw binary value(s) to display value(s)

        Uses vectorized numpy evaluation when possible (compiled expression
        applied to the whole array at once). Falls back to per-element
        simpleeval for expressions that can't be safely vectorized.

        Args:
            raw_value: Raw value or array of values

        Returns:
            Converted value or array

        Raises:
            ScalingConversionError: If conversion fails
        """
        return self._eval_expr(
            self._toexpr, self._toexpr_compiled, raw_value, "to display"
        )

    def from_display(
        self, display_value: Union[float, np.ndarray]
    ) -> Union[float, np.ndarray]:
        """
        Convert display value(s) back to raw binary value(s)

        Uses vectorized numpy evaluation when possible. Falls back to
        per-element simpleeval for non-vectorizable expressions.

        Args:
            display_value: Display value or array of values

        Returns:
            Raw value or array for writing to ROM

        Raises:
            ScalingConversionError: If conversion fails
        """
        return self._eval_expr(
            self._frexpr, self._frexpr_compiled, display_value, "from display"
        )

    def _eval_expr(
        self,
        expr: str,
        compiled_expr,
        value: Union[float, np.ndarray],
        direction: str,
    ) -> Union[float, np.ndarray]:
        """
        Evaluate a scaling expression on a value or array.

        Tries vectorized numpy evaluation first (single pass over the whole
        array). Falls back to per-element simpleeval if the expression was
        not compiled or if the vectorized path raises an error.

        Args:
            expr: The expression string (for error messages and fallback)
            compiled_expr: Pre-compiled code object, or None
            value: Scalar or numpy array to transform
            direction: "to display" or "from display" (for error messages)

        Returns:
            Transformed value or array

        Raises:
            ScalingConversionError: If both vectorized and fallback paths fail
        """
        # --- Fast path: vectorised numpy evaluation ---
        if compiled_expr is not None:
            try:
                # eval with x bound to the value/array; only numpy is in scope
                result = eval(
                    compiled_expr, {"__builtins__": {}}, {"x": value}
                )  # noqa: S307
                # Ensure the result is a numpy array when the input was one
                if isinstance(value, np.ndarray) and not isinstance(result, np.ndarray):
                    result = np.full_like(value, result, dtype=float)
                return result
            except Exception as exc:
                # Vectorised eval failed (e.g. division by zero on some element).
                # Fall through to the per-element path below.
                logger.warning(
                    f"Vectorized eval failed for '{expr}' ({type(exc).__name__}: {exc}), "
                    f"falling back to per-element"
                )

        # --- Fallback: per-element simpleeval ---
        try:
            if isinstance(value, np.ndarray):
                return np.array([simple_eval(expr, names={"x": v}) for v in value])
            else:
                return simple_eval(expr, names={"x": value})
        except Exception as e:
            logger.error(f"Error converting {direction} with expr '{expr}': {e}")
            raise ScalingConversionError(
                f"Failed to convert value {direction} using expression '{expr}': {e}"
            )


class RomReader:
    """
    Reads data from ROM binary files based on ROM definitions
    """

    def __init__(self, rom_path: str, definition: RomDefinition):
        """
        Initialize ROM reader

        Args:
            rom_path: Path to ROM binary file
            definition: ROM definition with table/scaling metadata

        Raises:
            RomFileNotFoundError: If ROM file doesn't exist
        """
        self.rom_path = Path(rom_path)
        if not self.rom_path.exists():
            logger.error(f"ROM file not found: {rom_path}")
            raise RomFileNotFoundError(f"ROM file not found: {rom_path}")

        self.definition = definition
        self.rom_data = None

        logger.info(f"Initializing ROM reader for {rom_path}")
        # Load entire ROM into memory (they're < 1MB)
        self._load_rom()

    def _load_rom(self):
        """
        Load ROM binary file into memory

        Raises:
            RomReadError: If ROM file cannot be read
        """
        try:
            with open(self.rom_path, "rb") as f:
                self.rom_data = bytearray(f.read())
            logger.info(f"Loaded {len(self.rom_data)} bytes from ROM file")
        except IOError as e:
            logger.error(f"Failed to read ROM file {self.rom_path}: {e}")
            raise RomReadError(f"Failed to read ROM file: {e}")

    def verify_rom_id(self) -> bool:
        """
        Verify that the ROM matches the expected ID

        Returns:
            True if ROM ID matches, False otherwise
        """
        try:
            address = self.definition.romid.internal_id_address_int
            expected_id = self.definition.romid.internalidstring
            id_length = len(expected_id)

            raw_bytes = self.rom_data[address : address + id_length]
            actual_id = raw_bytes.decode("ascii", errors="ignore")
            if len(actual_id) != len(raw_bytes):
                logger.warning(
                    f"ROM ID at {hex(address)} contains non-ASCII bytes "
                    f"({len(raw_bytes) - len(actual_id)} bytes dropped)"
                )
            match = actual_id == expected_id

            if match:
                logger.info(f"ROM ID verified: {actual_id}")
            else:
                logger.warning(
                    f"ROM ID mismatch - Expected: {expected_id}, Found: {actual_id}"
                )

            return match
        except Exception as e:
            logger.error(f"Error verifying ROM ID: {e}")
            return False

    def _read_raw_values(
        self, address: int, count: int, scaling: Scaling
    ) -> np.ndarray:
        """
        Read raw binary values from ROM

        Args:
            address: Starting address
            count: Number of elements to read
            scaling: Scaling definition with storage type info

        Returns:
            NumPy array of raw values

        Raises:
            RomReadError: If address is out of bounds
        """
        bytes_per_elem = scaling.bytes_per_element
        total_bytes = count * bytes_per_elem

        # Validate bounds
        if address < 0:
            raise RomReadError(f"Invalid address: {hex(address)} (negative)")
        if address >= len(self.rom_data):
            raise RomReadError(
                f"Address out of bounds: {hex(address)} "
                f"(ROM size: {len(self.rom_data)} bytes)"
            )
        if address + total_bytes > len(self.rom_data):
            raise RomReadError(
                f"Read operation exceeds ROM bounds: "
                f"trying to read {total_bytes} bytes from {hex(address)} "
                f"(ROM size: {len(self.rom_data)} bytes)"
            )

        # Extract bytes
        data_bytes = self.rom_data[address : address + total_bytes]

        # Determine struct format
        endian_char = ">" if scaling.endian == "big" else "<"

        format_char = STORAGE_TYPE_FORMAT.get(
            scaling.storagetype.lower(), DEFAULT_FORMAT_CHAR
        )
        format_string = f"{endian_char}{count}{format_char}"

        # Unpack binary data
        try:
            values = struct.unpack(format_string, data_bytes)
            logger.debug(f"Read {count} values from address {hex(address)}")
            return np.array(values)
        except struct.error as e:
            logger.error(f"Error unpacking data at address {hex(address)}: {e}")
            raise RomReadError(f"Failed to unpack data at address {hex(address)}: {e}")

    def read_table_data(self, table: Table) -> Optional[dict]:
        """
        Read table data from ROM and apply scaling

        Args:
            table: Table definition

        Returns:
            Dictionary with:
                - 'values': Main table values (scaled)
                - 'x_axis': X axis values if 3D (scaled)
                - 'y_axis': Y axis values if 2D/3D (scaled)
                - 'raw_values': Raw unscaled main values

        Raises:
            ScalingNotFoundError: If scaling definition is not found
            RomReadError: If reading table data fails
        """
        logger.debug(f"Reading table data: {table.name}")

        # Get scaling for main table
        scaling = self.definition.get_scaling(table.scaling)
        if not scaling:
            logger.error(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )
            raise ScalingNotFoundError(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )

        # Handle interleaved 3D layout separately
        if table.layout == TableLayout.INTERLEAVED and table.type == TableType.THREE_D:
            return self._read_interleaved_3d(table, scaling)

        # Read main table values (contiguous layout)
        raw_values = self._read_raw_values(
            address=table.address_int, count=table.elements, scaling=scaling
        )

        # Convert to display values
        converter = ScalingConverter(scaling)
        display_values = converter.to_display(raw_values)

        result = {
            "values": display_values,
            "raw_values": raw_values,
        }

        # Read axes for 2D/3D tables
        if table.type in [TableType.TWO_D, TableType.THREE_D]:
            # Read Y axis (exists for both 2D and 3D)
            y_axis = table.y_axis
            if y_axis:
                y_scaling = self.definition.get_scaling(y_axis.scaling)
                if not y_scaling:
                    logger.warning(
                        f"Scaling '{y_axis.scaling}' not found for Y axis of '{table.name}'"
                    )
                if y_scaling:
                    y_raw = self._read_raw_values(
                        address=y_axis.address_int,
                        count=y_axis.elements,
                        scaling=y_scaling,
                    )
                    y_converter = ScalingConverter(y_scaling)
                    result["y_axis"] = y_converter.to_display(y_raw)

        if table.type == TableType.THREE_D:
            # Read X axis (only for 3D)
            x_axis = table.x_axis
            if x_axis:
                x_scaling = self.definition.get_scaling(x_axis.scaling)
                if not x_scaling:
                    logger.warning(
                        f"Scaling '{x_axis.scaling}' not found for X axis of '{table.name}'"
                    )
                if x_scaling:
                    x_raw = self._read_raw_values(
                        address=x_axis.address_int,
                        count=x_axis.elements,
                        scaling=x_scaling,
                    )
                    x_converter = ScalingConverter(x_scaling)
                    result["x_axis"] = x_converter.to_display(x_raw)

            # Reshape 3D table data into 2D grid
            if "x_axis" in result and "y_axis" in result:
                x_len = len(result["x_axis"])
                y_len = len(result["y_axis"])
                if len(display_values) == x_len * y_len:
                    # Reshape to (y_len, x_len) - rows are Y, columns are X
                    # Use 'F' (Fortran/column-major) order when swapxy is true
                    # because data is stored column-by-column in ROM
                    order = "F" if table.swapxy else "C"
                    result["values"] = display_values.reshape(
                        (y_len, x_len), order=order
                    )

        logger.debug(
            f"Read table data: {table.name} ({table.address}) ({table.type.value})"
        )
        return result

    def _read_interleaved_3d(self, table: Table, scaling: Scaling) -> dict:
        """
        Read a 3D table with interleaved Y-axis + data layout.

        Format: [M][N][X_axis: M bytes][Row0: Y0 D0..DM-1][Row1: Y1 D0..DM-1]...
        Each row is (M+1) bytes: 1 Y-axis byte followed by M data bytes.
        """
        base = table.address_int
        m = self.rom_data[base]       # X axis count
        n = self.rom_data[base + 1]   # Y axis count (row count)
        x_start = base + 2
        row_start = x_start + m
        stride = m + 1

        # Read X axis (contiguous)
        x_axis = table.x_axis
        x_raw = np.array([self.rom_data[x_start + i] for i in range(m)], dtype=np.float64)
        if x_axis:
            x_scaling = self.definition.get_scaling(x_axis.scaling)
            if x_scaling:
                x_raw = np.array(
                    [self.rom_data[x_start + i] for i in range(m)], dtype=np.float64
                )
                x_converter = ScalingConverter(x_scaling)
                x_display = x_converter.to_display(x_raw)
            else:
                x_display = x_raw
        else:
            x_display = x_raw

        # Extract Y axis (interleaved: first byte of each row)
        y_raw_list = [self.rom_data[row_start + r * stride] for r in range(n)]
        y_raw = np.array(y_raw_list, dtype=np.float64)
        y_axis = table.y_axis
        if y_axis:
            y_scaling = self.definition.get_scaling(y_axis.scaling)
            if y_scaling:
                y_converter = ScalingConverter(y_scaling)
                y_display = y_converter.to_display(y_raw)
            else:
                y_display = y_raw
        else:
            y_display = y_raw

        # Extract data (M bytes per row, skipping Y byte)
        data_list = []
        for r in range(n):
            row_base = row_start + r * stride + 1  # +1 to skip Y byte
            for c in range(m):
                data_list.append(self.rom_data[row_base + c])

        raw_values = np.array(data_list, dtype=np.float64)
        converter = ScalingConverter(scaling)
        display_values = converter.to_display(raw_values)

        result = {
            "values": display_values.reshape(n, m),
            "raw_values": raw_values,
            "x_axis": x_display,
            "y_axis": y_display,
        }

        logger.debug(
            f"Read interleaved 3D table: {table.name} ({m}x{n}) at {table.address}"
        )
        return result

    def write_table_data(self, table: Table, values: np.ndarray) -> None:
        """
        Write modified table data back to ROM (in memory, not to file yet)

        Args:
            table: Table definition
            values: New values to write (in display units)

        Raises:
            ScalingNotFoundError: If scaling definition is not found
            RomWriteError: If writing table data fails
        """
        logger.debug(f"Writing table data: {table.name}")

        scaling = self.definition.get_scaling(table.scaling)
        if not scaling:
            logger.error(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )
            raise ScalingNotFoundError(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )

        # Convert display values back to raw
        converter = ScalingConverter(scaling)

        # Flatten if 2D array (must match reshape order used in read_table_data)
        if isinstance(values, np.ndarray) and values.ndim > 1:
            order = "F" if table.swapxy else "C"
            values = values.flatten(order=order)

        # Validate element count matches table definition
        if len(values) != table.elements:
            raise RomWriteError(
                f"Value count mismatch for '{table.name}': "
                f"got {len(values)}, expected {table.elements}"
            )

        raw_values = converter.from_display(values)

        # Handle interleaved write (scatter data back into interleaved rows)
        if table.layout == TableLayout.INTERLEAVED and table.type == TableType.THREE_D:
            base = table.address_int
            m = self.rom_data[base]
            n = self.rom_data[base + 1]
            row_start = base + 2 + m
            stride = m + 1
            bytes_per_elem = scaling.bytes_per_element
            endian_char = ">" if scaling.endian == "big" else "<"
            format_char = STORAGE_TYPE_FORMAT.get(
                scaling.storagetype.lower(), DEFAULT_FORMAT_CHAR
            )
            format_string = f"{endian_char}{format_char}"

            try:
                for r in range(n):
                    for c in range(m):
                        idx = r * m + c
                        addr = row_start + r * stride + 1 + c * bytes_per_elem
                        val = raw_values[idx]
                        if format_char in ("B", "b", "H", "h", "I", "i"):
                            val = int(round(val))
                        packed = struct.pack(format_string, val)
                        self.rom_data[addr : addr + len(packed)] = packed
                logger.info(f"Successfully wrote interleaved table data: {table.name}")
            except Exception as e:
                logger.error(f"Error writing interleaved table data: {e}")
                raise RomWriteError(f"Failed to write interleaved table data: {e}")
            return

        # Pack back to binary (contiguous layout)
        address = table.address_int
        bytes_per_elem = scaling.bytes_per_element
        endian_char = ">" if scaling.endian == "big" else "<"

        format_char = STORAGE_TYPE_FORMAT.get(
            scaling.storagetype.lower(), DEFAULT_FORMAT_CHAR
        )
        format_string = f"{endian_char}{len(raw_values)}{format_char}"

        try:
            packed_data = struct.pack(format_string, *raw_values)

            # Validate bounds before writing
            if address < 0:
                raise RomWriteError(f"Invalid address: {hex(address)} (negative)")
            if address >= len(self.rom_data):
                raise RomWriteError(
                    f"Address out of bounds: {hex(address)} "
                    f"(ROM size: {len(self.rom_data)} bytes)"
                )
            if address + len(packed_data) > len(self.rom_data):
                raise RomWriteError(
                    f"Write operation exceeds ROM bounds: "
                    f"trying to write {len(packed_data)} bytes at {hex(address)} "
                    f"(ROM size: {len(self.rom_data)} bytes)"
                )

            # Modify ROM data in memory (in-place)
            self.rom_data[address : address + len(packed_data)] = packed_data
            logger.info(f"Successfully wrote table data: {table.name}")
        except RomWriteError:
            # Re-raise our own exceptions
            raise
        except Exception as e:
            logger.error(f"Error writing table data for '{table.name}': {e}")
            raise RomWriteError(f"Failed to write table data: {e}")

    def write_cell_value(
        self, table: Table, row: int, col: int, raw_value: float
    ) -> None:
        """
        Write a single cell value to ROM (in memory)

        Args:
            table: Table definition
            row: Row index in the table
            col: Column index in the table (0 for 1D/2D tables)
            raw_value: Raw binary value to write

        Raises:
            ScalingNotFoundError: If scaling definition is not found
            RomWriteError: If writing fails
        """
        scaling = self.definition.get_scaling(table.scaling)
        if not scaling:
            raise ScalingNotFoundError(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )

        # Calculate the byte address for this cell
        bytes_per_elem = scaling.bytes_per_element

        if table.layout == TableLayout.INTERLEAVED and table.type.value == "3D":
            # Interleaved: [M][N][X_axis][Y0 D0..DM-1][Y1 D0..DM-1]...
            base = table.address_int
            m = self.rom_data[base]
            row_start = base + 2 + m
            stride = m + 1
            address = row_start + row * stride + 1 + col * bytes_per_elem
        elif table.type.value == "3D":
            # Contiguous 3D: calculate linear index from row, col
            x_axis = table.x_axis
            y_axis = table.y_axis
            cols = x_axis.elements if x_axis else 1
            rows = y_axis.elements if y_axis else 1

            # Must match the reshape order used in read_table_data
            if table.swapxy:
                linear_index = col * rows + row
            else:
                linear_index = row * cols + col
            address = table.address_int + (linear_index * bytes_per_elem)
        else:
            # For 1D/2D tables, just use row
            linear_index = row
            address = table.address_int + (linear_index * bytes_per_elem)
        endian_char = ">" if scaling.endian == "big" else "<"

        format_char = STORAGE_TYPE_FORMAT.get(
            scaling.storagetype.lower(), DEFAULT_FORMAT_CHAR
        )
        format_string = f"{endian_char}{format_char}"

        try:
            # Convert to appropriate integer type if needed
            if format_char in ("B", "b", "H", "h", "I", "i"):
                raw_value = int(round(raw_value))

            packed_data = struct.pack(format_string, raw_value)

            # Validate bounds
            if address < 0 or address >= len(self.rom_data):
                raise RomWriteError(f"Address out of bounds: {hex(address)}")
            if address + len(packed_data) > len(self.rom_data):
                raise RomWriteError(f"Write exceeds ROM bounds at {hex(address)}")

            # Modify ROM data in memory (in-place)
            self.rom_data[address : address + len(packed_data)] = packed_data
            logger.debug(f"Wrote cell [{row},{col}] = {raw_value} at {hex(address)}")

        except RomWriteError:
            raise
        except Exception as e:
            logger.error(f"Error writing cell value: {e}")
            raise RomWriteError(f"Failed to write cell value: {e}")

    def write_axis_value(
        self, table: Table, axis_type: str, index: int, raw_value: float
    ) -> None:
        """
        Write a single axis value to ROM (in memory)

        Args:
            table: Table definition (parent table containing the axis)
            axis_type: 'x_axis' or 'y_axis'
            index: Index in the axis array
            raw_value: Raw binary value to write

        Raises:
            ScalingNotFoundError: If scaling definition is not found
            RomWriteError: If writing fails
        """
        # Get the axis table
        if axis_type == "x_axis":
            axis_table = table.x_axis
        elif axis_type == "y_axis":
            axis_table = table.y_axis
        else:
            raise RomWriteError(f"Invalid axis type: {axis_type}")

        if not axis_table:
            raise RomWriteError(f"Table '{table.name}' does not have {axis_type}")

        scaling = self.definition.get_scaling(axis_table.scaling)
        if not scaling:
            raise ScalingNotFoundError(
                f"Scaling '{axis_table.scaling}' not found for axis in table '{table.name}'"
            )

        # Calculate the byte offset
        bytes_per_elem = scaling.bytes_per_element
        if (table.layout == TableLayout.INTERLEAVED
                and axis_type == "y_axis"):
            # Y axis values are interleaved: first byte of each row
            base = table.address_int
            m = self.rom_data[base]
            row_start = base + 2 + m
            stride = m + 1
            address = row_start + index * stride
        else:
            address = axis_table.address_int + (index * bytes_per_elem)
        endian_char = ">" if scaling.endian == "big" else "<"

        format_char = STORAGE_TYPE_FORMAT.get(
            scaling.storagetype.lower(), DEFAULT_FORMAT_CHAR
        )
        format_string = f"{endian_char}{format_char}"

        try:
            # Convert to appropriate integer type if needed
            if format_char in ("B", "b", "H", "h", "I", "i"):
                raw_value = int(round(raw_value))

            packed_data = struct.pack(format_string, raw_value)

            # Validate bounds
            if address < 0 or address >= len(self.rom_data):
                raise RomWriteError(f"Address out of bounds: {hex(address)}")
            if address + len(packed_data) > len(self.rom_data):
                raise RomWriteError(f"Write exceeds ROM bounds at {hex(address)}")

            # Modify ROM data in memory (in-place)
            self.rom_data[address : address + len(packed_data)] = packed_data
            logger.debug(
                f"Wrote axis [{axis_type}][{index}] = {raw_value} at {hex(address)}"
            )

        except RomWriteError:
            raise
        except Exception as e:
            logger.error(f"Error writing axis value: {e}")
            raise RomWriteError(f"Failed to write axis value: {e}")

    def save_rom(self, output_path: Optional[str] = None):
        """
        Save modified ROM to file

        Args:
            output_path: Output file path, defaults to overwriting original

        Raises:
            RomWriteError: If writing ROM file fails
        """
        if output_path is None:
            output_path = self.rom_path

        logger.info(f"Saving ROM to {output_path}")

        # Atomic write: write to temp file, then replace original
        tmp_path = str(output_path) + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(self.rom_data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(output_path))
            logger.info(
                f"Successfully saved {len(self.rom_data)} bytes to {output_path}"
            )
        except Exception as e:
            # Clean up temp file on failure
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            logger.error(f"Failed to save ROM file to {output_path}: {e}")
            raise RomWriteError(f"Failed to save ROM file: {e}")
