"""
Logging configuration for NC ROM Editor

Provides centralized logging setup with file and console handlers.
"""

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from .constants import LOG_FILE_MAX_BYTES, LOG_FILE_BACKUP_COUNT


# Default log format
DEFAULT_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
DETAILED_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    console: bool = True,
    detailed: bool = False
) -> None:
    """
    Configure logging for the application

    Args:
        level: Logging level (e.g., logging.INFO, logging.DEBUG)
        log_file: Path to log file (creates rotating file handler if provided)
        console: Whether to enable console logging
        detailed: Whether to use detailed log format with file/line info
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear any existing handlers
    root_logger.handlers.clear()

    # Choose format
    log_format = DETAILED_FORMAT if detailed else DEFAULT_FORMAT
    formatter = logging.Formatter(log_format)

    # Console handler
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # File handler (rotating)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)


def set_log_level(level: int) -> None:
    """
    Set the logging level for all handlers

    Args:
        level: Logging level (e.g., logging.DEBUG, logging.INFO)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in root_logger.handlers:
        handler.setLevel(level)
