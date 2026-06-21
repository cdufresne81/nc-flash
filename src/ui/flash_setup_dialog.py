"""
Flash Setup Dialog

Pre-flash dialog that reads ECU info (VIN, flash counter, DTCs),
lets the user choose Full vs Dynamic flash, shows safety warnings,
and proceeds to the flash progress dialog.
"""

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QLabel,
    QRadioButton,
    QPushButton,
    QButtonGroup,
)
from PySide6.QtCore import QThread, Signal, QObject

from src.ecu.constants import ARCHIVE_FILENAME

logger = logging.getLogger(__name__)


class _ECUInfoWorker(QObject):
    """Background worker to read ECU info without blocking the UI."""

    finished = Signal(dict)  # {vin, rom_id, dtc_count, dtcs_text}
    error = Signal(str)

    def __init__(self, dll_path: str, session_uds=None):
        super().__init__()
        self._dll_path = dll_path
        self._session_uds = session_uds

    def run(self):
        try:
            if self._session_uds:
                uds = self._session_uds
                vin_data = uds.read_vin_block()
                rom_id = uds.read_rom_id()
                dtcs = []
                try:
                    dtcs = uds.read_dtc_status()
                except Exception:
                    logger.info("DTC read failed in ECU info worker (non-critical)")
            else:
                from src.ecu.j2534 import J2534Device, setup_isotp_flow_control
                from src.ecu.protocol import UDSConnection
                from src.ecu.transport import J2534Transport
                from src.ecu.constants import (
                    J2534_PROTOCOL_ISO15765,
                    CAN_BAUDRATE,
                    ISO15765_BS,
                    ISO15765_STMIN,
                )

                with J2534Device(self._dll_path) as device:
                    channel_id = device.connect(
                        J2534_PROTOCOL_ISO15765, 0, CAN_BAUDRATE
                    )
                    device.set_config(channel_id, {ISO15765_BS: 0, ISO15765_STMIN: 0})
                    setup_isotp_flow_control(device, channel_id)

                    uds = UDSConnection(J2534Transport(device, channel_id))
                    uds.tester_present()

                    vin_data = uds.read_vin_block()
                    rom_id = uds.read_rom_id()
                    dtcs = []
                    try:
                        dtcs = uds.read_dtc_status()
                    except Exception:
                        logger.info("DTC read failed in ECU info worker (non-critical)")

            vin_str = (
                vin_data.decode("ascii", errors="replace").rstrip("\x00")
                if vin_data
                else "N/A"
            )

            dtcs_text = ""
            if dtcs:
                dtcs_text = "\n".join(f"{d.formatted}: {d.description}" for d in dtcs)

            self.finished.emit(
                {
                    "vin": vin_str,
                    "rom_id": rom_id or "N/A",
                    "dtc_count": len(dtcs),
                    "dtcs_text": dtcs_text,
                }
            )
        except Exception as e:
            self.error.emit(str(e))


