"""The Targets tab: everything the guard manages, auto-detected containers (read-only) and configured targets,
with buttons to add a preset, a service, a command or a process, and to edit or remove a configured target."""
from __future__ import annotations

import dataclasses
import shlex
from typing import Any, Callable, List, Optional, Tuple

from ..config import TargetSpec
from . import advanced_target_dialogs as dialogs_
from .advanced_common import (KIND_RISK, KIND_TIP, KIND_TITLE, RISK_BADGE, BadgeDelegate, CandidateSource, TabBase,
                              centered, focus_cell, focused_cell, make_table, mib_text, set_switch, table_item)
from .qt import QtCore, QtWidgets
from .theme import SPACE, Callout, ToggleSwitch, button, plain_tooltip, styled_label

Qt = QtCore.Qt
IGNORED_NOTE = "Ignored through ignore_containers. "
COLUMNS = ["On", "Name", "Type", "Details", "Memory"]


def spec_detail(spec: TargetSpec) -> str:
    if spec.kind == "service":
        return f"{spec.unit} ({spec.scope})"
    if spec.kind == "command":
        return "stop: " + shlex.join(spec.stop or [])
    return f'matches "{spec.match}"' + ("; restarted after the game" if spec.restart else "; not restarted")


class TargetsTab(TabBase):
    def __init__(self, model: Any, source: Optional[CandidateSource] = None, polkit_status: Optional[Callable[[str], Optional[bool]]] = None,
                 presets_loader: Optional[Callable[[], list]] = None,
                 applicable: Optional[Callable[..., bool]] = None, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self.source = source or CandidateSource(model)
        self.polkit_status = polkit_status
        self.presets_loader = presets_loader
        self.applicable = applicable
        self.rows: List[Tuple[str, Any]] = []  # ("auto", Candidate) or ("target", TargetSpec), one per table row
        self._keys: List[Tuple[str, str]] = []  # what each table row stands for; a change means a rebuild
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        layout.setSpacing(SPACE + 4)
        self.note = Callout("Containers are detected automatically and shown here for reference only. To leave one "
                            "alone, add its name under Detection, Ignored containers.")
        layout.addWidget(self.note)
        self.table = make_table(COLUMNS)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Fixed)
        metrics = self.fontMetrics()
        self.table.setColumnWidth(0, max(64, metrics.horizontalAdvance("On") + 56))
        self.table.setColumnWidth(1, 190)
        self.table.setColumnWidth(2, max(112, metrics.horizontalAdvance("Container") + 48))
        self.table.setColumnWidth(4, max(96, metrics.horizontalAdvance("Memory") + 48))
        self.table.setItemDelegateForColumn(2, BadgeDelegate(self.table))
        self.table.horizontalHeaderItem(4).setTextAlignment(int(Qt.AlignRight | Qt.AlignVCenter))
        layout.addWidget(self.table, 1)
        self.caption = styled_label("", "small")
        layout.addWidget(self.caption)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(SPACE)
        self.preset_button = button("Add preset")
        self.service_button = button("Add service")
        self.command_button = button("Add command")
        self.process_button = button("Add process")
        self.edit_button = button("Edit")
        self.remove_button = button("Remove")
        tips = {
            self.preset_button: "Pick ready-made targets such as Ollama, ticked when found on this machine.",
            self.service_button: "Manage a systemd service.",
            self.command_button: "Manage a workload with a stop command and a start command.",
            self.process_button: "Manage a running program. Riskier: unsaved work can be lost.",
            self.edit_button: "Change the selected target.",
            self.remove_button: "Remove the selected target from the configuration.",
        }
        for widget, tip in tips.items():
            plain_tooltip(widget, tip)
        for widget in (self.preset_button, self.service_button, self.command_button, self.process_button):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self.edit_button)
        row.addWidget(self.remove_button)
        layout.addLayout(row)
        self.preset_button.clicked.connect(self.add_preset)
        self.service_button.clicked.connect(lambda: self.add_target("service"))
        self.command_button.clicked.connect(lambda: self.add_target("command"))
        self.process_button.clicked.connect(lambda: self.add_target("process"))
        self.edit_button.clicked.connect(self.edit_selected)
        self.remove_button.clicked.connect(self.remove_selected)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_selected())
        self.refresh()

    # Table content.
    def _memory_of(self, spec: TargetSpec, candidates: list) -> str:
        for candidate in candidates:
            if candidate.kind == "service" and spec.kind == "service" and candidate.name == spec.unit:
                return mib_text(candidate.vram_mib)
            if candidate.kind == "process" and spec.kind == "process" and candidate.name == spec.match:
                return mib_text(candidate.vram_mib)
        return "-"

    def refresh(self) -> None:
        config = self.model.config
        candidates = self.source.get()
        containers = [c for c in candidates if c.kind == "container"]
        targets = list(config.targets)
        rows: List[Tuple[str, Any]] = [("auto", c) for c in containers] + [("target", t) for t in targets]
        keys = [("auto", c.key) if origin == "auto" else ("target", c.id) for origin, c in rows]
        selected = self.selected_id()
        focus = focused_cell(self.table)
        focus_key = (self._keys[focus[0]], focus[1]) if focus and focus[0] < len(self._keys) else None
        self.rows = rows
        rebuild = keys != self._keys
        self.table.setUpdatesEnabled(False)
        if rebuild:
            self.table.clearContents()
            self.table.setRowCount(len(rows))
            self._keys = keys
        for index, (origin, obj) in enumerate(rows):
            if origin == "auto":
                self._fill_container(index, obj, config, rebuild)
            else:
                self._fill_target(index, obj, candidates, rebuild)
        self.table.setUpdatesEnabled(True)
        auto_on = config.auto_containers
        self.note.setVisible(True)
        if self.source.scanning or not self.source.loaded:
            lead = "Scanning... "
        else:
            lead = ""
        self.caption.setText(
            lead + f"{len(containers)} auto-detected container{'' if len(containers) == 1 else 's'}"
            f"{'' if auto_on else ' (detection is off)'}, {len(targets)} configured "
            f"target{'' if len(targets) == 1 else 's'}.")
        if selected and rebuild:
            for index, (origin, obj) in enumerate(rows):
                if origin == "target" and obj.id == selected:
                    self.table.selectRow(index)
        if rebuild and focus_key is not None and focus_key[0] in keys:
            focus_cell(self.table, keys.index(focus_key[0]), focus_key[1])
        self._selection_changed()

    def _fill_container(self, index: int, candidate: Any, config: Any, create: bool) -> None:
        ignored = config.is_ignored(candidate.name)
        managed = bool(config.auto_containers) and not ignored
        note = candidate.note
        if note.startswith(IGNORED_NOTE):
            note = note[len(IGNORED_NOTE):]
        if ignored:
            note = IGNORED_NOTE + note
        if create:
            switch = ToggleSwitch(managed, accessible_name=f"{candidate.label} is managed")
            switch.setEnabled(False)
            plain_tooltip(switch, "Detected automatically. Exclude it under Detection, Ignored containers.")
            self.table.setCellWidget(index, 0, centered(switch))
        else:
            switch = self.table.cellWidget(index, 0).findChild(ToggleSwitch)
            set_switch(switch, managed)
        suffix = " (ignored)" if ignored else "" if config.auto_containers else " (detection is off)"
        self.table.setItem(index, 1, table_item(candidate.label, note))
        self.table.setItem(index, 2, table_item("Container", KIND_TIP[candidate.risk] if candidate.risk in KIND_TIP
                                                else "", kind=RISK_BADGE.get(candidate.risk, "ok")))
        self.table.setItem(index, 3, table_item(candidate.detail + suffix, note, muted=True))
        self.table.setItem(index, 4, table_item(mib_text(candidate.vram_mib), align=int(Qt.AlignRight)))

    def _fill_target(self, index: int, spec: TargetSpec, candidates: list, create: bool) -> None:
        if create:
            switch = ToggleSwitch(spec.enabled, accessible_name=f"{spec.label or spec.id} is managed")
            plain_tooltip(switch, "Switch the target off to keep it configured without stopping it.")
            switch.toggled.connect(lambda on, tid=spec.id: self._set_enabled(tid, on))
            self.table.setCellWidget(index, 0, centered(switch))
        else:
            switch = self.table.cellWidget(index, 0).findChild(ToggleSwitch)
            switch.setAccessibleName(f"{spec.label or spec.id} is managed")
            set_switch(switch, spec.enabled)
        risk = KIND_RISK.get(spec.kind, "medium")
        self.table.setItem(index, 1, table_item(spec.label or spec.id, f"id: {spec.id}", muted=not spec.enabled))
        self.table.setItem(index, 2, table_item(KIND_TITLE.get(spec.kind, spec.kind), KIND_TIP[risk],
                                                kind=RISK_BADGE[risk]))
        detail = spec_detail(spec)
        self.table.setItem(index, 3, table_item(detail, detail, muted=True))
        self.table.setItem(index, 4, table_item(self._memory_of(spec, candidates), align=int(Qt.AlignRight)))

    def _set_enabled(self, target_id: str, enabled: bool) -> None:
        spec = self.model.config.target(target_id)
        if spec is not None and spec.enabled != enabled:
            self.model.set_target(dataclasses.replace(spec, enabled=enabled))

    # Selection.
    def selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def selected_spec(self) -> Optional[TargetSpec]:
        row = self.selected_row()
        if 0 <= row < len(self.rows) and self.rows[row][0] == "target":
            return self.rows[row][1]
        return None

    def selected_id(self) -> str:
        spec = self.selected_spec()
        return spec.id if spec else ""

    def _selection_changed(self) -> None:
        has = self.selected_spec() is not None
        self.edit_button.setEnabled(has)
        self.remove_button.setEnabled(has)

    # Actions.
    def add_preset(self) -> None:
        dialog = dialogs_.PresetDialog(self.model.config, self.presets_loader, self.applicable, self)
        if self.run_dialog(dialog):
            for spec in dialog.selected():
                self.model.set_target(spec)

    def make_dialog(self, kind: str, existing: Optional[TargetSpec] = None) -> "QtWidgets.QDialog":
        config = self.model.config
        if kind == "service":
            return dialogs_.ServiceDialog(config, existing, self.polkit_status, self)
        if kind == "command":
            return dialogs_.CommandDialog(config, existing, self)
        return dialogs_.ProcessDialog(config, existing, self)

    def add_target(self, kind: str) -> None:
        dialog = self.make_dialog(kind)
        if self.run_dialog(dialog):
            spec = dialog.spec()
            if spec is not None:
                self.model.set_target(spec)

    def edit_selected(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            return
        dialog = self.make_dialog(spec.kind, spec)
        if self.run_dialog(dialog):
            updated = dialog.spec()
            if updated is not None:
                self.model.set_target(updated)

    def remove_selected(self) -> None:
        spec = self.selected_spec()
        if spec is None:
            return
        if self.confirm(self, f"Remove the target \"{spec.label or spec.id}\"? Its settings are deleted from the "
                              "configuration once the changes are applied."):
            self.model.remove_target(spec.id)
