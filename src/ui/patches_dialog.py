"""
Patches Dialog

Shows which bundled NC Flash patches (format v2, e.g. AutoBlip) fit the open
ROM, and applies one: every check runs first, the report says why a patch is
refused, and the result always goes to a NEW file (the open ROM is never
changed). The engine is src/ecu/ncflash_patch.py; this dialog only collects
the user's consent and the output path.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.definition_overlay import stale_tables
from ..ecu import ncflash_patch as N
from ..ecu.exceptions import ROMValidationError
from ..ecu.rom_utils import calculate_flash_start_index, get_cal_id, get_rom_id
from . import theme

logger = logging.getLogger(__name__)

_LEVEL_COLOR = {N.REFUSE: theme.REFUSE_RED, N.WARN: theme.WARNING_AMBER}
_VERDICT_COLOR = {
    "REFUSED": theme.REFUSE_RED,
    "WARN": theme.WARNING_AMBER,
    "OK": theme.PASS_GREEN,
}
_STATE_TEXT = {
    "installed": "Installed",
    "previous": "Older pack installed",
    "available": "Available",
    "unavailable": "Not available",
}
DEFAULT_NEXT_STEPS = [
    "Flash the new file, then drive with the feature off (it ships disabled) "
    "and check that no new fault code appears before you enable it."
]


def describe_rom(data: bytes) -> tuple:
    """(cal ID or None, one-line description) of a ROM image."""
    try:
        cal = get_cal_id(data).decode("ascii", errors="replace").rstrip("\x00")
    except ROMValidationError:
        return None, "not an NC ECU ROM (no calibration ID)"
    try:
        rev = get_rom_id(data)
        romdrop = f"Romdrop revision word {rev}"
    except ROMValidationError:
        romdrop = "no Romdrop revision word (factory ROM?)"
    return cal, f"ROM ID {cal}, {romdrop}"


def next_steps(patch) -> list:
    """(feature name, steps) groups: per feature, else the pack's, else a default."""
    groups = []
    for f in patch.features:
        steps = [str(x) for x in f.get("next_steps", [])]
        if steps:
            groups.append((str(f.get("name") or f.get("id")), steps))
    if not groups:
        groups = [("", patch.next_steps or DEFAULT_NEXT_STEPS)]
    return groups


def feature_details(data: bytes, patch) -> str:
    """'AutoBlip off; Antilag ON (needs X enabled)' for an installed pack."""
    parts = []
    for s in N.feature_states(data, patch):
        state = {True: "ON", False: "off", None: "?"}[s.enabled]
        need = f" (needs {', '.join(s.missing)} enabled)" if s.missing else ""
        parts.append(f"{s.name} {state}{need}")
    return "; ".join(parts)


def flash_size_note(written_addrs: list) -> str:
    """How much of the ROM the first flash after applying rewrites."""
    if not written_addrs:
        return ""
    start = calculate_flash_start_index(min(written_addrs))
    kb = (N.ROM_SIZE - start) // 1024
    return (
        f"The first flash of this file is a large reflash: it rewrites the ROM from "
        f"0x{start:06X} to the end (about {kb} KB), so it takes longer than a table "
        "change. Keep the battery charged and don't interrupt it."
    )


