"""
ROM Definition Data Structures

Represents the structure of ROM definition files (XML metadata)
that describe how to interpret ECU ROM binary files.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List
from enum import Enum

from .storage_types import STORAGE_TYPE_BYTES, DEFAULT_BYTE_SIZE


class TableType(Enum):
    """Type of calibration table"""
    ONE_D = "1D"
    TWO_D = "2D"
    THREE_D = "3D"


class AxisType(Enum):
    """Axis type for child tables"""
    X_AXIS = "X Axis"
    Y_AXIS = "Y Axis"


@dataclass
class RomID:
    """ROM identification metadata"""
    xmlid: str
    internalidaddress: str  # Hex address as string
    internalidstring: str
    ecuid: str
    make: str
    model: str
    flashmethod: str
    memmodel: str
    checksummodule: str
    market: Optional[str] = None
    submodel: Optional[str] = None
    transmission: Optional[str] = None
    year: Optional[str] = None

    @property
    def internal_id_address_int(self) -> int:
        """Convert hex address string to integer"""
        return int(self.internalidaddress, 16)


@dataclass
class Scaling:
    """
    Defines how to convert between raw binary values and display values

    toexpr: Expression to convert from raw to display (e.g., "x*0.01")
    frexpr: Expression to convert from display to raw (e.g., "x/0.01")
    """
    name: str
    units: str
    toexpr: str  # To display expression
    frexpr: str  # From display expression
    format: str  # Printf-style format
    min: float
    max: float
    inc: float  # Increment for editing
    storagetype: str  # float, uint8, uint16, int16, etc.
    endian: str  # big or little

    @property
    def bytes_per_element(self) -> int:
        """Calculate bytes per element based on storage type"""
        return STORAGE_TYPE_BYTES.get(self.storagetype.lower(), DEFAULT_BYTE_SIZE)

    @property
    def is_float(self) -> bool:
        """Check if storage type is floating point"""
        return self.storagetype.lower() in ['float', 'double']

    @property
    def is_signed(self) -> bool:
        """Check if storage type is signed integer"""
        return self.storagetype.lower() in ['int8', 'int16', 'int32']


@dataclass
class Table:
    """
    Calibration table definition

    For 2D tables: has 1 child (Y axis)
    For 3D tables: has 2 children (X axis and Y axis)
    """
    name: str
    address: str  # Hex address as string
    elements: int  # Total number of elements
    scaling: str  # Reference to scaling definition name
    type: TableType
    level: int = 1  # Priority level (1-4)
    category: str = ""
    swapxy: bool = False
    flipx: bool = False  # Reverse X axis order
    flipy: bool = False  # Reverse Y axis order

    # Child tables (axes for 2D/3D tables)
    children: List['Table'] = field(default_factory=list)
    axis_type: Optional[AxisType] = None  # For child axis tables

    @property
    def address_int(self) -> int:
        """Convert hex address string to integer"""
        return int(self.address, 16)

    @property
    def is_axis(self) -> bool:
        """Check if this is an axis table"""
        return self.axis_type is not None

    def get_axis(self, axis_type: AxisType) -> Optional['Table']:
        """Get child axis table by type"""
        for child in self.children:
            if child.axis_type == axis_type:
                return child
        return None

    @property
    def x_axis(self) -> Optional['Table']:
        """Get X axis child table (for 3D tables)"""
        return self.get_axis(AxisType.X_AXIS)

    @property
    def y_axis(self) -> Optional['Table']:
        """Get Y axis child table (for 2D/3D tables)"""
        return self.get_axis(AxisType.Y_AXIS)


@dataclass
class RomDefinition:
    """
    Complete ROM definition containing all metadata
    """
    romid: RomID
    scalings: Dict[str, Scaling] = field(default_factory=dict)
    tables: List[Table] = field(default_factory=list)
    xml_path: Optional[str] = None  # Path to source XML file

    # Lazy lookup caches (built on first access)
    _cache_by_category: Optional[Dict[str, List['Table']]] = field(
        default=None, init=False, repr=False, compare=False
    )
    _cache_by_name: Optional[Dict[str, 'Table']] = field(
        default=None, init=False, repr=False, compare=False
    )

    def get_scaling(self, name: str) -> Optional[Scaling]:
        """Get scaling definition by name"""
        return self.scalings.get(name)

    def _build_category_cache(self) -> Dict[str, List[Table]]:
        """Build and cache the tables-by-category lookup dict."""
        categories: Dict[str, List[Table]] = {}
        for table in self.tables:
            if table.is_axis:  # Skip axis tables
                continue
            category = table.category or "Uncategorized"
            if category not in categories:
                categories[category] = []
            categories[category].append(table)
        return categories

    def _build_name_cache(self) -> Dict[str, Table]:
        """Build and cache the table-by-name lookup dict."""
        return {table.name: table for table in self.tables}

    def get_tables_by_category(self) -> Dict[str, List[Table]]:
        """Group tables by category for UI display (cached after first call)"""
        if self._cache_by_category is None:
            self._cache_by_category = self._build_category_cache()
        return self._cache_by_category

    def get_table_by_name(self, name: str) -> Optional[Table]:
        """Find table by name (O(1) via cached dict)"""
        if self._cache_by_name is None:
            self._cache_by_name = self._build_name_cache()
        return self._cache_by_name.get(name)