class FlashSetupDialog(QDialog):
    """Pre-flash setup dialog with ECU info and flash mode selection."""

    def __init__(
        self,
        file_name: str,
        rom_path: Path,
        dll_path: str,
        parent=None,
        session_uds=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Flash ROM to ECU")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._rom_path = rom_path
        self._dll_path = dll_path
        self._archive_path = str(rom_path.parent / ARCHIVE_FILENAME)
        self._archive_exists = Path(self._archive_path).is_file()
        self._ecu_connected = False

        # Result: "full", "dynamic", or None (cancelled)
        self.selected_mode = None

        layout = QVBoxLayout(self)

        # ROM info
        rom_label = QLabel(f"ROM: <b>{file_name}</b>")
        layout.addWidget(rom_label)

        layout.addSpacing(8)

        # ECU Info group
        ecu_group = QGroupBox("ECU Info")
        ecu_layout = QFormLayout()
        ecu_group.setLayout(ecu_layout)

        self._vin_label = QLabel("Connecting...")
        self._vin_label.setStyleSheet("color: gray; font-style: italic;")
        ecu_layout.addRow("VIN:", self._vin_label)

        self._rom_id_label = QLabel("—")
        ecu_layout.addRow("ROM ID:", self._rom_id_label)

        self._dtc_label = QLabel("—")
        ecu_layout.addRow("DTCs:", self._dtc_label)

        layout.addWidget(ecu_group)

        layout.addSpacing(8)

        # Flash mode selection
        mode_group = QGroupBox("Flash Mode")
        mode_layout = QVBoxLayout()
        mode_group.setLayout(mode_layout)

        self._mode_group = QButtonGroup(self)

        self._dynamic_radio = QRadioButton(
            "Dynamic Flash  (faster — only changed blocks)"
        )
        self._full_radio = QRadioButton(
            "Full Flash  (complete reflash from offset 0x2000)"
        )

        self._mode_group.addButton(self._dynamic_radio, 0)
        self._mode_group.addButton(self._full_radio, 1)

        mode_layout.addWidget(self._dynamic_radio)

        if not self._archive_exists:
            self._dynamic_radio.setEnabled(False)
            no_archive_label = QLabel(
                "No archive found — full flash required for first flash"
            )
            no_archive_label.setStyleSheet(
                "color: #cc8800; font-size: 10px; margin-left: 20px;"
            )
            mode_layout.addWidget(no_archive_label)
            self._full_radio.setChecked(True)
        else:
            self._dynamic_radio.setChecked(True)

        mode_layout.addWidget(self._full_radio)
        layout.addWidget(mode_group)

        layout.addSpacing(8)

        # Safety warnings
        warn_group = QGroupBox("Safety")
        warn_layout = QVBoxLayout()
        warn_group.setLayout(warn_layout)

        warnings = [
            "Engine must be OFF — ignition key in ON position only",
            "Ensure car battery is healthy and fully charged",
            "Do NOT disconnect the OBD-II cable during the flash",
            "Do NOT interrupt the flashing process once started",
        ]
        for w in warnings:
            lbl = QLabel(f"\u26a0  {w}")
            lbl.setStyleSheet("font-size: 11px;")
            lbl.setWordWrap(True)
            warn_layout.addWidget(lbl)

        layout.addWidget(warn_group)

        layout.addSpacing(12)

        # Buttons
        btn_layout = QHBoxLayout()
        layout.addLayout(btn_layout)
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self._flash_btn = QPushButton("Flash")
        self._flash_btn.setDefault(True)
        self._flash_btn.setEnabled(False)
        self._flash_btn.setMinimumWidth(100)
        self._flash_btn.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 6px 16px; }"
        )
        self._flash_btn.clicked.connect(self._on_flash_clicked)
        btn_layout.addWidget(self._flash_btn)

        # Start ECU info read in background
        self._thread = QThread()
        self._worker = _ECUInfoWorker(dll_path, session_uds=session_uds)
        self._worker.moveToThread(self._thread)
        self._worker.finished.connect(self._on_ecu_info_loaded)
        self._worker.error.connect(self._on_ecu_info_error)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_ecu_info_loaded(self, info: dict):
        self._ecu_connected = True
        self._vin_label.setText(info["vin"])
        self._vin_label.setStyleSheet("")
        self._rom_id_label.setText(info["rom_id"])

        dtc_count = info["dtc_count"]
        if dtc_count == 0:
            self._dtc_label.setText("0 stored")
            self._dtc_label.setStyleSheet("color: #44aa44;")
        else:
            self._dtc_label.setText(f"{dtc_count} stored")
            self._dtc_label.setStyleSheet("color: #cc8800;")
            if info["dtcs_text"]:
                self._dtc_label.setToolTip(info["dtcs_text"])

        self._flash_btn.setEnabled(True)

    def _on_ecu_info_error(self, msg: str):
        self._vin_label.setText("Connection failed")
        self._vin_label.setStyleSheet("color: #cc4444;")
        self._rom_id_label.setText("—")
        self._dtc_label.setText("—")
        logger.warning(f"ECU info read failed: {msg}")
        # Still allow flash — user might want to try anyway
        self._flash_btn.setEnabled(True)

    def _on_flash_clicked(self):
        if self._dynamic_radio.isChecked():
            self.selected_mode = "dynamic"
        else:
            self.selected_mode = "full"
        self.accept()

    def closeEvent(self, event):
        if hasattr(self, "_thread") and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