class PatchesDialog(QDialog):
    """Lists the bundled patches for the open ROM and applies one to a new file.

    NC Flash ships one feature pack per ROM ID (release, or test after an
    opt-in); a ROM holds one pack.

    Args:
        rom_path: the open ROM's file (None: no ROM open).
        save_document: called before the file is read; saves pending edits and
            returns False if the user cancelled or the save failed.
        patches: the bundled packs (all ROM IDs); problems: catalogue issues.
        definition: the open ROM's loaded definition, to flag tables lying in a
            pack's area that the pack doesn't define (stale controls).
    """

    def __init__(
        self,
        rom_path,
        save_document,
        patches,
        problems=(),
        parent=None,
        definition=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Patches")
        self.setMinimumSize(760, 560)
        self._rom_path = Path(rom_path) if rom_path else None
        self._save_document = save_document
        self._all = list(patches)
        self._problems = list(problems)
        self._definition = definition
        self._data = None
        self._cal = None
        self._rows = []  # (patch, status) shown
        self._last = None  # (patch, consents, ApplyResult)
        self._consent_boxes = {}
        self._consent_widgets = []
        # Result for the caller (FlashMixin): the file written and whether to open it.
        self.patched_rom_path = None
        self.open_requested = False

        layout = QVBoxLayout(self)
        self._header = QLabel()
        self._header.setWordWrap(True)
        layout.addWidget(self._header)

        self._show_test = QCheckBox("Show test patches (not reviewed for release)")
        self._show_test.toggled.connect(self._refresh)
        layout.addWidget(self._show_test)

        self._list = QTableWidget(0, 4)
        self._list.setHorizontalHeaderLabels(["Patch", "Version", "State", "Details"])
        self._list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._list.verticalHeader().setVisible(False)
        self._list.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._list.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._list, 1)

        self._consent_area = QWidget()
        self._consent_layout = QVBoxLayout(self._consent_area)
        self._consent_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._consent_area)

        self._verdict = QLabel()
        layout.addWidget(self._verdict)
        self._report = QTableWidget(0, 2)
        self._report.setHorizontalHeaderLabels(["Level", "Finding"])
        self._report.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._report.setWordWrap(True)
        self._report.verticalHeader().setVisible(False)
        self._report.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self._report, 2)

        # Non-routine WARNs (spec "WARN classes"): shown apart, each one must be
        # acknowledged before the patched ROM can be saved.
        self._ack_area = QWidget()
        self._ack_layout = QVBoxLayout(self._ack_area)
        self._ack_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._ack_area)
        self._ack_boxes = {}  # message -> QCheckBox
        self._ack_widgets = []

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._check_btn = QPushButton("Check")
        self._check_btn.clicked.connect(self._on_check)
        buttons.addWidget(self._check_btn)
        self._apply_btn = QPushButton("Apply and Save As...")
        self._apply_btn.clicked.connect(self._on_apply)
        buttons.addWidget(self._apply_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._load_rom()
        self._refresh()

    # ------------------------------------------------------------ state

    def _load_rom(self):
        self._data, self._cal = None, None
        if self._rom_path is None:
            self._header.setText("Open a ROM first, then choose Tools > Patches.")
            return
        try:
            self._data = self._rom_path.read_bytes()
        except OSError as e:
            self._header.setText(f"Cannot read {self._rom_path.name}: {e}")
            return
        self._cal, desc = describe_rom(self._data)
        text = f"<b>{self._rom_path.name}</b> (as saved on disk): {desc}."
        if self._problems:
            text += "<br>Some bundled patches were not loaded: " + "; ".join(
                self._problems
            )
        self._header.setText(text)

    def _installed(self):
        if self._data is None:
            return []
        return [
            p
            for p in self._all
            if p.rom_id == self._cal and N.detect(self._data, p).state == "installed"
        ]

    def _refresh(self):
        self._rows = []
        if self._data is not None and self._cal:
            rows = [
                (p, N.detect(self._data, p)) for p in self._all if p.rom_id == self._cal
            ]
            held = [r for r in rows if r[1].state in ("installed", "previous")]
            test = [r for r in rows if r[0].channel == "test"]
            if held:
                # a ROM holds one pack: show what it holds, nothing next to it
                self._rows = held
            elif self._show_test.isChecked() and test:
                # the test pack replaces the release pack, never sits next to it
                self._rows = test
            else:
                self._rows = [r for r in rows if r[0].channel != "test"]
        self._list.setRowCount(0)
        for p, st in self._rows:
            row = self._list.rowCount()
            self._list.insertRow(row)
            title = p.summary + ("  [TEST]" if p.channel == "test" else "")
            state = _STATE_TEXT[st.state]
            detail = st.detail
            if st.state == "installed":
                detail = feature_details(self._data, p) or detail
            elif st.state == "previous":
                state = f"Older pack installed ({st.version})"
                detail = "upgrade not supported yet: re-apply on a ROM without it"
            for col, text in enumerate([title, p.version, state, detail]):
                self._list.setItem(row, col, QTableWidgetItem(text))
        if self._data is not None and self._cal and not self._rows:
            self._verdict.setText(
                f"NC Flash features are not available for {self._cal} yet."
            )
        else:
            self._verdict.setText("")
        self._list.resizeColumnsToContents()
        self._on_selection()

    def _selected(self):
        rows = self._list.selectionModel().selectedRows()
        if not rows:
            return None, None
        return self._rows[rows[0].row()]

    def _on_selection(self):
        patch, st = self._selected()
        self._last = None
        self._show_report(None)
        for w in self._consent_widgets:
            w.setParent(None)
        self._consent_widgets, self._consent_boxes = [], {}
        ok = patch is not None and st.state == "available"
        self._check_btn.setEnabled(ok)
        self._apply_btn.setEnabled(False)
        if not ok:
            return
        for c in N.consents_needed(self._data, patch):
            text = QLabel(c["text"])
            text.setWordWrap(True)
            box = QCheckBox(f"I accept this change ({c['id']}):")
            box.toggled.connect(self._invalidate)
            self._consent_layout.addWidget(box)
            self._consent_layout.addWidget(text)
            self._consent_widgets += [box, text]
            self._consent_boxes[c["id"]] = box

    def _invalidate(self):
        self._last = None
        self._apply_btn.setEnabled(False)

    def _acknowledged(self) -> set:
        return {m for m, box in self._ack_boxes.items() if box.isChecked()}

    def _update_apply(self):
        """Apply needs a passing check AND every non-routine WARN acknowledged."""
        res = self._last[2] if self._last else None
        ok = res is not None and res.output is not None
        self._apply_btn.setEnabled(
            ok and len(self._acknowledged()) == len(self._ack_boxes)
        )

    def _rebuild_acks(self, res, keep=frozenset()):
        for w in self._ack_widgets:
            w.setParent(None)
        self._ack_widgets, self._ack_boxes = [], {}
        if res is None or res.output is None:
            return
        messages = res.report.non_routine()
        if not messages:
            return
        head = QLabel(
            f"<b>{len(messages)} warning(s) need your acknowledgement before saving.</b> "
            "Read each one; tick it only if you understand it and still want the patched ROM."
        )
        head.setWordWrap(True)
        self._ack_layout.addWidget(head)
        self._ack_widgets.append(head)
        for m in messages:
            box = QCheckBox("I have read this warning and accept it:")
            box.setChecked(m in keep)
            box.toggled.connect(self._update_apply)
            text = QLabel(m)
            text.setWordWrap(True)
            self._ack_layout.addWidget(box)
            self._ack_layout.addWidget(text)
            self._ack_widgets += [box, text]
            self._ack_boxes[m] = box

    def _consents(self):
        return {cid for cid, box in self._consent_boxes.items() if box.isChecked()}

    # ------------------------------------------------------------ actions

    def _ensure_saved(self) -> bool:
        """Save pending edits; the check reads the file from disk."""
        if not self._save_document():
            return False
        self._load_rom()
        return self._data is not None

    def _run(self, patch):
        consents = self._consents()
        try:
            res = N.apply_file(self._rom_path, patch, consents, self._installed())
        except Exception as e:  # never half-apply: report and write nothing
            logger.error(f"Patch check failed: {type(e).__name__}: {e}", exc_info=True)
            res = N.ApplyResult(N.Report())
            res.report.add(
                N.REFUSE, f"the check stopped with an error: {type(e).__name__}: {e}"
            )
        for line in self._stale(patch):
            res.report.add(
                N.WARN,
                f"the definition has a table {line} inside the {patch.title} area: "
                "it may be a dead or stale control",
            )
        self._last = (patch, consents, res)
        self._show_report(res)
        return res

    def _stale(self, patch) -> list:
        if self._definition is None:
            return []
        try:
            return stale_tables(
                self._definition,
                patch.footprint,
                patch.definitions,
                f"{patch.id} {patch.version}",
            )
        except Exception as e:  # a display aid only: never block a check
            logger.warning(f"Stale-table check skipped: {e}")
            return []

    def _on_check(self):
        patch, _ = self._selected()
        if patch is None or not self._ensure_saved():
            return
        self._run(patch)

    def _on_apply(self):
        patch, _ = self._selected()
        if patch is None or not self._ensure_saved():
            return
        acknowledged = self._acknowledged()
        res = self._run(patch)  # fresh read + every check again
        self._rebuild_acks(res, keep=acknowledged)
        self._update_apply()
        if res.output is None:
            return
        new = set(res.report.non_routine()) - acknowledged
        if new:
            QMessageBox.information(
                self,
                "Acknowledge Every Warning",
                f"{len(new)} warning(s) below are not acknowledged yet. Read and tick "
                "each one, then choose Apply and Save As again.",
            )
            return
        default = self._rom_path.with_name(
            f"{self._rom_path.stem}_{patch.id}-{patch.version}.bin"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Patched ROM As (a new file)",
            str(default),
            "ROM Files (*.bin);;All Files (*)",
            options=QFileDialog.DontConfirmOverwrite,
        )
        if not path:
            return
        self._write(Path(path), patch, res)

    def _write(self, out_path: Path, patch, res):
        if out_path.resolve() == self._rom_path.resolve() or out_path.exists():
            QMessageBox.warning(
                self,
                "Choose a New File",
                f"{out_path.name} already exists. The patched ROM always goes to a new "
                "file, so the original stays untouched. Choose another name.",
            )
            return
        try:
            N.write_new_file(out_path, res.output)
        except OSError as e:
            QMessageBox.critical(
                self, "Save Failed", f"The patched ROM was not saved:\n{e}"
            )
            return
        logger.info(
            f"Patch {patch.title} applied: {self._rom_path} -> {out_path} "
            f"(sha256 {N.sha256_hex(res.output)})"
        )
        self.patched_rom_path = str(out_path)
        if self._confirm_open(out_path, patch, res):
            self.open_requested = True
            self.accept()

    def _confirm_open(self, out_path: Path, patch, res) -> bool:
        written = N.diff_addrs(res.output, self._data)
        lines, i = [], 1
        for feature, steps in next_steps(patch):
            if feature:
                lines.append(f"{feature}:")
            for step in steps:
                lines.append(f"{i}. {step}")
                i += 1
        box = QMessageBox(self)
        box.setWindowTitle("Patch Applied")
        box.setIcon(QMessageBox.Information)
        box.setText(f"{patch.title} written to a new file:\n{out_path}")
        box.setInformativeText(
            "Every feature ships disabled. Next steps:\n"
            + "\n".join(lines)
            + "\n\n"
            + flash_size_note(written)
            + "\n\nOpen the new file now?"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        return box.exec() == QMessageBox.Yes

    # ------------------------------------------------------------ report

    def _show_report(self, res):
        self._report.setRowCount(0)
        self._rebuild_acks(res)
        self._update_apply()
        if res is None:
            self._verdict.setStyleSheet("")
            if self._rows:
                self._verdict.setText("")
            return
        verdict = res.verdict
        n_ack = len(res.report.non_routine()) if res.output is not None else 0
        text = {
            "REFUSED": "REFUSED: nothing will be written. See the red rows.",
            "WARN": (
                f"OK with warnings: acknowledge the {n_ack} warning(s) below before saving."
                if n_ack
                else "OK with routine warnings only (grey rows): you can save."
            ),
            "OK": "OK: every check passed.",
        }[verdict]
        self._verdict.setText(text)
        self._verdict.setStyleSheet(
            f"color: {_VERDICT_COLOR[verdict]}; font-weight: bold;"
        )
        for f in res.report.rows:
            row = self._report.rowCount()
            self._report.insertRow(row)
            routine = f.cls == N.ROUTINE
            level = QTableWidgetItem(f"{f.level} (routine)" if routine else f.level)
            msg = QTableWidgetItem(f.message)
            msg.setToolTip(f.message)
            color = theme.TEXT_MUTED if routine else _LEVEL_COLOR.get(f.level)
            if color:
                level.setForeground(QColor(color))
                msg.setForeground(QColor(color))
            level.setTextAlignment(Qt.AlignTop | Qt.AlignLeft)
            self._report.setItem(row, 0, level)
            self._report.setItem(row, 1, msg)
        self._report.resizeRowsToContents()


def run_patches_dialog(parent, document, update_tab_title, open_rom_file):
    """Tools > Patches: show the dialog for ``document`` (or None).

    ``update_tab_title(document)`` refreshes the tab after the forced save;
    ``open_rom_file(path)`` opens the patched ROM if the user asks. MainWindow
    passes these explicitly; the dialog never walks back to its parent.
    """
    from ..ecu.bundled_patches import bundled_catalog

    def save_document() -> bool:
        # The checks read the file on disk: pending edits must be saved first.
        if document is None:
            return False
        if not document.is_modified():
            return True
        answer = QMessageBox.question(
            parent,
            "Save First",
            "This ROM has unsaved changes. Patches are checked and applied against "
            "the saved file. Save it now?",
            QMessageBox.Save | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer != QMessageBox.Save:
            return False
        try:
            document.save()
        except Exception as e:
            QMessageBox.warning(parent, "Save Failed", f"Cannot save ROM:\n{e}")
            return False
        document.set_modified(False)
        update_tab_title(document)
        return True

    catalog = bundled_catalog()
    dlg = PatchesDialog(
        document.rom_path if document else None,
        save_document,
        catalog.patches,
        catalog.problems,
        parent=parent,
        definition=getattr(document, "rom_definition", None),
    )
    dlg.exec()
    if dlg.open_requested and dlg.patched_rom_path:
        open_rom_file(dlg.patched_rom_path)
