"""
Flash Mixin for MainWindow

Handles ECU session management and the Patch ROM dialog.

This is a mixin class — it has no __init__ and relies on MainWindow providing:
- self.settings (AppSettings instance)
- self.statusBar() method
"""

import logging

from src.ecu.constants import DEFAULT_J2534_DLL
from src.ecu.session import ECUSession

logger = logging.getLogger(__name__)


class FlashMixin:
    """Mixin providing ECU flash operations for MainWindow."""

    _ecu_session: ECUSession | None = None

    def _get_j2534_dll_path(self) -> str:
        """Get J2534 DLL path: settings override, or default (op20pt32.dll)."""
        return self.settings.get_j2534_dll_path() or DEFAULT_J2534_DLL

    # --- ECU Session Management ---

    def _on_ecu_connect(self):
        """ECU > Connect menu action."""
        if self._ecu_session and self._ecu_session.is_connected:
            return

        dll_path = self._get_j2534_dll_path()
        self._ecu_session = ECUSession(dll_path, parent=self)
        self._ecu_session.state_changed.connect(self._on_ecu_state_changed)
        self._ecu_session.connection_lost.connect(self._on_ecu_connection_lost)
        self._ecu_session.connect_ecu()
        self.statusBar().showMessage("Connecting to ECU...")

    def _on_ecu_disconnect(self):
        """ECU > Disconnect menu action."""
        if self._ecu_session:
            self._ecu_session.disconnect_ecu()

    def _on_ecu_state_changed(self, state: str):
        """Update based on session state (legacy — ECU window handles its own)."""
        pass

    def _on_ecu_connection_lost(self, reason: str):
        """Handle unexpected connection loss."""
        self.statusBar().showMessage(f"ECU connection lost: {reason}", 5000)
        logger.warning("ECU connection lost: %s", reason)

    def _cleanup_ecu_session(self):
        """Clean up ECU session on app exit."""
        if self._ecu_session:
            self._ecu_session.cleanup()
            self._ecu_session = None

    def _get_session_uds(self):
        """Return the session's UDS connection if connected, else None."""
        if self._ecu_session and self._ecu_session.is_connected:
            return self._ecu_session.uds
        return None

    def _on_patch_rom(self):
        """Apply an XOR patch to a stock ROM via the Patch ROM dialog."""
        from src.ui.patch_dialog import PatchRomDialog

        dlg = PatchRomDialog(parent=self)
        dlg.exec()
