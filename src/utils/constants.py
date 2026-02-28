"""
Application Constants

Centralized location for all magic numbers and configuration constants.
"""

# Application metadata
APP_NAME = "NC ROM Editor"
APP_VERSION = "1.2.0"
APP_VERSION_STRING = f"v{APP_VERSION}"
APP_DESCRIPTION = "An open-source ROM editor for NC Miata ECUs"

# Main window settings
MAIN_WINDOW_X = 100
MAIN_WINDOW_Y = 100
MAIN_WINDOW_WIDTH = 1400
MAIN_WINDOW_HEIGHT = 900
MAIN_SPLITTER_LEFT = 420  # Table browser width (30%)
MAIN_SPLITTER_RIGHT = 980  # Activity log width (70%)

# Table browser settings
TABLE_BROWSER_COLUMN_WIDTH = 300  # Width of name column

# Log console settings
LOG_CONSOLE_MAX_LINES = 1000  # Maximum lines kept in console
LOG_CONSOLE_CLEAR_BUTTON_WIDTH = 80
LOG_CONSOLE_FONT_FAMILIES = ("Consolas", "Courier New", "DejaVu Sans Mono", "Monospace")
LOG_CONSOLE_FONT_SIZE = 9

# Logging configuration
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_FILE_BACKUP_COUNT = 5

# Settings
MAX_RECENT_FILES = 10

# Table viewer window
TABLE_VIEWER_DEFAULT_WIDTH = 800
TABLE_VIEWER_DEFAULT_HEIGHT = 600
