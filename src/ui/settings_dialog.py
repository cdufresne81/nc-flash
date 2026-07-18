"""
Settings Dialog

Configuration window with tree navigation and search.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QFrame,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..utils.settings import get_settings
from ..utils.colormap import reload_colormap

# ---------------------------------------------------------------------------
# Settings Registry
# ---------------------------------------------------------------------------


@dataclass
class SettingDescriptor:
    """Describes a single user-configurable setting."""

    key: str
    label: str
    description: str
    category: str
    subcategory: str
    widget_type: (
        # path_dir, path_file, spinbox, combobox, checkbox, button, readonly,
        # section (a visual rule + header splitting a page into topic groups;
        # no widget, no getter/setter, excluded from search)
        str
    )
    getter: str
    setter: Optional[str] = None
    widget_options: dict = field(default_factory=dict)
    keywords: list = field(default_factory=list)


SETTINGS_REGISTRY = [
    # -- General > Paths --
    # Ordered: workspace root, then inputs (ROMs, metadata), working (projects),
    # outputs (exports, screenshots)
    SettingDescriptor(
        key="general.paths.workspace_dir",
        label="Workspace Directory",
        description=(
            "Root directory for all user content. Subdirectories (projects, exports, "
            "metadata, etc.) are created automatically. Individual paths below can override."
        ),
        category="General",
        subcategory="Paths",
        widget_type="path_dir",
        getter="get_workspace_directory",
        setter="set_workspace_directory",
        widget_options={"placeholder": "Root folder for all user content"},
        keywords=["workspace", "root", "home", "folder", "base"],
    ),
    SettingDescriptor(
        key="general.paths.roms_dir",
        label="ROMs Directory",
        description="Default folder shown in the ROM open/save dialogs",
        category="General",
        subcategory="Paths",
        widget_type="path_dir",
        getter="get_roms_directory",
        setter="set_roms_directory",
        widget_options={"placeholder": "Folder for ROM binary files"},
        keywords=["rom", "bin", "binary", "open"],
    ),
    SettingDescriptor(
        key="general.paths.metadata_dir",
        label="Metadata Directory",
        description=(
            "Location of ROM metadata XML files (e.g., lf9veb.xml). "
            "These define the table layouts for each calibration ID."
        ),
        category="General",
        subcategory="Paths",
        widget_type="path_dir",
        getter="get_metadata_directory",
        setter="set_metadata_directory",
        widget_options={"placeholder": "Path to ROM metadata XML files"},
        keywords=["metadata", "xml", "rom", "definition", "calibration"],
    ),
    SettingDescriptor(
        key="general.paths.projects_dir",
        label="Projects Directory",
        description="Location where ROM tuning projects are stored",
        category="General",
        subcategory="Paths",
        widget_type="path_dir",
        getter="get_projects_directory",
        setter="set_projects_directory",
        widget_options={"placeholder": "Path to store ROM projects"},
        keywords=["folder", "project", "location"],
    ),
    SettingDescriptor(
        key="general.paths.export_dir",
        label="Export Directory",
        description="Default folder for CSV exports (Ctrl+E)",
        category="General",
        subcategory="Paths",
        widget_type="path_dir",
        getter="get_export_directory",
        setter="set_export_directory",
        widget_options={"placeholder": "Folder for CSV exports"},
        keywords=["csv", "export", "folder"],
    ),
    SettingDescriptor(
        key="general.paths.screenshots_dir",
        label="Screenshots Directory",
        description="Default folder for screenshots",
        category="General",
        subcategory="Paths",
        widget_type="path_dir",
        getter="get_screenshots_directory",
        setter="set_screenshots_directory",
        widget_options={"placeholder": "Folder for screenshots"},
        keywords=["screenshot", "capture", "image", "png"],
    ),
    # -- Appearance > Table Display --
    SettingDescriptor(
        key="appearance.table_display.font_size",
        label="Table font size",
        description=(
            "Font size in pixels for table cell values. "
            "Changes take effect on newly opened tables."
        ),
        category="Appearance",
        subcategory="Table Display",
        widget_type="spinbox",
        getter="get_table_font_size",
        setter="set_table_font_size",
        widget_options={"min": 6, "max": 16, "suffix": " px"},
        keywords=["font", "text", "size", "pixels"],
    ),
    SettingDescriptor(
        key="appearance.table_display.gradient_mode",
        label="Cell gradient coloring",
        description=(
            "How cell background colors are calculated. 'Min/Max' uses the table's "
            "global range; 'Neighbors' uses local surrounding values."
        ),
        category="Appearance",
        subcategory="Table Display",
        widget_type="combobox",
        getter="get_gradient_mode",
        setter="set_gradient_mode",
        widget_options={
            "items": [
                ("Min/Max of table", "minmax"),
                ("Relative to neighbors", "neighbors"),
            ]
        },
        keywords=["color", "gradient", "heat", "map"],
    ),
    # -- Appearance > Color Map --
    SettingDescriptor(
        key="appearance.colormap.path",
        label="Color map file",
        description="256-entry RGB color map file (.map format)",
        category="Appearance",
        subcategory="Color Map",
        widget_type="path_file",
        getter="get_colormap_path",
        setter="set_colormap_path",
        widget_options={
            "filter": "Color Map Files (*.map);;All Files (*)",
            "placeholder": "Path to .map file (or empty for built-in)",
        },
        keywords=["color", "gradient", "palette", "map"],
    ),
    # -- Appearance > Table Browser --
    SettingDescriptor(
        key="appearance.browser.show_type",
        label="Show Type column",
        description="Display the Type column (1D, 2D, 3D) in the table browser sidebar",
        category="Appearance",
        subcategory="Table Browser",
        widget_type="checkbox",
        getter="get_show_type_column",
        setter="set_show_type_column",
        keywords=["column", "browser", "type", "table"],
    ),
    SettingDescriptor(
        key="appearance.browser.show_address",
        label="Show Address column",
        description="Display the hex Address column in the table browser sidebar",
        category="Appearance",
        subcategory="Table Browser",
        widget_type="checkbox",
        getter="get_show_address_column",
        setter="set_show_address_column",
        keywords=["column", "browser", "address", "hex"],
    ),
    # -- Editor > Toggle Display --
    SettingDescriptor(
        key="editor.toggle.dtc_flags",
        label="Use toggle switches for DTC Activation Flags",
        description=(
            "When enabled, DTC Activation Flag tables show an ON/OFF toggle "
            "instead of a numeric cell (0 = OFF, non-zero = ON)"
        ),
        category="Editor",
        subcategory="Toggle Display",
        widget_type="checkbox",
        getter="get_toggle_categories",
        setter="set_toggle_categories",
        keywords=["toggle", "dtc", "switch", "on", "off", "activation"],
    ),
    # -- Editor > Rounding --
    SettingDescriptor(
        key="editor.rounding.auto_round",
        label="Auto-round after interpolation and smoothing",
        description=(
            "When enabled, interpolation and smoothing results are automatically "
            "rounded one decimal level coarser than the table\u2019s display format "
            "(e.g. 12.11 \u2192 12.1 for %0.2f)"
        ),
        category="Editor",
        subcategory="Rounding",
        widget_type="checkbox",
        getter="get_auto_round",
        setter="set_auto_round",
        keywords=["round", "decimal", "interpolation", "smooth", "precision"],
    ),
    # -- Tools > MCP Server --
    SettingDescriptor(
        key="tools.mcp.auto_start",
        label="Start MCP server automatically on app launch",
        description=(
            "Enables AI assistants (Claude, ChatGPT, etc.) to read your open ROMs "
            "via the Model Context Protocol"
        ),
        category="Tools",
        subcategory="MCP Server",
        widget_type="checkbox",
        getter="get_mcp_auto_start",
        setter="set_mcp_auto_start",
        keywords=["mcp", "ai", "server", "claude", "assistant", "model context"],
    ),
    # -- ECU > Adapter --
    SettingDescriptor(
        key="ecu.adapter.kind",
        label="ECU Adapter",
        description=(
            "Which adapter NC Flash uses to talk to the ECU. J2534 (wired, e.g. "
            "Tactrix OpenPort) is the default and recommended for flashing. WiCAN "
            "(wireless WiFi/SLCAN) is opt-in; WiCAN flashing is experimental."
        ),
        category="ECU",
        subcategory="Adapter",
        widget_type="combobox",
        getter="get_ecu_adapter",
        setter="set_ecu_adapter",
        widget_options={
            "items": [("J2534 (wired)", "j2534"), ("WiCAN (WiFi)", "wican")]
        },
        keywords=["adapter", "j2534", "wican", "wifi", "transport", "connection"],
    ),
    # -- ECU > J2534 --
    SettingDescriptor(
        key="ecu.j2534.dll_path",
        label="J2534 DLL override",
        description=(
            "Leave empty for Tactrix OpenPort 2.0 (op20pt32.dll is found automatically). "
            "Only set this if you use a different J2534 adapter."
        ),
        category="ECU",
        subcategory="J2534",
        widget_type="path_file",
        getter="get_j2534_dll_path",
        setter="set_j2534_dll_path",
        widget_options={
            "filter": "DLL Files (*.dll);;All Files (*)",
            "placeholder": "op20pt32.dll (auto-detected)",
        },
        keywords=["j2534", "dll", "passthru", "adapter", "tactrix", "openport"],
    ),
    SettingDescriptor(
        key="ecu.j2534.test_connection",
        label="Test Connection",
        description="Attempts to connect to the J2534 device to verify it is available",
        category="ECU",
        subcategory="J2534",
        widget_type="button",
        getter="",
        setter=None,
        widget_options={
            "text": "Test Connection",
            "callback_name": "_test_j2534_connection",
        },
        keywords=["test", "connection", "j2534", "device"],
    ),
    # -- ECU > WiCAN --
    SettingDescriptor(
        key="ecu.wican.host",
        label="WiCAN Host / IP",
        description="IP address or hostname of the WiCAN adapter (e.g. 192.168.1.169).",
        category="ECU",
        subcategory="WiCAN",
        widget_type="text",
        getter="get_wican_host",
        setter="set_wican_host",
        widget_options={
            "placeholder": "192.168.1.169",
            "scan_button": True,
            "scan_callback": "_scan_wican_devices",
        },
        keywords=["wican", "host", "ip", "address", "wifi", "scan", "discover", "mdns"],
    ),
    SettingDescriptor(
        key="ecu.wican.port",
        label="WiCAN SLCAN Port",
        description="TCP port of the WiCAN SLCAN socket (the PRO is often 35000).",
        category="ECU",
        subcategory="WiCAN",
        widget_type="spinbox",
        getter="get_wican_port",
        setter="set_wican_port",
        widget_options={"min": 1, "max": 65535},
        keywords=["wican", "port", "slcan", "tcp"],
    ),
    SettingDescriptor(
        key="ecu.wican.auto_config",
        label="Auto-configure adapter (SLCAN switch + restore)",
        description=(
            "Switch the WiCAN into SLCAN mode on connect (a ~6 s reboot) and "
            "restore its previous protocol on disconnect. Turn off if you keep "
            "the device permanently in SLCAN mode."
        ),
        category="ECU",
        subcategory="WiCAN",
        widget_type="checkbox",
        getter="get_wican_auto_config",
        setter="set_wican_auto_config",
        keywords=["wican", "slcan", "auto", "config", "protocol", "switch"],
    ),
    SettingDescriptor(
        key="ecu.wican.test_connection",
        label="Test Connection",
        description=(
            "Open the WiCAN link and report reachability + link quality "
            "(packet loss / latency). Honours the auto-configure setting above."
        ),
        category="ECU",
        subcategory="WiCAN",
        widget_type="button",
        getter="",
        setter=None,
        widget_options={
            "text": "Test Connection",
            "callback_name": "_test_wican_connection",
        },
        keywords=["wican", "test", "connection", "link", "ping", "quality"],
    ),
    SettingDescriptor(
        key="ecu.wican.section_trip_logs",
        label="Trip Logs",
        description="",
        category="ECU",
        subcategory="WiCAN",
        widget_type="section",
        getter="",
        setter=None,
    ),
    SettingDescriptor(
        key="ecu.wican.auto_download_logs",
        label="Check for new trip logs at startup",
        description=(
            "At app launch, quietly check the WiCAN's SD card for new trip "
            "logs. When some are found you are asked first — with a size and "
            "time estimate — and a confirmed download runs in the Trip Logs "
            "window. Only active while the WiCAN adapter is selected; skipped "
            "silently when the device is asleep or unreachable; never deletes "
            "from the device."
        ),
        category="ECU",
        subcategory="WiCAN",
        widget_type="checkbox",
        getter="get_wican_auto_download_logs",
        setter="set_wican_auto_download_logs",
        keywords=["wican", "logs", "datalog", "csv", "download", "trip", "auto"],
    ),
    SettingDescriptor(
        key="ecu.wican.logs_dir",
        label="Trip Logs Directory",
        description="Local folder where WiCAN trip logs (.csv) are downloaded",
        category="ECU",
        subcategory="WiCAN",
        widget_type="path_dir",
        getter="get_logs_directory",
        setter="set_logs_directory",
        widget_options={"placeholder": "Folder for WiCAN trip logs"},
        keywords=["wican", "logs", "directory", "folder", "csv", "datalog", "trip"],
    ),
    # -- ECU > Flash Security --
    SettingDescriptor(
        key="ecu.security.status",
        label="Flash Security Module",
        description="Shows whether the security module required for ECU flash operations is installed",
        category="ECU",
        subcategory="Flash Security",
        widget_type="readonly",
        getter="",
        setter=None,
        keywords=["security", "flash", "module", "installed"],
    ),
]

# Ordered list of categories for deterministic tree building
_CATEGORY_ORDER = ["General", "Appearance", "Editor", "Tools", "ECU"]


# ---------------------------------------------------------------------------
# Settings Dialog
# ---------------------------------------------------------------------------


class _WiCANScanWorker(QObject):
    """Runs the blocking mDNS scan off the GUI thread.

    Lives in a :class:`QThread` so the settings dialog stays responsive and can
    show an elapsed-seconds progress dialog with a working Cancel button. The
    scan is hard-bounded by ``timeout_s`` (it can never run forever); a set
    ``cancel_event`` makes it return early. Emits the device list on success or
    the raised exception object on failure (the slot formats the message so the
    existing per-cause warnings are preserved).
    """

    finished = Signal(object)  # list[WiCANDevice]
    error = Signal(object)  # Exception

    def __init__(self, cancel_event, timeout_s):
        super().__init__()
        self._cancel_event = cancel_event
        self._timeout_s = timeout_s

    def run(self):
        try:
            from src.ecu import wican_discovery

            devices = wican_discovery.discover(
                timeout_s=self._timeout_s, cancel_event=self._cancel_event
            )
            self.finished.emit(devices)
        except Exception as e:  # DiscoveryUnavailable / OSError / anything
            self.error.emit(e)


class SettingsDialog(QDialog):
    """Application settings dialog with tree navigation and search."""

    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(720, 560)
        self.resize(820, 640)

        self.settings = get_settings()
        self._widgets = {}  # key -> input widget
        # Pending WiCAN identity to persist on apply: None = leave as-is, "" =
        # clear (user typed a manual IP), else a device_id/mac from an mDNS pick.
        self._pending_wican_device_id = None
        # In-flight mDNS scan state (None when no scan is running).
        self._scan_thread = None
        self._scan_worker = None
        self._scan_progress = None
        self._scan_timer = None
        self._scan_cancel_event = None
        self._scan_host_edit = None
        self._scan_cancelled = False
        self._page_indices = {}  # (category, subcategory) -> stack index
        self._tree_sub_items = {}  # (category, subcategory) -> QTreeWidgetItem
        self._ecu_available = False
        self._secure_module_available = False

        self._active_registry = self._build_active_registry()
        self._init_ui()
        self._setup_shortcuts()
        self.load_settings()

        # Select the first subcategory
        self._select_first_item()

    # ------------------------------------------------------------------ #
    # Registry
    # ------------------------------------------------------------------ #

    def _build_active_registry(self):
        """Build the settings registry, conditionally including ECU entries."""
        registry = [d for d in SETTINGS_REGISTRY if d.category != "ECU"]
        try:
            from src.ecu.flash_manager import SECURE_MODULE_AVAILABLE

            registry.extend(d for d in SETTINGS_REGISTRY if d.category == "ECU")
            self._ecu_available = True
            self._secure_module_available = SECURE_MODULE_AVAILABLE
        except ImportError:
            pass
        return registry

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #

    def _init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # --- Search bar ---
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_icon = QLabel("\U0001f50d")
        search_icon.setFixedWidth(24)
        search_icon.setStyleSheet("font-size: 14px;")
        search_row.addWidget(search_icon)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search settings\u2026")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setStyleSheet(
            "QLineEdit { padding: 5px 8px; border: 1px solid #ccc; "
            "border-radius: 4px; font-size: 12px; }"
            "QLineEdit:focus { border-color: #5b9bd5; }"
        )
        self._search_edit.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self._search_edit)
        outer.addLayout(search_row)

        # --- Splitter: tree | content ---
        self._splitter = QSplitter(Qt.Horizontal)

        # Tree sidebar
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(16)
        self._tree.setMinimumWidth(170)
        self._tree.setMaximumWidth(280)
        self._tree.setStyleSheet(
            "QTreeWidget { border: none; border-right: 1px solid #d0d0d0; "
            "outline: none; font-size: 12px; background: #fafafa; }"
            "QTreeWidget::item { padding: 3px 8px; border-radius: 3px; margin: 1px 4px; }"
            "QTreeWidget::item:selected { background: #e0ecf8; color: black; }"
            "QTreeWidget::item:hover:!selected { background: rgba(128,128,128,0.10); }"
        )
        self._tree.currentItemChanged.connect(self._on_tree_item_changed)
        self._splitter.addWidget(self._tree)

        # Content area
        content_wrapper = QWidget()
        content_layout = QVBoxLayout(content_wrapper)
        content_layout.setContentsMargins(16, 8, 8, 0)
        content_layout.setSpacing(4)

        self._page_title = QLabel()
        self._page_title.setStyleSheet(
            "font-size: 14px; font-weight: 600; padding-bottom: 6px; "
            "border-bottom: 1px solid #e0e0e0; margin-bottom: 4px;"
        )
        content_layout.addWidget(self._page_title)

        self._stack = QStackedWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        scroll.setWidget(self._stack)
        content_layout.addWidget(scroll)

        self._splitter.addWidget(content_wrapper)
        self._splitter.setSizes([200, 620])
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        outer.addWidget(self._splitter)

        # Build tree and pages from registry
        self._build_tree_and_pages()

        # Search results page (added last to the stack)
        self._search_page = QWidget()
        self._search_layout = QVBoxLayout(self._search_page)
        self._search_layout.setContentsMargins(4, 4, 4, 4)
        self._search_page_index = self._stack.addWidget(self._search_page)

        # --- Button box ---
        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        btn_box.button(QDialogButtonBox.Apply).clicked.connect(self.apply_settings)
        outer.addWidget(btn_box)

    def _build_tree_and_pages(self):
        """Populate the nav tree and stacked pages from the active registry."""
        # Collect (category, subcategory) pairs in order
        seen = set()
        ordered_pairs = []
        for desc in self._active_registry:
            pair = (desc.category, desc.subcategory)
            if pair not in seen:
                seen.add(pair)
                ordered_pairs.append(pair)

        # Create a stacked page for each subcategory
        for cat, sub in ordered_pairs:
            page = self._build_page(cat, sub)
            idx = self._stack.addWidget(page)
            self._page_indices[(cat, sub)] = idx

        # Build tree items
        cat_items = {}  # category -> QTreeWidgetItem
        for cat in _CATEGORY_ORDER:
            subs = [s for c, s in ordered_pairs if c == cat]
            if not subs:
                continue
            cat_item = QTreeWidgetItem([cat])
            cat_item.setData(0, Qt.UserRole, None)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._tree.addTopLevelItem(cat_item)
            cat_items[cat] = cat_item

            for sub in subs:
                sub_item = QTreeWidgetItem([sub])
                sub_item.setData(0, Qt.UserRole, (cat, sub))
                cat_item.addChild(sub_item)
                self._tree_sub_items[(cat, sub)] = sub_item

            cat_item.setExpanded(True)

    def _build_page(self, category: str, subcategory: str) -> QWidget:
        """Build a single settings page for a category/subcategory pair."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(2)

        descs = [
            d
            for d in self._active_registry
            if d.category == category and d.subcategory == subcategory
        ]

        for desc in descs:
            # Section break: a rule + header splitting the page into topic
            # groups (e.g. WiCAN connection vs trip logs). Not a setting — no
            # input widget, nothing registered.
            if desc.widget_type == "section":
                layout.addSpacing(10)
                rule = QFrame()
                rule.setFrameShape(QFrame.HLine)
                rule.setFrameShadow(QFrame.Sunken)
                layout.addWidget(rule)
                header = QLabel(desc.label)
                header.setStyleSheet("font-weight: 700; font-size: 13px;")
                layout.addWidget(header)
                layout.addSpacing(4)
                continue

            # Label (skip for checkbox/button/readonly which embed their own)
            if desc.widget_type not in ("checkbox", "button", "readonly"):
                lbl = QLabel(desc.label)
                lbl.setStyleSheet("font-weight: 600; font-size: 12px;")
                layout.addWidget(lbl)

            widget = self._create_setting_widget(desc)
            layout.addWidget(widget)
            layout.addSpacing(6)

        layout.addStretch()
        return page

    # ------------------------------------------------------------------ #
    # Widget factory
    # ------------------------------------------------------------------ #

    def _create_setting_widget(self, desc: SettingDescriptor) -> QWidget:
        """Create the input widget for a setting descriptor."""
        container = QWidget()
        lo = QVBoxLayout(container)
        lo.setContentsMargins(0, 0, 0, 4)
        lo.setSpacing(2)

        wtype = desc.widget_type

        if wtype == "path_dir":
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit()
            edit.setPlaceholderText(desc.widget_options.get("placeholder", ""))
            row.addWidget(edit)
            btn = QPushButton("Browse\u2026")
            btn.setFixedWidth(80)
            btn.clicked.connect(lambda _=False, e=edit: self._browse_directory(e))
            row.addWidget(btn)
            lo.addLayout(row)
            self._widgets[desc.key] = edit

        elif wtype == "path_file":
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit()
            edit.setPlaceholderText(desc.widget_options.get("placeholder", ""))
            row.addWidget(edit)
            filt = desc.widget_options.get("filter", "All Files (*)")
            btn = QPushButton("Browse\u2026")
            btn.setFixedWidth(80)
            btn.clicked.connect(lambda _=False, e=edit, f=filt: self._browse_file(e, f))
            row.addWidget(btn)
            lo.addLayout(row)
            self._widgets[desc.key] = edit

        elif wtype == "text":
            edit = QLineEdit()
            edit.setPlaceholderText(desc.widget_options.get("placeholder", ""))
            scan_cb = desc.widget_options.get("scan_callback")
            if (
                desc.widget_options.get("scan_button")
                and scan_cb
                and hasattr(self, scan_cb)
            ):
                # Render an inline "Scan…" affordance next to the field (mirrors
                # the path_dir/path_file Browse pattern). The handler discovers
                # adapters over mDNS and fills this edit with the chosen IP.
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(edit)
                btn = QPushButton("Scan…")
                btn.setFixedWidth(80)
                btn.clicked.connect(
                    lambda _=False, e=edit, cb=scan_cb: getattr(self, cb)(e)
                )
                row.addWidget(btn)
                lo.addLayout(row)
                # Typing a manual IP detaches the stored mDNS identity so the
                # connect-time re-resolve won't silently override it. textEdited
                # fires only on USER edits, not programmatic setText (Scan/load).
                edit.textEdited.connect(
                    lambda *_: setattr(self, "_pending_wican_device_id", "")
                )
            else:
                lo.addWidget(edit)
            self._widgets[desc.key] = edit

        elif wtype == "spinbox":
            spin = QSpinBox()
            spin.setRange(
                desc.widget_options.get("min", 0),
                desc.widget_options.get("max", 100),
            )
            if "suffix" in desc.widget_options:
                spin.setSuffix(desc.widget_options["suffix"])
            spin.setFixedWidth(120)
            lo.addWidget(spin)
            self._widgets[desc.key] = spin

        elif wtype == "combobox":
            combo = QComboBox()
            for display_text, data_value in desc.widget_options.get("items", []):
                combo.addItem(display_text, data_value)
            combo.setFixedWidth(250)
            lo.addWidget(combo)
            self._widgets[desc.key] = combo

        elif wtype == "checkbox":
            cb = QCheckBox(desc.label)
            lo.addWidget(cb)
            self._widgets[desc.key] = cb

        elif wtype == "button":
            btn = QPushButton(desc.widget_options.get("text", desc.label))
            btn.setFixedWidth(160)
            cb_name = desc.widget_options.get("callback_name")
            if cb_name and hasattr(self, cb_name):
                btn.clicked.connect(getattr(self, cb_name))
            lo.addWidget(btn)

        elif wtype == "readonly":
            lbl = QLabel()
            lbl.setWordWrap(True)
            lo.addWidget(lbl)
            self._widgets[desc.key] = lbl

        # Description text
        if desc.description and wtype not in ("button",):
            desc_lbl = QLabel(desc.description)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet("color: #555; font-size: 11px; padding-top: 2px;")
            lo.addWidget(desc_lbl)

        return container

    # ------------------------------------------------------------------ #
    # Tree navigation
    # ------------------------------------------------------------------ #

    def _select_first_item(self):
        """Select the first subcategory in the tree."""
        root = self._tree.topLevelItem(0)
        if root and root.childCount() > 0:
            first_child = root.child(0)
            self._tree.setCurrentItem(first_child)

    def _on_tree_item_changed(self, current, _previous):
        if current is None:
            return
        data = current.data(0, Qt.UserRole)
        if data is None:
            # Category header clicked — select its first child
            if current.childCount() > 0:
                self._tree.setCurrentItem(current.child(0))
            return
        cat, sub = data
        self._show_page(cat, sub)

    def _show_page(self, category: str, subcategory: str):
        idx = self._page_indices.get((category, subcategory))
        if idx is not None:
            self._stack.setCurrentIndex(idx)
            self._page_title.setText(f"{category}  \u203a  {subcategory}")

    def _select_tree_item(self, category: str, subcategory: str):
        """Programmatically select a tree item by category/subcategory."""
        item = self._tree_sub_items.get((category, subcategory))
        if item:
            self._tree.setCurrentItem(item)

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def _on_search_changed(self, text: str):
        query = text.strip().lower()
        if not query:
            self._show_normal_mode()
            return
        self._show_search_results(query)

    def _show_normal_mode(self):
        """Restore tree navigation mode."""
        # Show all tree items
        for i in range(self._tree.topLevelItemCount()):
            cat_item = self._tree.topLevelItem(i)
            cat_item.setHidden(False)
            for j in range(cat_item.childCount()):
                cat_item.child(j).setHidden(False)
        # Restore selected page
        current = self._tree.currentItem()
        if current:
            data = current.data(0, Qt.UserRole)
            if data:
                self._show_page(*data)

    def _show_search_results(self, query: str):
        """Filter tree and show search results page."""
        # Score and collect matches
        matches = []
        for desc in self._active_registry:
            score = self._match_score(desc, query)
            if score > 0:
                matches.append((score, desc))
        matches.sort(key=lambda x: -x[0])

        # Filter tree using already-scored matches
        matching_pairs = {(d.category, d.subcategory) for _, d in matches}
        self._filter_tree(matching_pairs)

        # Build search results page
        self._populate_search_page(matches, query)
        self._stack.setCurrentIndex(self._search_page_index)
        self._page_title.setText(f"Search: \u201c{query}\u201d")

    def _match_score(self, desc: SettingDescriptor, query: str) -> int:
        if desc.widget_type == "section":
            return 0  # headers are layout, not settings — never a search hit
        score = 0
        fields = [
            (desc.label, 10),
            (desc.description, 5),
            (desc.category, 3),
            (desc.subcategory, 3),
        ]
        for kw in desc.keywords:
            fields.append((kw, 7))
        for text, weight in fields:
            lower = text.lower()
            if query in lower:
                score += weight
                if lower.startswith(query):
                    score += weight
        return score

    def _filter_tree(self, matching_pairs: set):
        """Hide tree items whose (category, subcategory) is not in matching_pairs."""
        for i in range(self._tree.topLevelItemCount()):
            cat_item = self._tree.topLevelItem(i)
            any_visible = False
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                data = child.data(0, Qt.UserRole)
                visible = data in matching_pairs if data else False
                child.setHidden(not visible)
                if visible:
                    any_visible = True
            cat_item.setHidden(not any_visible)

    def _populate_search_page(self, matches, query: str):
        """Rebuild the search results page content."""
        self._clear_layout(self._search_layout)

        if not matches:
            no_results = QLabel(f"No settings matching \u201c{query}\u201d")
            no_results.setStyleSheet("color: #888; font-size: 13px; padding: 20px;")
            no_results.setAlignment(Qt.AlignCenter)
            self._search_layout.addWidget(no_results)
            self._search_layout.addStretch()
            return

        count_lbl = QLabel(
            f"{len(matches)} setting{'s' if len(matches) != 1 else ''} found"
        )
        count_lbl.setStyleSheet("color: #555; font-size: 11px; padding-bottom: 6px;")
        self._search_layout.addWidget(count_lbl)

        current_section = None
        for _score, desc in matches:
            section = f"{desc.category} \u203a {desc.subcategory}"
            if section != current_section:
                current_section = section
                sec_lbl = QLabel(section)
                sec_lbl.setStyleSheet(
                    "font-size: 11px; font-weight: 600; color: #888; "
                    "padding-top: 10px; padding-bottom: 3px; "
                    "border-bottom: 1px solid #e8e8e8;"
                )
                self._search_layout.addWidget(sec_lbl)

            card = self._make_search_card(desc, query)
            self._search_layout.addWidget(card)

        self._search_layout.addStretch()

    def _make_search_card(self, desc: SettingDescriptor, query: str) -> QWidget:
        """Create a clickable search result card."""
        card = QWidget()
        card.setCursor(Qt.PointingHandCursor)
        lo = QVBoxLayout(card)
        lo.setContentsMargins(8, 6, 8, 6)
        lo.setSpacing(2)

        name_html = self._highlight_text(desc.label, query)
        name_lbl = QLabel(name_html)
        name_lbl.setStyleSheet("font-weight: 600; font-size: 12px;")
        lo.addWidget(name_lbl)

        desc_html = self._highlight_text(desc.description, query)
        desc_lbl = QLabel(desc_html)
        desc_lbl.setWordWrap(True)
        desc_lbl.setStyleSheet("color: #555; font-size: 11px;")
        lo.addWidget(desc_lbl)

        path_lbl = QLabel(f"{desc.category} \u203a {desc.subcategory}")
        path_lbl.setStyleSheet("color: #999; font-size: 10px;")
        lo.addWidget(path_lbl)

        card.setStyleSheet(
            "QWidget { border-radius: 4px; }"
            "QWidget:hover { background: rgba(128,128,128,0.08); }"
        )

        # Clicking navigates to the setting's page
        cat, sub = desc.category, desc.subcategory
        card.mousePressEvent = lambda _e, c=cat, s=sub: self._navigate_to(c, s)

        return card

    def _navigate_to(self, category: str, subcategory: str):
        """Clear search and navigate to a specific settings page."""
        self._search_edit.clear()
        self._select_tree_item(category, subcategory)

    @staticmethod
    def _highlight_text(text: str, query: str) -> str:
        """Return HTML with the query highlighted in the text."""
        if not query:
            return text
        escaped_query = re.escape(query)
        pattern = re.compile(f"({escaped_query})", re.IGNORECASE)
        return pattern.sub(
            r'<span style="background:#fff3cd; padding:0 1px;">\1</span>', text
        )

    @staticmethod
    def _clear_layout(layout):
        """Remove all widgets and sub-layouts from a layout."""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()
            elif item.layout():
                SettingsDialog._clear_layout(item.layout())

    # ------------------------------------------------------------------ #
    # Keyboard shortcuts
    # ------------------------------------------------------------------ #

    def _setup_shortcuts(self):
        shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        shortcut.activated.connect(self._search_edit.setFocus)
        self._search_edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self._search_edit and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key_Escape:
                if self._search_edit.text():
                    self._search_edit.clear()
                    return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------ #
    # Browse helpers
    # ------------------------------------------------------------------ #

    def _browse_directory(self, line_edit: QLineEdit):
        current = line_edit.text() or str(Path.cwd())
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            current,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if directory:
            line_edit.setText(directory)

    def _browse_file(self, line_edit: QLineEdit, file_filter: str):
        current = line_edit.text() or str(Path.cwd())
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select File", current, file_filter
        )
        if file_path:
            line_edit.setText(file_path)

    # ------------------------------------------------------------------ #
    # ECU-specific
    # ------------------------------------------------------------------ #

    def _test_j2534_connection(self):
        from PySide6.QtWidgets import QMessageBox
        from src.ecu.constants import DEFAULT_J2534_DLL

        edit = self._widgets.get("ecu.j2534.dll_path")
        dll_path = (edit.text().strip() if edit else "") or DEFAULT_J2534_DLL

        try:
            from src.ecu.j2534 import J2534Device

            device = J2534Device(dll_path)
            device.open()
            device.close()
            QMessageBox.information(
                self, "Connection OK", "J2534 device connected successfully!"
            )
        except Exception as e:
            QMessageBox.warning(
                self,
                "Connection Failed",
                f"Could not connect to J2534 device:\n{e}",
            )

    def _test_wican_connection(self):
        """Open the WiCAN link from the current field values and grade it."""
        from PySide6.QtWidgets import QMessageBox

        host_edit = self._widgets.get("ecu.wican.host")
        port_spin = self._widgets.get("ecu.wican.port")
        auto_cb = self._widgets.get("ecu.wican.auto_config")
        host = (host_edit.text().strip() if host_edit else "") or "192.168.1.169"
        port = port_spin.value() if port_spin else 35000
        auto_config = auto_cb.isChecked() if auto_cb else True

        try:
            from src.ecu.transport import create_ecu_transport
            from src.ecu.protocol import UDSConnection
            from src.ecu.link_quality import check_link_quality
            from src.ecu.wican_config import WiCANConfigurator, WiCANConfigError
            from src.ecu.wican_transport import WiCANError
        except ImportError as e:
            QMessageBox.warning(self, "Unavailable", f"WiCAN modules unavailable:\n{e}")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        configurator = None
        prev_protocol = None
        transport = None
        try:
            if auto_config:
                configurator = WiCANConfigurator(host)
                prev_protocol = configurator.switch_to_slcan()
            transport = create_ecu_transport(
                {"kind": "wican", "host": host, "port": port}
            )
            transport.open()
            uds = UDSConnection(transport)
            result = check_link_quality(uds)
            QApplication.restoreOverrideCursor()
            if result.ok:
                QMessageBox.information(
                    self,
                    "Connection OK",
                    f"WiCAN reachable at {host}:{port}.\n\n"
                    f"Link: {result.replies}/{result.pings} replied, "
                    f"loss {result.loss_pct:.0f}%, p95 {result.p95_ms:.0f} ms.\n"
                    f"{result.reason}",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Link Marginal",
                    f"WiCAN reachable at {host}:{port}, but the link is not "
                    f"flash-ready:\n\n{result.reason}\n\n"
                    "Reads may still work; do not flash over this link.",
                )
        except (WiCANError, WiCANConfigError, OSError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, "Connection Failed", f"Could not reach the WiCAN adapter:\n{e}"
            )
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            if configurator is not None and prev_protocol and prev_protocol != "slcan":
                try:
                    configurator.restore(prev_protocol)
                except Exception:
                    pass

    def _scan_wican_devices(self, host_edit):
        """Discover WiCAN adapters over mDNS (off-thread) and let the user pick one.

        Runs the bounded mDNS scan in a worker thread while showing an
        elapsed-seconds progress dialog with a Cancel button, so the settings
        window never freezes and the user is never left wondering. The scan is
        hard-bounded by ``wican_discovery.DEFAULT_TIMEOUT_S`` (it can never run
        forever) and Cancel stops it immediately. On success the picker fills the
        host field with the chosen adapter's CURRENT IP and stages its stable
        ``device_id`` for persistence (so connect-time re-resolve can follow the
        adapter across DHCP changes).

        The SLCAN port field is deliberately left untouched: the mDNS record
        advertises the device's HTTP port (80), NOT the SLCAN port we connect on
        (35000) — overwriting the port from discovery would break the link.
        """
        from PySide6.QtWidgets import QMessageBox

        try:
            from src.ecu import wican_discovery
        except ImportError as e:
            QMessageBox.warning(
                self, "Unavailable", f"Discovery module unavailable:\n{e}"
            )
            return

        if not wican_discovery.zeroconf_available():
            QMessageBox.warning(
                self,
                "Discovery Unavailable",
                "mDNS discovery needs the 'zeroconf' package.\n\n"
                "Install it with:  pip install zeroconf\n\n"
                "Or enter the WiCAN IP address manually.",
            )
            return

        # A WindowModal progress dialog blocks re-clicks, but guard regardless.
        if self._scan_thread is not None:
            return

        import threading

        timeout_s = float(wican_discovery.DEFAULT_TIMEOUT_S)
        cancel_event = threading.Event()
        total_ticks = max(1, int(timeout_s * 10))  # 100 ms ticker resolution

        progress = QProgressDialog(
            "Scanning for WiCAN adapters…", "Cancel", 0, total_ticks, self
        )
        progress.setWindowTitle("Scan for WiCAN")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        # We drive value/close ourselves so the bar never auto-finishes early.
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        worker = _WiCANScanWorker(cancel_event, timeout_s)
        thread = QThread()
        worker.moveToThread(thread)

        timer = QTimer(self)
        timer.setInterval(100)

        self._scan_thread = thread
        self._scan_worker = worker
        self._scan_progress = progress
        self._scan_timer = timer
        self._scan_cancel_event = cancel_event
        self._scan_host_edit = host_edit
        self._scan_cancelled = False
        self._scan_timeout_s = timeout_s

        timer.timeout.connect(self._on_scan_tick)
        progress.canceled.connect(self._on_scan_cancel)
        # Bound-method receivers => queued delivery lands on the GUI thread (same
        # rationale as ECUProgrammingWindow's flash-worker wiring). The result
        # arrives as a signal argument. The thread/worker are NOT self-disposed
        # via deleteLater: _teardown_scan owns the lifecycle and quit()+wait()s
        # the thread before disposing it (mirrors ECUProgrammingWindow). Dropping
        # the Python refs to a still-running QThread lets PySide6 GC the wrapper
        # and destroy the C++ thread mid-run ("QThread: Destroyed while running").
        worker.finished.connect(self._on_scan_finished, Qt.QueuedConnection)
        worker.error.connect(self._on_scan_error, Qt.QueuedConnection)
        thread.started.connect(worker.run)

        timer.start()
        thread.start()
        progress.show()

    def _on_scan_tick(self):
        """Advance the elapsed-seconds counter on the scan progress dialog."""
        # Once cancelled, leave the "Cancelling…" label alone — don't overwrite it
        # with a fresh elapsed time before teardown closes the dialog.
        if self._scan_cancelled:
            return
        progress = self._scan_progress
        if progress is None:
            return
        # Cap below the maximum so the dialog never auto-closes before the worker
        # reports back; the worker's finished/error signal does the real close.
        value = min(progress.value() + 1, progress.maximum() - 1)
        progress.setValue(value)
        secs = value / 10.0
        progress.setLabelText(
            "Scanning for WiCAN adapters…\n\n"
            f"{secs:.0f}s  (up to {self._scan_timeout_s:.0f}s)"
        )

    def _on_scan_cancel(self):
        """Cancel button — ask the bounded scan to return early."""
        self._scan_cancelled = True
        if self._scan_cancel_event is not None:
            self._scan_cancel_event.set()
        # Stop the ticker so it can't overwrite the "Cancelling…" label; teardown
        # (on the worker's finished/error) disposes of it for good.
        if self._scan_timer is not None:
            self._scan_timer.stop()
        if self._scan_progress is not None:
            self._scan_progress.setLabelText("Cancelling…")

    def _on_scan_finished(self, devices):
        """Worker `finished` slot (GUI thread): present results unless cancelled."""
        # Scans are serialized (the re-entrancy guard + modal picker prevent
        # overlap), so a slot with no active scan is a stale/duplicate delivery
        # from an already-torn-down worker — drop it.
        if self._scan_thread is None:
            return
        cancelled = self._scan_cancelled
        host_edit = self._scan_host_edit
        self._teardown_scan()
        if cancelled:
            return
        self._present_scan_results(devices, host_edit)

    def _on_scan_error(self, exc):
        """Worker `error` slot (GUI thread): surface the cause unless cancelled."""
        from PySide6.QtWidgets import QMessageBox

        from src.ecu import wican_discovery

        if self._scan_thread is None:
            return  # stale/duplicate signal from a torn-down worker
        cancelled = self._scan_cancelled
        self._teardown_scan()
        if cancelled:
            return
        if isinstance(exc, wican_discovery.DiscoveryUnavailable):
            QMessageBox.warning(self, "Discovery Unavailable", str(exc))
        elif isinstance(exc, OSError):
            QMessageBox.warning(
                self,
                "Scan Failed",
                f"mDNS scan failed (network/socket error):\n{exc}\n\n"
                "Common causes: a firewall blocking UDP 5353, no network "
                "interface up, or the system lacking mDNS support. You can "
                "enter the WiCAN IP address manually instead.",
            )
        else:
            QMessageBox.warning(
                self,
                "Scan Failed",
                f"Unexpected error during mDNS scan:\n{exc}\n\n"
                "You can enter the WiCAN IP address manually instead.",
            )

    def _teardown_scan(self, blocking=False):
        """Stop the ticker + dialog and dispose of the worker thread.

        Normally the thread quit()+join is scheduled off the current signal
        handler (``QTimer.singleShot``) so we never block the GUI thread inside a
        slot. On dialog close (``blocking=True``) it joins synchronously, because
        the dialog is about to be destroyed and no later event-loop turn is
        guaranteed to run the deferred cleanup — which would leave a QThread
        destroyed while still running.
        """
        if self._scan_timer is not None:
            self._scan_timer.stop()
            self._scan_timer.deleteLater()
        if self._scan_progress is not None:
            # close() (not cancel()) so we don't re-fire the canceled signal.
            self._scan_progress.close()
            self._scan_progress.deleteLater()
        thread = self._scan_thread
        worker = self._scan_worker
        self._scan_timer = None
        self._scan_progress = None
        self._scan_thread = None
        self._scan_worker = None
        self._scan_cancel_event = None
        self._scan_host_edit = None
        self._scan_cancelled = False
        if thread is None:
            return
        # Hold the refs in the closure/args so the QThread isn't GC'd before it
        # has been quit() + join()ed.
        if blocking:
            self._cleanup_scan_thread(thread, worker)
        else:
            QTimer.singleShot(0, lambda: self._cleanup_scan_thread(thread, worker))

    @staticmethod
    def _cleanup_scan_thread(thread, worker):
        """Quit, join, and dispose a finished/cancelled scan thread (GUI thread)."""
        if thread is not None:
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()

    def _present_scan_results(self, devices, host_edit):
        """Show the device picker and apply the user's choice (GUI thread).

        Split out from the scan orchestration so it stays synchronous and
        unit-testable: handles the empty-result case, the picker, and staging
        the chosen adapter's stable identity for persistence on apply.
        """
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        if not devices:
            QMessageBox.information(
                self,
                "No Devices Found",
                "No WiCAN adapters were found on the network.\n\n"
                "Check that the adapter is powered, joined to the same Wi-Fi/LAN, "
                "and that mDNS (UDP 5353) is not blocked by a firewall. You can "
                "still enter the IP manually.",
            )
            return

        labels = [d.label for d in devices]
        choice, ok = QInputDialog.getItem(
            self,
            "Select WiCAN Adapter",
            f"Found {len(devices)} adapter(s):",
            labels,
            0,
            False,  # not editable
        )
        if not ok or not choice:
            return
        chosen = devices[labels.index(choice)]

        # setText (not user typing) does NOT fire textEdited, so the identity we
        # stage below is not immediately cleared by our manual-edit guard.
        host_edit.setText(chosen.host)
        self._pending_wican_device_id = chosen.stable_id or ""

    # ------------------------------------------------------------------ #
    # Load / Apply settings
    # ------------------------------------------------------------------ #

    def load_settings(self):
        """Load current settings from AppSettings into all widgets."""
        s = self.settings

        for desc in self._active_registry:
            if desc.key not in self._widgets or not desc.getter:
                continue

            widget = self._widgets[desc.key]
            getter = getattr(s, desc.getter, None)
            if getter is None:
                continue
            value = getter()

            if desc.widget_type in ("path_dir", "path_file", "text"):
                widget.setText(str(value))
            elif desc.widget_type == "spinbox":
                widget.setValue(int(value))
            elif desc.widget_type == "combobox":
                idx = widget.findData(value)
                if idx >= 0:
                    widget.setCurrentIndex(idx)
            elif desc.widget_type == "checkbox":
                if desc.key == "editor.toggle.dtc_flags":
                    widget.setChecked("DTC - Activation Flags" in value)
                else:
                    widget.setChecked(bool(value))

        # ECU readonly status
        self._load_ecu_status()

    def _load_ecu_status(self):
        lbl = self._widgets.get("ecu.security.status")
        if lbl is None:
            return
        if self._secure_module_available:
            lbl.setText("Installed \u2014 flash operations are available")
            lbl.setStyleSheet("color: green; font-weight: bold;")
        else:
            lbl.setText(
                "Not installed \u2014 flash operations are disabled.\n"
                "Contact the project maintainer for access to the security module."
            )
            lbl.setStyleSheet("color: red;")

    def apply_settings(self):
        """Save all widget values back to AppSettings."""
        s = self.settings

        for desc in self._active_registry:
            if desc.key not in self._widgets or not desc.setter:
                continue

            widget = self._widgets[desc.key]
            setter = getattr(s, desc.setter, None)
            if setter is None:
                continue

            if desc.widget_type in ("path_dir", "path_file", "text"):
                val = widget.text().strip()
                if val:
                    setter(val)
            elif desc.widget_type == "spinbox":
                setter(widget.value())
            elif desc.widget_type == "combobox":
                setter(widget.currentData())
            elif desc.widget_type == "checkbox":
                if desc.key == "editor.toggle.dtc_flags":
                    cats = ["DTC - Activation Flags"] if widget.isChecked() else []
                    setter(cats)
                else:
                    setter(widget.isChecked())

        # Persist any WiCAN identity change from a Scan pick or manual edit
        # (transactional with the host field above: only written on apply).
        if self._pending_wican_device_id is not None and hasattr(
            s, "set_wican_device_id"
        ):
            s.set_wican_device_id(self._pending_wican_device_id)
            self._pending_wican_device_id = None

        # Reload colormap in case path changed
        reload_colormap()

        self.settings_changed.emit()

    def accept(self):
        """OK button — apply and close."""
        self.apply_settings()
        super().accept()

    def done(self, result):
        """Single close chokepoint (accept/reject/Esc) — stop any in-flight scan.

        Cancelling makes the worker's bounded mDNS wait return promptly; we then
        join the thread *synchronously* before the dialog tears down, so the
        QThread is never destroyed while still running, and so a late ``finished``
        can't pop a device picker after the window has closed (teardown nulls
        ``_scan_thread``, which the slots treat as a stale-signal drop).
        """
        if self._scan_thread is not None:
            self._scan_cancelled = True
            if self._scan_cancel_event is not None:
                self._scan_cancel_event.set()
            self._teardown_scan(blocking=True)
        super().done(result)
