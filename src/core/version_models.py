"""
Version Control Data Models

Dataclasses for representing changes, commits, and projects.

Serialization Pattern:
- All serializable classes implement to_dict() and from_dict()
- Required fields use direct dict access (raises KeyError if missing)
- Optional/default fields use .get() with defaults
- Nested objects delegate to their own from_dict()
- datetime fields use .isoformat() / datetime.fromisoformat()
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Optional, List, Protocol, TypeVar, runtime_checkable
from datetime import datetime
import uuid

T = TypeVar("T")


@runtime_checkable
class Serializable(Protocol):
    """Protocol for JSON-serializable dataclasses"""

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage"""
        ...

    @classmethod
    def from_dict(cls, data: dict) -> "Serializable":
        """Deserialize from dictionary"""
        ...


@dataclass
class CellChange:
    """Represents a single cell value change"""

    table_name: str
    table_address: str  # Hex address
    row: int
    col: int
    old_value: float  # Display value
    new_value: float  # Display value
    old_raw: float  # Raw binary value
    new_raw: float  # Raw binary value
    table_key: object = (
        None  # TableKey namedtuple for multi-ROM isolation (not serialized)
    )

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage"""
        return {
            "table_name": self.table_name,
            "table_address": self.table_address,
            "row": self.row,
            "col": self.col,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_raw": self.old_raw,
            "new_raw": self.new_raw,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CellChange":
        """Deserialize from dictionary"""
        return cls(
            table_name=data["table_name"],
            table_address=data["table_address"],
            row=data["row"],
            col=data["col"],
            old_value=data["old_value"],
            new_value=data["new_value"],
            old_raw=data["old_raw"],
            new_raw=data["new_raw"],
        )


@dataclass
class TableChanges:
    """Groups all changes for a single table"""

    table_name: str
    table_address: str
    cell_changes: List[CellChange] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "table_name": self.table_name,
            "table_address": self.table_address,
            "cell_changes": [c.to_dict() for c in self.cell_changes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TableChanges":
        return cls(
            table_name=data["table_name"],
            table_address=data["table_address"],
            cell_changes=[CellChange.from_dict(c) for c in data["cell_changes"]],
        )


@dataclass
class Commit:
    """Represents a single commit (save point)"""

    id: str
    version: int  # Linear version number (0, 1, 2...)
    parent_id: Optional[str]
    message: str
    timestamp: datetime
    author: str
    tables_modified: List[str]
    changes: List[TableChanges]
    has_snapshot: bool = False
    snapshot_filename: Optional[str] = None  # Custom filename like v1_LF9VEB_stage1.bin
    deleted: bool = False  # Soft-deleted version (moved to _trash/)

    @classmethod
    def create(
        cls,
        message: str,
        changes: List[TableChanges],
        version: int = 0,
        parent_id: Optional[str] = None,
        author: Optional[str] = None,
        snapshot_filename: Optional[str] = None,
    ) -> "Commit":
        """Factory method to create a new commit"""
        if author is None:
            try:
                author = os.getlogin()
            except OSError:
                author = "User"
        return cls(
            id=uuid.uuid4().hex,
            version=version,
            parent_id=parent_id,
            message=message,
            timestamp=datetime.now(),
            author=author,
            tables_modified=list(set(tc.table_name for tc in changes)),
            changes=changes,
            has_snapshot=snapshot_filename is not None,
            snapshot_filename=snapshot_filename,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "parent_id": self.parent_id,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "author": self.author,
            "tables_modified": self.tables_modified,
            "changes": [c.to_dict() for c in self.changes],
            "has_snapshot": self.has_snapshot,
            "snapshot_filename": self.snapshot_filename,
            "deleted": self.deleted,
        }

    @classmethod
    def from_dict(cls, data: dict, fallback_version: int = 0) -> "Commit":
        """
        Deserialize from dictionary.

        Args:
            data: Dictionary containing commit data
            fallback_version: Version to use if not present (for backward compatibility)
        """
        return cls(
            id=data["id"],
            version=data.get("version", fallback_version),  # Backward compatibility
            parent_id=data["parent_id"],
            message=data["message"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            author=data["author"],
            tables_modified=data["tables_modified"],
            changes=[TableChanges.from_dict(c) for c in data["changes"]],
            has_snapshot=data.get("has_snapshot", False),
            snapshot_filename=data.get("snapshot_filename"),  # Backward compatibility
            deleted=data.get("deleted", False),  # Backward compatibility
        )


@dataclass
class AxisChange:
    """Represents a single axis value change"""

    table_name: str
    table_address: str  # Hex address
    axis_type: str  # 'x_axis' or 'y_axis'
    index: int  # Index in the axis array
    old_value: float  # Display value
    new_value: float  # Display value
    old_raw: float  # Raw binary value
    new_raw: float  # Raw binary value
    table_key: object = (
        None  # TableKey namedtuple for multi-ROM isolation (not serialized)
    )

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage"""
        return {
            "table_name": self.table_name,
            "table_address": self.table_address,
            "axis_type": self.axis_type,
            "index": self.index,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_raw": self.old_raw,
            "new_raw": self.new_raw,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AxisChange":
        """Deserialize from dictionary"""
        return cls(
            table_name=data["table_name"],
            table_address=data["table_address"],
            axis_type=data["axis_type"],
            index=data["index"],
            old_value=data["old_value"],
            new_value=data["new_value"],
            old_raw=data["old_raw"],
            new_raw=data["new_raw"],
        )


@dataclass
class OriginalRomInfo:
    """Metadata about the original ROM file"""

    filename: str
    size: int
    checksum_sha256: str
    rom_id: str
    definition_xmlid: str
    make: str
    model: str

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size": self.size,
            "checksum_sha256": self.checksum_sha256,
            "rom_id": self.rom_id,
            "definition_xmlid": self.definition_xmlid,
            "make": self.make,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OriginalRomInfo":
        return cls(
            filename=data["filename"],
            size=data["size"],
            checksum_sha256=data["checksum_sha256"],
            rom_id=data["rom_id"],
            definition_xmlid=data["definition_xmlid"],
            make=data["make"],
            model=data["model"],
        )


@dataclass
class Project:
    """Represents a ROM editing project"""

    version: str
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    original_rom: OriginalRomInfo
    working_rom: str
    head_commit_id: Optional[str]
    project_path: str  # Path to project folder
    head_version: int = 0  # Current version number

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "original_rom": self.original_rom.to_dict(),
            "working_rom": self.working_rom,
            "head_commit_id": self.head_commit_id,
            "head_version": self.head_version,
        }

    @classmethod
    def from_dict(cls, data: dict, project_path: str) -> "Project":
        return cls(
            version=data["version"],
            name=data["name"],
            description=data["description"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            original_rom=OriginalRomInfo.from_dict(data["original_rom"]),
            working_rom=data["working_rom"],
            head_commit_id=data.get("head_commit_id"),
            project_path=project_path,
            head_version=data.get("head_version", 0),  # Backward compatibility
        )

    @property
    def original_rom_path(self) -> str:
        """Full path to original ROM file"""
        return str(Path(self.project_path) / self.original_rom.filename)

    @property
    def working_rom_path(self) -> str:
        """Full path to working ROM file"""
        return str(Path(self.project_path) / self.working_rom)
