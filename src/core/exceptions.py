"""
Custom exceptions for NC Flash

Provides specific exception types for better error handling and debugging.
"""


class RomEditorError(Exception):
    """Base exception for all ROM Editor errors"""

    pass


# Definition/Parsing Errors
class DefinitionError(RomEditorError):
    """Base exception for ROM definition errors"""

    pass


class DefinitionNotFoundError(DefinitionError):
    """Raised when a ROM definition file cannot be found"""

    pass


class DefinitionParseError(DefinitionError):
    """Raised when a ROM definition file cannot be parsed"""

    pass


class InvalidDefinitionError(DefinitionError):
    """Raised when a ROM definition is malformed or invalid"""

    pass


class ScalingNotFoundError(DefinitionError):
    """Raised when a required scaling definition is not found"""

    pass


# ROM File Errors
class RomFileError(RomEditorError):
    """Base exception for ROM file errors"""

    pass


class RomFileNotFoundError(RomFileError):
    """Raised when a ROM file cannot be found"""

    pass


class RomIdMismatchError(RomFileError):
    """Raised when ROM ID doesn't match expected value"""

    pass


class InvalidRomFileError(RomFileError):
    """Raised when a ROM file is invalid or corrupted"""

    pass


class RomReadError(RomFileError):
    """Raised when reading data from ROM fails"""

    pass


class RomWriteError(RomFileError):
    """Raised when writing data to ROM fails"""

    pass


# ROM Detection Errors
class DetectionError(RomEditorError):
    """Base exception for ROM detection errors"""

    pass


class NoMatchingDefinitionError(DetectionError):
    """Raised when no matching ROM definition is found"""

    pass


class MetadataDirectoryError(DetectionError):
    """Raised when metadata directory is missing or invalid"""

    pass


# Data Conversion Errors
class ConversionError(RomEditorError):
    """Base exception for data conversion errors"""

    pass


class ScalingConversionError(ConversionError):
    """Raised when scaling conversion fails"""

    pass


class AddressConversionError(ConversionError):
    """Raised when hex address conversion fails"""

    pass


# Table Errors
class TableError(RomEditorError):
    """Base exception for table-related errors"""

    pass


class TableNotFoundError(TableError):
    """Raised when a table cannot be found"""

    pass


class InvalidTableDataError(TableError):
    """Raised when table data is invalid"""

    pass


# Project Errors
class ProjectError(RomEditorError):
    """Base exception for project-related errors"""

    pass


class ProjectNotFoundError(ProjectError):
    """Raised when a project folder or file cannot be found"""

    pass


class ProjectCorruptError(ProjectError):
    """Raised when project data is corrupted or invalid"""

    pass


class ProjectSaveError(ProjectError):
    """Raised when saving a project fails"""

    pass
