"""
Log Console Widget

Displays application logs and messages in a console-like interface.
Similar to ECUFlash's message window.
"""

import logging
from datetime import datetime
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QTextEdit,
    QLabel,
    QPushButton,
    QHBoxLayout
)
from PySide6.QtGui import QTextCursor, QFont, QColor
from PySide6.QtCore import Qt, Signal, QObject


class QtLogHandler(logging.Handler, QObject):
    """
    Custom logging handler that emits Qt signals for thread-safe log display
    """
    log_message = Signal(str, int)  # message, level

    def __init__(self):
        logging.Handler.__init__(self)
        QObject.__init__(self)

    def emit(self, record):
        """Emit log record as Qt signal"""
        msg = self.format(record)
        self.log_message.emit(msg, record.levelno)


class LogConsole(QWidget):
    """
    Console widget for displaying application logs and messages
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.max_lines = 1000  # Maximum lines to keep in console
        self.init_ui()
        self.setup_logging()

    def init_ui(self):
        """Initialize the user interface"""
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Header with title and controls
        header_layout = QHBoxLayout()

        # Title label
        title_label = QLabel("Activity Log")
        title_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        header_layout.addWidget(title_label)

        header_layout.addStretch()

        # Clear button
        clear_button = QPushButton("Clear")
        clear_button.setMaximumWidth(80)
        clear_button.clicked.connect(self.clear)
        header_layout.addWidget(clear_button)

        layout.addLayout(header_layout)

        # Console text area
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QTextEdit.NoWrap)

        # Use monospace font for console-like appearance
        font = QFont("Courier New", 9)
        self.console.setFont(font)

        # Style the console
        self.console.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
            }
        """)

        layout.addWidget(self.console)

        # Status bar at bottom
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(
            "padding: 4px; background-color: #2d2d2d; color: #888;"
        )
        layout.addWidget(self.status_label)

    def setup_logging(self):
        """Setup custom logging handler to display logs in console"""
        self.log_handler = QtLogHandler()
        self.log_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                            datefmt='%H:%M:%S')
        )
        self.log_handler.log_message.connect(self.append_log)

        # Add handler to root logger
        logging.getLogger().addHandler(self.log_handler)

    def append_log(self, message: str, level: int):
        """
        Append log message to console with appropriate color

        Args:
            message: Log message text
            level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        """
        # Color coding based on log level
        colors = {
            logging.DEBUG: "#808080",      # Gray
            logging.INFO: "#4ec9b0",       # Cyan
            logging.WARNING: "#dcdcaa",    # Yellow
            logging.ERROR: "#f48771",      # Red
            logging.CRITICAL: "#ff0000"    # Bright Red
        }

        color = colors.get(level, "#d4d4d4")  # Default white

        # Format with HTML for colors
        html_message = f'<span style="color: {color};">{message}</span>'

        # Append to console
        self.console.append(html_message)

        # Auto-scroll to bottom
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.console.setTextCursor(cursor)

        # Limit number of lines
        self._limit_lines()

    def _limit_lines(self):
        """Limit console to max_lines by removing old lines"""
        document = self.console.document()
        if document.blockCount() > self.max_lines:
            cursor = QTextCursor(document.findBlockByLineNumber(0))
            cursor.select(QTextCursor.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()  # Remove the newline

    def log_info(self, message: str):
        """Log an info message"""
        logging.info(message)

    def log_warning(self, message: str):
        """Log a warning message"""
        logging.warning(message)

    def log_error(self, message: str):
        """Log an error message"""
        logging.error(message)

    def log_success(self, message: str):
        """Log a success message (info level with custom formatting)"""
        logging.info(f"✓ {message}")

    def clear(self):
        """Clear the console"""
        self.console.clear()
        self.log_info("Console cleared")

    def update_status(self, message: str):
        """Update the status bar at the bottom"""
        self.status_label.setText(message)

    def closeEvent(self, event):
        """Remove log handler when widget is closed"""
        logging.getLogger().removeHandler(self.log_handler)
        event.accept()
