"""
ROM Definition Data Structures

Represents the structure of ROM definition files (XML metadata)
that describe how to interpret ECU ROM binary files.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Union
from enum import Enum

from .storage_types import STORAGE_TYPE_BYTES, DEFAULT_BYTE_SIZE
from .table_lookup import parse_address, qualified_name, split_qualified_name


class TableType(Enum):
    """Type of calibration table"""

    ONE_D = "1D"
    TWO_D = "2D"
    THREE_D = "3D"


class AxisType(Enum):
    """Axis type for child tables"""

    X_AXIS = "X Axis"
    Y_AXIS = "Y Axis"


class TableLayout(Enum):
    """Memory layout for table data"""

    CONTIGUOUS = (
        "contiguous"  # Standard: axes and data at separate contiguous addresses
    )
    INTERLEAVED = "interleaved"  # TCM-style: [M][N][X_axis][Y0 D0..DM][Y1 D1..DM]...


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
        return self.storagetype.lower() in ["float", "double"]

    @property
    def is_signed(self) -> bool:
        """Check if storage type is signed integer"""
        return self.storagetype.lower() in ["int8", "int16", "int32"]


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
    layout: TableLayout = TableLayout.CONTIGUOUS  # Memory layout

    # Child tables (axes for 2D/3D tables)
    children: List["Table"] = field(default_factory=list)
    axis_type: Optional[AxisType] = None  # For child axis tables

    @property
    def address_int(self) -> int:
        """Convert hex address string to integer"""
        return int(self.address, 16)

    @property
    def is_axis(self) -> bool:
        """Check if this is an axis table"""
        return self.axis_type is not None

    def get_axis(self, axis_type: AxisType) -> Optional["Table"]:
        """Get child axis table by type"""
        for child in self.children:
            if child.axis_type == axis_type:
                return child
        return None

    @property
    def x_axis(self) -> Optional["Table"]:
        """Get X axis child table (for 3D tables)"""
        return self.get_axis(AxisType.X_AXIS)

    @property
    def y_axis(self) -> Optional["Table"]:
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
    _cache_by_category: Optional[Dict[str, List["Table"]]] = field(
        default=None, init=False, repr=False, compare=False
    )
    # name -> every table with that exact name, in definition order
    _cache_by_name: Optional[Dict[str, List["Table"]]] = field(
        default=None, init=False, repr=False, compare=False
    )
    # stripped name -> tables whose name differs from it only by whitespace
    _cache_by_stripped_name: Optional[Dict[str, List["Table"]]] = field(
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

    def _build_name_cache(self) -> None:
        """Build and cache the table-by-name lookup dicts.

        Names are NOT unique (definitions reuse some at different addresses),
        so each key maps to every copy. The stripped-name dict lets lookups
        work regardless of trailing whitespace in the XML definition.
        """
        exact: Dict[str, List[Table]] = {}
        stripped: Dict[str, List[Table]] = {}
        for table in self.tables:
            exact.setdefault(table.name, []).append(table)
            if table.name.strip() != table.name:
                stripped.setdefault(table.name.strip(), []).append(table)
        self._cache_by_name = exact
        self._cache_by_stripped_name = stripped

    def get_tables_by_category(self) -> Dict[str, List[Table]]:
        """Group tables by category for UI display (cached after first call)"""
        if self._cache_by_category is None:
            self._cache_by_category = self._build_category_cache()
        return self._cache_by_category

    def get_tables_by_name(self, name: str) -> List[Table]:
        """Every table called ``name``, in definition order (may be several)."""
        if self._cache_by_name is None:
            self._build_name_cache()
        found = self._cache_by_name.get(name)
        if found is None:
            found = self._cache_by_stripped_name.get(name, [])
        return list(found)

    def is_name_ambiguous(self, name: str) -> bool:
        """True when more than one table carries this name."""
        return len(self.get_tables_by_name(name)) > 1

    def display_name(self, table: Table) -> str:
        """The table's name, or "Name @ 0xADDR" when other tables share it."""
        if self.is_name_ambiguous(table.name):
            return qualified_name(table)
        return table.name

    def get_table_by_name(
        self, name: str, address: Optional[Union[int, str]] = None
    ) -> Optional[Table]:
        """Find one table by name.

        Several tables can share a name. A unique name returns its table, as
        it always has. A shared name raises AmbiguousTableNameError (listing
        each copy) unless the copy is picked by ``address`` or by the
        "Name @ 0xADDR" form. Returns None when nothing matches; raises
        ValueError for a malformed ``address``.
        """
        from .exceptions import AmbiguousTableNameError

        wanted = parse_address(address) if address is not None else None
        copies = self.get_tables_by_name(name)
        if not copies:
            base, suffix_addr = split_qualified_name(name)
            if suffix_addr is None:
                return None
            if wanted is not None and wanted != suffix_addr:
                return None
            wanted = suffix_addr
            copies = self.get_tables_by_name(base)

        if wanted is not None:
            copies = [t for t in copies if _table_address(t) == wanted]
            if len(copies) > 1:
                raise AmbiguousTableNameError(name, copies)
            return copies[0] if copies else None

        if len(copies) > 1:
            raise AmbiguousTableNameError(name, copies)
        return copies[0]


def _table_address(table: Table) -> Optional[int]:
    try:
        return table.address_int
    except (TypeError, ValueError):
        return None
