"""Regression tests for B6 — uncaught exceptions reach the session log.

Slot and worker-thread exceptions otherwise bypass the logging diagnostics
(qt_diagnostics only covers Qt C++ messages + native faults). _install_exception_hooks
logs CRITICAL with a traceback and then chains to the previous hook.
"""

import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import main


def test_excepthook_logs_and_chains_to_previous():
    prev_sys = MagicMock()
    prev_thread = MagicMock()
    with (
        patch.object(sys, "excepthook", prev_sys),
        patch.object(threading, "excepthook", prev_thread),
    ):
        main._install_exception_hooks()
        installed = sys.excepthook
        assert installed is not prev_sys  # a new hook was installed

        with patch.object(main.logger, "critical") as crit:
            installed(ValueError, ValueError("boom"), None)

        crit.assert_called_once()
        prev_sys.assert_called_once()  # previous hook still runs


def test_excepthook_does_not_log_keyboard_interrupt_but_still_chains():
    prev_sys = MagicMock()
    with (
        patch.object(sys, "excepthook", prev_sys),
        patch.object(threading, "excepthook", MagicMock()),
    ):
        main._install_exception_hooks()
        installed = sys.excepthook
        with patch.object(main.logger, "critical") as crit:
            installed(KeyboardInterrupt, KeyboardInterrupt(), None)
        crit.assert_not_called()
        prev_sys.assert_called_once()


def test_threading_excepthook_logs_and_chains():
    prev_thread = MagicMock()
    with (
        patch.object(sys, "excepthook", MagicMock()),
        patch.object(threading, "excepthook", prev_thread),
    ):
        main._install_exception_hooks()
        installed = threading.excepthook
        args = SimpleNamespace(
            exc_type=RuntimeError,
            exc_value=RuntimeError("thread boom"),
            exc_traceback=None,
            thread=SimpleNamespace(name="worker-1"),
        )
        with patch.object(main.logger, "critical") as crit:
            installed(args)
        crit.assert_called_once()
        prev_thread.assert_called_once_with(args)
