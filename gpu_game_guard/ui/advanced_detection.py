"""The Detection tab: what is detected automatically, which names are left alone or protected, the memory
threshold for a heavy workload, and which runtimes are queried and in what order."""
from __future__ import annotations

from typing import Any, List, Optional

from .. import config as config_module
from ..discovery import BUILTIN_PROTECT
from .advanced_common import (NUMBER_RANGES, ListEditor, RestartNote, ScrolledTab, divider, mono_font, quiet,
                              setting_row)
from .qt import QtCore, QtWidgets
from .theme import SPACE, Card, ToggleSwitch, button, styled_label

Qt = QtCore.Qt
RUNTIME_NOTES = {
    "docker": "Docker containers that use the GPU",
    "podman": "Podman containers that use the GPU",
    "service": "configured systemd services",
    "command": "configured stop and start commands",
    "process": "configured programs",
}
KNOWN_RUNTIMES = list(config_module.DEFAULT_RUNTIMES)


def runtime_order(config_runtimes: List[str]) -> List[str]:
    """The runtimes as listed in the editor: the configured ones in order, then the remaining known ones."""
    first = [r for r in config_runtimes if r in KNOWN_RUNTIMES]
    return first + [r for r in KNOWN_RUNTIMES if r not in first]


class DetectionTab(ScrolledTab):
    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self._build()
        self.refresh()

    def _build(self) -> None:
        containers = Card("Containers", "Docker and Podman containers that use the GPU are found without any setup.")
        self.auto_switch = ToggleSwitch(True, accessible_name="Detect GPU containers automatically")
        containers.addWidget(setting_row(
            "Detect containers automatically",
            "When off, no container is managed and the container runtimes are not queried.", self.auto_switch,
            "auto_containers: detect GPU containers and stop them before a game."))
        containers.addWidget(divider())
        self.ignore_editor = ListEditor("backup-*")
        containers.addWidget(setting_row(
            "Ignored containers",
            "Name patterns of containers that are never stopped. * and ? work as wildcards; case matters.",
            self.ignore_editor, "ignore_containers: fnmatch patterns on the container name.", wide=True))
        self.column.addWidget(containers)

        protect = Card("Protected names", "Programs and units that are never offered and never stopped.")
        self.protect_editor = ListEditor("my-editor*")
        protect.addWidget(setting_row(
            "Your protected names",
            "Added to the built-in list. Patterns on a program or unit name; case does not matter.",
            self.protect_editor, "protect: extra fnmatch patterns that are never stopped.", wide=True))
        protect.addWidget(divider())
        self.builtin_toggle = button(f"Show built-in protected names ({len(BUILTIN_PROTECT)})")
        self.builtin_toggle.setCheckable(True)
        self.builtin_toggle.setProperty("flat", "true")
        self.builtin_toggle.setToolTip("Compositors, audio, session managers, Steam, terminals, shells and the guard "
                                       "itself. This list is fixed.")
        self.builtin_view = QtWidgets.QPlainTextEdit()
        self.builtin_view.setReadOnly(True)
        self.builtin_view.setFont(mono_font())
        self.builtin_view.setPlainText("  ".join(BUILTIN_PROTECT))
        self.builtin_view.setMaximumHeight(120)
        self.builtin_view.setVisible(False)
        self.builtin_toggle.toggled.connect(self._toggle_builtin)
        protect.addWidget(self.builtin_toggle)
        protect.addWidget(self.builtin_view)
        self.column.addWidget(protect)

        limits = Card("Memory", "")
        low, high = NUMBER_RANGES["heavy_vram_mib"][1:]
        self.heavy_spin = QtWidgets.QSpinBox()
        self.heavy_spin.setRange(int(low), int(high))
        self.heavy_spin.setSuffix(" MiB")
        self.heavy_spin.setKeyboardTracking(False)
        self.heavy_spin.setMinimumWidth(150)
        limits.addWidget(setting_row(
            "Heavy workload threshold",
            f"A container holding at least this much GPU memory is called heavy in dialogs. {int(low)} to {int(high)} MiB.",
            self.heavy_spin, "heavy_vram_mib: at what GPU memory use a workload counts as heavy."))
        self.column.addWidget(limits)

        runtimes = Card("Runtimes", "Which kinds of target the guard looks at, and in what order.")
        self.runtime_list = QtWidgets.QListWidget()
        self.runtime_list.setMaximumHeight(150)
        self.runtime_list.setAlternatingRowColors(True)
        self.runtime_list.setToolTip("Tick the runtimes to use. Order decides which is tried first.")
        buttons = QtWidgets.QVBoxLayout()
        self.up_button = button("Up")
        self.down_button = button("Down")
        self.up_button.setToolTip("Move the selected runtime earlier.")
        self.down_button.setToolTip("Move the selected runtime later.")
        buttons.addWidget(self.up_button)
        buttons.addWidget(self.down_button)
        buttons.addStretch(1)
        both = QtWidgets.QHBoxLayout()
        both.setSpacing(SPACE)
        both.addWidget(self.runtime_list, 1)
        both.addLayout(buttons)
        holder = QtWidgets.QWidget()
        holder.setLayout(both)
        both.setContentsMargins(0, 0, 0, 0)
        runtimes.addWidget(setting_row(
            "Enabled runtimes", "Unticked runtimes are skipped. Docker and podman only matter when containers are "
            "detected automatically.", holder, "runtimes: the runtimes to use, in order of preference.", wide=True))
        runtimes.addWidget(styled_label("Changing runtimes needs a service restart before it takes effect.", "small"))
        self.restart_note = RestartNote(self.model)
        runtimes.addWidget(self.restart_note)
        self.column.addWidget(runtimes)
        self.finish()

        self.auto_switch.toggled.connect(lambda on: self.model.update(auto_containers=on))
        self.ignore_editor.changed.connect(lambda items: self.model.update(ignore_containers=items))
        self.protect_editor.changed.connect(lambda items: self.model.update(protect=items))
        self.heavy_spin.valueChanged.connect(lambda value: self.model.update(heavy_vram_mib=int(value)))
        self.runtime_list.itemChanged.connect(lambda _item: self._write_runtimes())
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.runtime_list.currentRowChanged.connect(lambda _r: self._update_move_buttons())

    def _toggle_builtin(self, shown: bool) -> None:
        self.builtin_view.setVisible(shown)
        self.builtin_toggle.setText(("Hide" if shown else "Show") + f" built-in protected names ({len(BUILTIN_PROTECT)})")

    def refresh(self) -> None:
        config = self.model.config
        with quiet(self.auto_switch, self.heavy_spin):
            self.auto_switch.setChecked(config.auto_containers)
            if self.heavy_spin.value() != config.heavy_vram_mib:
                self.heavy_spin.setValue(config.heavy_vram_mib)
        self.ignore_editor.set_items(list(config.ignore_containers))
        self.protect_editor.set_items(list(config.protect))
        self._load_runtimes(config.runtimes)
        self.restart_note.refresh()

    # Runtime order and selection.
    def _load_runtimes(self, enabled: List[str]) -> None:
        wanted = [(name, name in enabled) for name in runtime_order(enabled)]
        if wanted != self.runtimes():
            self._show_runtimes(wanted)

    def _show_runtimes(self, order: List[tuple]) -> None:
        current = self.runtime_list.currentRow()
        with quiet(self.runtime_list):
            self.runtime_list.clear()
            for name, on in order:
                item = QtWidgets.QListWidgetItem(f"{name}  -  {RUNTIME_NOTES[name]}")
                item.setData(Qt.UserRole, name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if on else Qt.Unchecked)
                item.setToolTip(f"{name}: {RUNTIME_NOTES[name]}.")
                self.runtime_list.addItem(item)
            if 0 <= current < self.runtime_list.count():
                self.runtime_list.setCurrentRow(current)
        self._update_move_buttons()

    def runtimes(self) -> List[tuple]:
        """(name, ticked) for every row, in the order shown."""
        result = []
        for i in range(self.runtime_list.count()):
            item = self.runtime_list.item(i)
            result.append((item.data(Qt.UserRole), item.checkState() == Qt.Checked))
        return result

    def _write_runtimes(self) -> None:
        self.model.update(runtimes=[name for name, on in self.runtimes() if on])

    def _update_move_buttons(self) -> None:
        row = self.runtime_list.currentRow()
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < self.runtime_list.count() - 1)

    def _move(self, step: int) -> None:
        row = self.runtime_list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.runtime_list.count():
            return
        order = self.runtimes()
        order[row], order[target] = order[target], order[row]
        self.model.update(runtimes=[name for name, on in order if on])
        # An unticked runtime has no place in the saved order, so the display order is restored after the reload.
        self._show_runtimes(order)
        self.runtime_list.setCurrentRow(target)
