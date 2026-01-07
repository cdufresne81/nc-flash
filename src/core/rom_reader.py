"""
ROM Binary File Reader

Reads binary ROM files using ROM definition metadata.
Extracts and scales table data based on definitions.
"""

import struct
import numpy as np
from pathlib import Path
from typing import Optional, Union, List
import logging
from simpleeval import simple_eval

from .rom_definition import RomDefinition, Table, Scaling, TableType
from .exceptions import (
    RomFileNotFoundError,
    RomReadError,
    RomWriteError,
    ScalingConversionError,
    ScalingNotFoundError,
    InvalidRomFileError
)

logger = logging.getLogger(__name__)


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

    def to_display(self, raw_value: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
        """
        Convert raw binary value(s) to display value(s)

        Args:
            raw_value: Raw value or array of values

        Returns:
            Converted value or array

        Raises:
            ScalingConversionError: If conversion fails
        """
        try:
            # Use simpleeval for safe expression evaluation
            if isinstance(raw_value, np.ndarray):
                return np.array([simple_eval(self.scaling.toexpr, names={'x': v}) for v in raw_value])
            else:
                return simple_eval(self.scaling.toexpr, names={'x': raw_value})
        except Exception as e:
            logger.error(f"Error converting to display with expr '{self.scaling.toexpr}': {e}")
            raise ScalingConversionError(
                f"Failed to convert raw value to display using expression '{self.scaling.toexpr}': {e}"
            )

    def from_display(self, display_value: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
        """
        Convert display value(s) back to raw binary value(s)

        Args:
            display_value: Display value or array of values

        Returns:
            Raw value or array for writing to ROM

        Raises:
            ScalingConversionError: If conversion fails
        """
        try:
            if isinstance(display_value, np.ndarray):
                return np.array([simple_eval(self.scaling.frexpr, names={'x': v}) for v in display_value])
            else:
                return simple_eval(self.scaling.frexpr, names={'x': display_value})
        except Exception as e:
            logger.error(f"Error converting from display with expr '{self.scaling.frexpr}': {e}")
            raise ScalingConversionError(
                f"Failed to convert display value to raw using expression '{self.scaling.frexpr}': {e}"
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
            with open(self.rom_path, 'rb') as f:
                self.rom_data = f.read()
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

            actual_id = self.rom_data[address:address + id_length].decode('ascii', errors='ignore')
            match = actual_id == expected_id

            if match:
                logger.info(f"ROM ID verified: {actual_id}")
            else:
                logger.warning(f"ROM ID mismatch - Expected: {expected_id}, Found: {actual_id}")

            return match
        except Exception as e:
            logger.error(f"Error verifying ROM ID: {e}")
            return False

    def _read_raw_values(self, address: int, count: int, scaling: Scaling) -> np.ndarray:
        """
        Read raw binary values from ROM

        Args:
            address: Starting address
            count: Number of elements to read
            scaling: Scaling definition with storage type info

        Returns:
            NumPy array of raw values
        """
        bytes_per_elem = scaling.bytes_per_element
        total_bytes = count * bytes_per_elem

        # Extract bytes
        data_bytes = self.rom_data[address:address + total_bytes]

        # Determine struct format
        endian_char = '>' if scaling.endian == 'big' else '<'

        # Map storage types to struct format characters
        type_map = {
            'uint8': 'B',
            'int8': 'b',
            'uint16': 'H',
            'int16': 'h',
            'uint32': 'I',
            'int32': 'i',
            'float': 'f',
            'double': 'd',
        }

        format_char = type_map.get(scaling.storagetype.lower(), 'f')
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
            logger.error(f"Scaling '{table.scaling}' not found for table '{table.name}'")
            raise ScalingNotFoundError(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )

        # Read main table values
        raw_values = self._read_raw_values(
            address=table.address_int,
            count=table.elements,
            scaling=scaling
        )

        # Convert to display values
        converter = ScalingConverter(scaling)
        display_values = converter.to_display(raw_values)

        result = {
            'values': display_values,
            'raw_values': raw_values,
        }

        # Read axes for 2D/3D tables
        if table.type in [TableType.TWO_D, TableType.THREE_D]:
            # Read Y axis (exists for both 2D and 3D)
            y_axis = table.y_axis
            if y_axis:
                y_scaling = self.definition.get_scaling(y_axis.scaling)
                if y_scaling:
                    y_raw = self._read_raw_values(
                        address=y_axis.address_int,
                        count=y_axis.elements,
                        scaling=y_scaling
                    )
                    y_converter = ScalingConverter(y_scaling)
                    result['y_axis'] = y_converter.to_display(y_raw)

        if table.type == TableType.THREE_D:
            # Read X axis (only for 3D)
            x_axis = table.x_axis
            if x_axis:
                x_scaling = self.definition.get_scaling(x_axis.scaling)
                if x_scaling:
                    x_raw = self._read_raw_values(
                        address=x_axis.address_int,
                        count=x_axis.elements,
                        scaling=x_scaling
                    )
                    x_converter = ScalingConverter(x_scaling)
                    result['x_axis'] = x_converter.to_display(x_raw)

            # Reshape 3D table data into 2D grid
            if 'x_axis' in result and 'y_axis' in result:
                x_len = len(result['x_axis'])
                y_len = len(result['y_axis'])
                if len(display_values) == x_len * y_len:
                    # Reshape to (y_len, x_len) - rows are Y, columns are X
                    result['values'] = display_values.reshape((y_len, x_len))

        logger.info(f"Successfully read table: {table.name} ({table.type.value})")
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
            logger.error(f"Scaling '{table.scaling}' not found for table '{table.name}'")
            raise ScalingNotFoundError(
                f"Scaling '{table.scaling}' not found for table '{table.name}'"
            )

        # Convert display values back to raw
        converter = ScalingConverter(scaling)

        # Flatten if 2D array
        if isinstance(values, np.ndarray) and values.ndim > 1:
            values = values.flatten()

        raw_values = converter.from_display(values)

        # Pack back to binary
        address = table.address_int
        bytes_per_elem = scaling.bytes_per_element
        endian_char = '>' if scaling.endian == 'big' else '<'

        type_map = {
            'uint8': 'B', 'int8': 'b',
            'uint16': 'H', 'int16': 'h',
            'uint32': 'I', 'int32': 'i',
            'float': 'f', 'double': 'd',
        }
        format_char = type_map.get(scaling.storagetype.lower(), 'f')
        format_string = f"{endian_char}{len(raw_values)}{format_char}"

        try:
            packed_data = struct.pack(format_string, *raw_values)
            # Modify ROM data in memory
            self.rom_data = (
                self.rom_data[:address] +
                packed_data +
                self.rom_data[address + len(packed_data):]
            )
            logger.info(f"Successfully wrote table data: {table.name}")
        except Exception as e:
            logger.error(f"Error writing table data for '{table.name}': {e}")
            raise RomWriteError(f"Failed to write table data: {e}")

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

        try:
            with open(output_path, 'wb') as f:
                f.write(self.rom_data)
            logger.info(f"Successfully saved {len(self.rom_data)} bytes to {output_path}")
        except IOError as e:
            logger.error(f"Failed to save ROM file to {output_path}: {e}")
            raise RomWriteError(f"Failed to save ROM file: {e}")
