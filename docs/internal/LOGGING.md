# Logging and Error Handling

NC Flash uses Python's built-in `logging` module for comprehensive logging and custom exception classes for better error handling and debugging.

## Logging Configuration

### Default Setup

Logging is initialized automatically when the application starts (in `main.py:main()`):

- **Log Level**: `INFO` by default
- **Console Logging**: Enabled (prints to terminal/console)
- **File Logging**: Enabled by default
- **Log File Location**: `~/.nc-flash/nc-flash.log`
- **Log Format**: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`
- **File Rotation**: Automatic, 10MB per file, keeps 5 backups

### Customizing Logging

You can customize logging by modifying the `setup_logging()` call in `main.py`:

```python
from src.utils.logging_config import setup_logging
import logging

# Example 1: Debug level with detailed format
setup_logging(
    level=logging.DEBUG,
    log_file="/path/to/custom/logfile.log",
    console=True,
    detailed=True  # Includes file name and line numbers
)

# Example 2: Console only, no file
setup_logging(
    level=logging.INFO,
    log_file=None,  # Disable file logging
    console=True,
    detailed=False
)

# Example 3: File only, no console
setup_logging(
    level=logging.WARNING,
    log_file="errors.log",
    console=False,
    detailed=True
)
```

### Log Levels

The application uses standard Python logging levels:

| Level | Purpose | Example Use Case |
|-------|---------|------------------|
| `DEBUG` | Detailed diagnostic information | Reading individual table addresses, parsing details |
| `INFO` | General informational messages | ROM opened, table loaded, file saved |
| `WARNING` | Warning messages (non-critical issues) | ROM ID mismatch, missing optional data |
| `ERROR` | Error messages (operation failed) | File not found, parsing error, invalid data |
| `CRITICAL` | Critical errors (application may crash) | Not currently used |

### Changing Log Level at Runtime

```python
from src.utils.logging_config import set_log_level
import logging

# Switch to debug mode
set_log_level(logging.DEBUG)

# Switch back to info mode
set_log_level(logging.INFO)
```

## Using Logging in Code

### Getting a Logger

Each module should get its own logger using `__name__`:

```python
import logging

logger = logging.getLogger(__name__)
```

### Logging Messages

```python
# Info - normal operations
logger.info(f"Loading ROM definition from {xml_path}")
logger.info(f"Successfully parsed {len(tables)} tables")

# Debug - detailed diagnostic info
logger.debug(f"Reading table data: {table.name}")
logger.debug(f"Parsed scaling: {scaling_name}")

# Warning - non-critical issues
logger.warning(f"ROM ID mismatch - Expected: {expected}, Found: {actual}")
logger.warning(f"Skipping table with unknown type: {type_str}")

# Error - operation failures
logger.error(f"Failed to load ROM definition: {e}")
logger.error(f"Error unpacking data at address {hex(address)}: {e}")
```

## Custom Exceptions

The application uses custom exception classes for better error handling. All exceptions inherit from `RomEditorError`.

### Exception Hierarchy

```
RomEditorError (base)
├── DefinitionError
│   ├── DefinitionNotFoundError
│   ├── DefinitionParseError
│   ├── InvalidDefinitionError
│   └── ScalingNotFoundError
├── RomFileError
│   ├── RomFileNotFoundError
│   ├── RomIdMismatchError
│   ├── InvalidRomFileError
│   ├── RomReadError
│   └── RomWriteError
├── DetectionError
│   ├── NoMatchingDefinitionError
│   └── MetadataDirectoryError
├── ConversionError
│   ├── ScalingConversionError
│   └── AddressConversionError
└── TableError
    ├── TableNotFoundError
    └── InvalidTableDataError
```

### Using Custom Exceptions

#### Raising Exceptions

```python
from src.core.exceptions import (
    DefinitionNotFoundError,
    RomReadError,
    ScalingNotFoundError
)

