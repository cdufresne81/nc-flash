"""Join the WiCAN's own Wi-Fi when the adapter can't be reached (GitHub issue 129).

Owned by the ECU Programming window. On Connect, if the WiCAN doesn't answer
on the current network but its access point is in range, ask the user, switch
the PC to it, and connect to the AP's fixed address for this session only. On
Disconnect or window close, offer the previous network back.

Never switches while anything talks to the WiCAN: only before a session opens,
and the rejoin only once it is closed. Design: ``docs/internal/WICAN_AP_AUTOSWITCH.md``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
)

from src.utils import wifi_switch
from src.utils.wifi_switch import WICAN_AP_HOST, WifiError, WifiLocationOff

logger = logging.getLogger(__name__)

# The WiCAN's web server; answers on every firmware, whichever mode it's in.
WICAN_HTTP_PORT = 80
REACH_TIMEOUT_S = 1.5
SCAN_WAIT_S = 5.0
JOIN_TIMEOUT_S = 25.0
AP_ANSWER_TIMEOUT_S = 15.0


class WiCANApSwitch:
    """Switches the PC's Wi-Fi to the WiCAN AP and back, always asking first."""

    def __init__(
        self,
        parent,
        settings,
        backend_factory: Callable = wifi_switch.get_backend,
        reachable: Callable[[str, int, float], bool] = wifi_switch.tcp_reachable,
    ):
        self._parent = parent
        self._settings = settings
        self._backend_factory = backend_factory
        self._reachable = reachable

    # ------------------------------------------------------------------ #
    # Connect
    # ------------------------------------------------------------------ #

    def reach(self, host: str, may_switch: bool = True) -> str:
        """The host to connect to: ``host`` when it answers, else the AP's.

        Returns ``host`` unchanged whenever nothing better is found or the user
        declines, so the connect then fails exactly as it did before.
        ``may_switch`` False (something else is using the network) still allows
        using the AP address when the PC is already on the WiCAN's Wi-Fi.
        """
        if self._reachable(host, WICAN_HTTP_PORT, REACH_TIMEOUT_S):
            return host
        backend = self._backend_factory()
        if backend is None:
            return host
        saved_ssid = self._settings.get_wican_ap_ssid()
        try:
            current = backend.current()
        except WifiError as exc:
            logger.debug("Can't read the current Wi-Fi: %s", exc)
            return host
        if current and wifi_switch.is_wican_ap(current.ssid, saved_ssid):
            logger.info(
                "On the WiCAN's Wi-Fi (%s): connecting to %s",
                current.ssid,
                WICAN_AP_HOST,
            )
            return WICAN_AP_HOST
        if not may_switch or self._settings.get_wican_ap_switch() == "off":
            return host

        network = self._find_ap(backend, saved_ssid)
        if network is None:
            return host
        leaving = (
            f"\n\nThis PC leaves {current.ssid} and has no internet while joined."
            if current
            else ""
        )
        reply = QMessageBox.question(
            self._parent,
            "WiCAN Not Found",
            f"The WiCAN doesn't answer at {host}, but its Wi-Fi "
            f"{network.ssid} is in range.\n\n"
            f"Switch this PC to {network.ssid} and connect?{leaving}\n\n"
            "NC Flash offers your previous network back when you disconnect.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return host
        if self._join(backend, network, current):
            return WICAN_AP_HOST
        return host

    def _find_ap(self, backend, saved_ssid: str) -> Optional[wifi_switch.WifiNetwork]:
        """The WiCAN AP if it's in range, scanning for a few seconds if needed."""
        try:
            found = wifi_switch.find_wican_ap(backend.visible(), saved_ssid)
            if found is not None:
                return found
            backend.scan()
            box: list = [None]

            def _seen() -> bool:
                box[0] = wifi_switch.find_wican_ap(backend.visible(), saved_ssid)
                return box[0] is not None

            with _Busy(self._parent, "Looking for the WiCAN's Wi-Fi…") as busy:
                wifi_switch.wait_until(_seen, SCAN_WAIT_S, busy.tick, 0.5)
            return box[0]
        except WifiLocationOff:
            QMessageBox.information(
                self._parent,
                "Wi-Fi Scan Blocked",
                "Windows didn't let NC Flash look for the WiCAN's Wi-Fi.\n\n"
                "Turn on Settings > Privacy & security > Location > "
                '"Let desktop apps access your location", or join the '
                "WiCAN's Wi-Fi from the Windows network menu.",
            )
        except WifiError as exc:
            logger.warning("Looking for the WiCAN's Wi-Fi failed: %s", exc)
        return None

    def _join(self, backend, network, current) -> bool:
        """Join ``network`` and wait until the WiCAN answers on it."""
        profile = network.profile
        created = False
        switched = False
        try:
            if not profile:
                profile = self._create_profile(backend, network)
                if not profile:
                    return False
                created = True
            previous = current.profile if current else ""
            # Saved before switching, so the rejoin offer survives a crash.
            self._settings.set_wican_ap_prev_profile(previous)
            logger.info("Switching Wi-Fi to %s", network.ssid)
            switched = True
            backend.connect(profile)

            def _joined() -> bool:
                now = backend.current()
                return bool(now and now.ssid == network.ssid)

            def _answers() -> bool:
                return self._reachable(WICAN_AP_HOST, WICAN_HTTP_PORT, 1.0)

            with _Busy(self._parent, f"Joining {network.ssid}…") as busy:
                ok = wifi_switch.wait_until(_joined, JOIN_TIMEOUT_S, busy.tick)
                if ok:
                    busy.set_text(f"Waiting for the WiCAN on {network.ssid}…")
                    ok = wifi_switch.wait_until(
                        _answers, AP_ANSWER_TIMEOUT_S, busy.tick
                    )
        except WifiError as exc:
            logger.warning("Switching to %s failed: %s", network.ssid, exc)
            ok = False

        if ok:
            logger.info(
                "Joined %s; the WiCAN answers at %s", network.ssid, WICAN_AP_HOST
            )
            if not self._settings.get_wican_ap_ssid():
                self._settings.set_wican_ap_ssid(network.ssid)
            return True

        if created:
            # Most likely a wrong password: drop the profile so the next
            # attempt asks again instead of failing the same way.
            backend.delete_profile(profile)
        back = self._rejoin_previous(backend) if switched else ""
        QMessageBox.warning(
            self._parent,
            "Couldn't Join the WiCAN",
            f"Joining {network.ssid} didn't work"
            + (" (wrong password?)" if created else "")
            + f", or the WiCAN didn't answer at {WICAN_AP_HOST}."
            + (f"\n\nThis PC is going back to {back}." if back else ""),
        )
        return False

    def _create_profile(self, backend, network) -> str:
        """Ask for the password and save a Windows profile; "" if cancelled."""
        if network.auth == wifi_switch.AUTH_OTHER:
            QMessageBox.information(
                self._parent,
                "Join the WiCAN Once by Hand",
                f"NC Flash can't set up {network.ssid}'s security by itself.\n\n"
                "Join it once from the Windows network menu; after that NC "
                "Flash can switch to it.",
            )
            return ""
        password = ""
        if network.auth == wifi_switch.AUTH_WPA2_PSK:
            password, ok = QInputDialog.getText(
                self._parent,
                "WiCAN Wi-Fi Password",
                f"Password for {network.ssid}\n"
                "(the WiCAN's factory password is @meatpi#):",
                QLineEdit.Password,
            )
            if not ok or not password:
                return ""
        # Windows keeps the password in its own profile store, not NC Flash.
        return backend.add_profile(network.ssid, network.auth, password)

    # ------------------------------------------------------------------ #
    # Disconnect
    # ------------------------------------------------------------------ #

    def offer_rejoin(self) -> None:
        """If NC Flash switched to the WiCAN's Wi-Fi, offer the old network back."""
        previous = self._settings.get_wican_ap_prev_profile()
        if not previous:
            return
        backend = self._backend_factory()
        try:
            current = backend.current() if backend is not None else None
        except WifiError:
            current = None
        if current is None or not wifi_switch.is_wican_ap(
            current.ssid, self._settings.get_wican_ap_ssid()
        ):
            # The user already moved off the WiCAN; nothing to offer.
            self._settings.set_wican_ap_prev_profile("")
            return
        reply = QMessageBox.question(
            self._parent,
            "Rejoin Your Network?",
            f"This PC is still on the WiCAN's Wi-Fi ({current.ssid}).\n\n"
            f"Switch back to {previous}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self._rejoin_previous(backend)
        else:
            self._settings.set_wican_ap_prev_profile("")

    def _rejoin_previous(self, backend) -> str:
        """Start joining the saved previous network; its name, or "" if none."""
        previous = self._settings.get_wican_ap_prev_profile()
        self._settings.set_wican_ap_prev_profile("")
        if not previous:
            return ""
        try:
            backend.connect(previous)
            logger.info("Switching Wi-Fi back to %s", previous)
            return previous
        except WifiError as exc:
            logger.warning("Switching back to %s failed: %s", previous, exc)
            return ""


class _Busy:
    """A cancellable busy dialog; ``tick`` pumps events and reports cancel."""

    def __init__(self, parent, text: str):
        self._dialog = QProgressDialog(text, "Cancel", 0, 0, parent)
        self._dialog.setWindowTitle("WiCAN Wi-Fi")
        self._dialog.setWindowModality(Qt.WindowModal)
        self._dialog.setMinimumDuration(0)

    def __enter__(self):
        self._dialog.show()
        QApplication.processEvents()
        return self

    def __exit__(self, *exc):
        self._dialog.close()
        self._dialog.deleteLater()

    def set_text(self, text: str) -> None:
        self._dialog.setLabelText(text)

    def tick(self) -> bool:
        QApplication.processEvents()
        return not self._dialog.wasCanceled()