# Check file exists
if not self.xml_path.exists():
    logger.error(f"Definition file not found: {xml_path}")
    raise DefinitionNotFoundError(f"Definition file not found: {xml_path}")

# Check scaling exists
scaling = self.definition.get_scaling(table.scaling)
if not scaling:
    logger.error(f"Scaling '{table.scaling}' not found")
    raise ScalingNotFoundError(f"Scaling '{table.scaling}' not found")

# Handle read errors
try:
    values = struct.unpack(format_string, data_bytes)
except struct.error as e:
    logger.error(f"Error unpacking data: {e}")
    raise RomReadError(f"Failed to unpack data: {e}")
```

#### Catching Exceptions

```python
from src.core.exceptions import DefinitionError, RomFileError

# Catch specific exception types
try:
    definition = load_definition(xml_path)
except DefinitionNotFoundError as e:
    logger.error(f"Definition file not found: {e}")
    # Handle missing file
except DefinitionParseError as e:
    logger.error(f"Failed to parse definition: {e}")
    # Handle parse error

# Catch base exception types
try:
    rom_reader = RomReader(rom_path, definition)
except RomFileError as e:
    logger.error(f"ROM file error: {e}")
    # Handle any ROM file error
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    # Handle unexpected errors
```

## Best Practices

### 1. Always Log Errors Before Raising Exceptions

```python
# Good
logger.error(f"Failed to read ROM file: {e}")
raise RomReadError(f"Failed to read ROM file: {e}")

# Bad (missing log)
raise RomReadError(f"Failed to read ROM file: {e}")
```

### 2. Use Specific Exception Types

```python
# Good - specific exception type
if not scaling:
    raise ScalingNotFoundError(f"Scaling '{name}' not found")

# Bad - generic exception
if not scaling:
    raise ValueError(f"Scaling '{name}' not found")
```

### 3. Include Context in Error Messages

```python
# Good - provides context
raise RomReadError(f"Failed to unpack data at address {hex(address)}: {e}")

# Bad - vague message
raise RomReadError("Read error")
```

### 4. Use Appropriate Log Levels

```python
# Good - success is INFO, errors are ERROR
logger.info(f"Successfully loaded ROM: {file_name}")
logger.error(f"Failed to load ROM: {e}")

# Bad - everything at DEBUG
logger.debug(f"Successfully loaded ROM: {file_name}")
logger.debug(f"Failed to load ROM: {e}")
```

### 5. Don't Log and Raise for the Same Error

```python
# Good - log once when catching
try:
    load_rom()
except RomFileError as e:
    logger.error(f"Failed to load ROM: {e}")
    # Show to user or handle

# Bad - logs twice (once when raising, once when catching)
```

## Example: Complete Error Handling Flow

```python
# Module: definition_parser.py
import logging
from src.core.exceptions import DefinitionNotFoundError, DefinitionParseError

logger = logging.getLogger(__name__)

def parse(self):
    logger.info(f"Parsing ROM definition from {self.xml_path}")

    try:
        tree = etree.parse(str(self.xml_path))
        root = tree.getroot()
    except etree.XMLSyntaxError as e:
        logger.error(f"XML syntax error: {e}")
        raise DefinitionParseError(f"Failed to parse XML file: {e}")

    logger.info(f"Successfully parsed ROM definition")
    return definition

# Module: main.py
from src.core.exceptions import DefinitionError

def load_definition(self, definition_path):
    try:
        logger.info(f"Loading ROM definition from {definition_path}")
        self.rom_definition = load_definition(definition_path)
        logger.info(f"Loaded definition: {self.rom_definition.romid.xmlid}")
    except DefinitionError as e:
        logger.error(f"Failed to load ROM definition: {e}")
        QMessageBox.critical(self, "Error", f"Failed to load ROM definition:\n{e}")
```

## Log File Location

- **Linux/macOS**: `~/.nc-flash/nc-flash.log`
- **Windows**: `C:\Users\<username>\.nc-flash\nc-flash.log`

The log directory is created automatically if it doesn't exist.
