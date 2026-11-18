"""Dialogs that add or edit a configured target: a service, a command pair, a process, or a ready-made preset.
Every field is validated as it changes, against the same rules `config.validate` applies, and the Save button stays
disabled until the entry is valid."""
from __future__ import annotations

import copy
import fnmatch
import re
import shlex
from typing import Callable, Dict, List, Optional

from .. import config as config_module
from .. import discovery, polkit, presets
from ..config import Config, TargetSpec
from .advanced_common import (KIND_TITLE, TIMEOUT_RANGE, bold_label, error_label, mono_font, quiet, set_switch, slug)
from .qt import QtCore, QtGui, QtWidgets
from .theme import SPACE, Badge, Callout, ToggleSwitch, button, safe_text, styled_label

Qt = QtCore.Qt

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
PROCESS_WARNING = ("Stopping a program can lose unsaved work. GPU Game Guard asks it to close politely and "
                   "never forces it.")


def polkit_command(unit: str) -> str:
    """The command that lets the current user stop and start `unit` without a password prompt."""
    return f"sudo gpu-game-guard polkit allow {unit or 'UNIT.service'}"


def default_polkit_status(unit: str) -> Optional[bool]:
    """Whether a polkit rule already allows `unit`; None when that cannot be told."""
    try:
        return polkit.status([unit]).get(unit)
    except (polkit.PolkitError, OSError, ValueError):
        return None


def split_command(text: str) -> List[str]:
    """A command line entered as one shell-style line, as an argument list; ValueError for open quotes."""
    return shlex.split(text)


def protected_reason(config: Config, pattern: str) -> str:
    """Why a process glob must be refused (it names or would match a protected program), or an empty string."""
    lowered = pattern.lower()
    if discovery.is_protected(config, pattern):
        return f"'{pattern}' is a protected name and is never stopped."
    plain = [p for p in list(discovery.BUILTIN_PROTECT) + list(config.protect) if not re.search(r"[*?\[]", p)]
    for name in plain:
        if fnmatch.fnmatchcase(name.lower(), lowered):
            return f"The pattern would also match the protected name '{name}'."
    return ""


class TargetDialog(QtWidgets.QDialog):
    """Common frame of the add and edit dialogs: a title, fields with an explanation and an error line each, an
    optional extra block, and Cancel and Save buttons. Subclasses supply the fields and `entry()`."""

    kind = ""
    heading = ""
    intro = ""

    def __init__(self, config: Config, existing: Optional[TargetSpec] = None,
                 parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.config = config
        self.existing = existing
        self.taken = {t.id for t in config.targets if existing is None or t.id != existing.id}
        self._touched: set = set()
        self._fields: Dict[str, QtWidgets.QLabel] = {}
        self._loading = True
        self._id_manual = existing is not None
        self.setWindowTitle(("Edit " if existing else "Add ") + self.heading.lower())
        self.setMinimumWidth(540)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(3 * SPACE, 3 * SPACE, 3 * SPACE, 3 * SPACE)
        self._layout.setSpacing(SPACE + 4)
        self._layout.addWidget(styled_label(("Edit " if existing else "Add ") + self.heading.lower(), "headline",
                                            wrap=False))
        self._layout.addWidget(styled_label(self.intro, "muted"))
        self.label_edit = QtWidgets.QLineEdit()
        self.id_edit = QtWidgets.QLineEdit()
        self.build_top()
        self.build_fields()
        self.add_field("label", "Name", self.label_edit, "Shown in dialogs and lists. Optional; the id is used when empty.")
        self.add_field("id", "Id", self.id_edit,
                       "Lowercase letters, digits, - and _. It cannot change after the target is saved."
                       if existing else "Lowercase letters, digits, - and _. Must be unique.")
        self.build_extra()
        self.summary = error_label()
        self._layout.addWidget(self.summary)
        self.buttons = QtWidgets.QHBoxLayout()
        self.buttons.addStretch(1)
        self.cancel_button = button("Cancel")
        self.save_button = button("Save changes" if existing else "Add", "primary")
        self.save_button.setDefault(True)
        self.buttons.addWidget(self.cancel_button)
        self.buttons.addWidget(self.save_button)
        self._layout.addLayout(self.buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        self.label_edit.textChanged.connect(self._label_changed)
        self.id_edit.textEdited.connect(self._id_edited)
        self.id_edit.textChanged.connect(lambda _t: self._touch("id"))
        self.id_edit.setReadOnly(existing is not None)
        if existing is not None:
            with quiet(self.label_edit, self.id_edit):
                self.label_edit.setText(existing.label or existing.id)
                self.id_edit.setText(existing.id)
            self.load(existing)
        self._loading = False
        self.validate()

    # Subclass hooks.
    def build_top(self) -> None:
        """Widgets placed above the name and id fields."""

    def build_fields(self) -> None:
        """The fields specific to the kind of target."""

    def build_extra(self) -> None:
        """What goes below the name and id fields."""

    def load(self, spec: TargetSpec) -> None:
        """Fill the kind's fields from an existing target."""

    def kind_entry(self) -> dict:
        """The kind's keys of the `targets` entry, as entered."""
        return {}

    def kind_problems(self) -> Dict[str, str]:
        """Field name to friendly problem, for the kind's fields."""
        return {}

    # Field plumbing.
    def add_field(self, name: str, title: str, widget: "QtWidgets.QWidget", explanation: str) -> None:
        box = QtWidgets.QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(bold_label(title))
        box.addWidget(widget)
        note = styled_label(explanation, "small")
        error = error_label()
        box.addWidget(note)
        box.addWidget(error)
        self._layout.addLayout(box)
        self._fields[name] = error
        widget.setToolTip(explanation)
        if isinstance(widget, QtWidgets.QLineEdit):
            widget.textChanged.connect(lambda _t, n=name: self._touch(n))
        elif isinstance(widget, QtWidgets.QAbstractSpinBox):
            widget.valueChanged.connect(lambda _v, n=name: self._touch(n))

    def _touch(self, name: str) -> None:
        if not self._loading:
            self._touched.add(name)
            self.validate()

    def _label_changed(self, text: str) -> None:
        if not self._id_manual and not self._loading:
            self.id_edit.setText(slug(text, self.taken) if text.strip() else "")
        self._touch("label")

    def _id_edited(self, _text: str) -> None:
        self._id_manual = True

    # Validation.
    def entry(self) -> dict:
        label = self.label_edit.text().strip()
        entry = {"id": self.id_edit.text().strip(), "kind": self.kind}
        if label:
            entry["label"] = label
        if self.existing is not None:
            entry["enabled"] = self.existing.enabled
        entry.update(self.kind_entry())
        return entry

    def problems(self) -> Dict[str, str]:
        """Every current problem by field name; empty when the entry is valid."""
        found: Dict[str, str] = {}
        target_id = self.id_edit.text().strip()
        if not target_id:
            found["id"] = "Enter an id."
        elif not ID_PATTERN.match(target_id):
            found["id"] = "Use lowercase letters, digits, - and _ only, starting with a letter or digit (40 at most)."
        elif target_id in self.taken:
            found["id"] = "Another target already uses this id."
        found.update(self.kind_problems())
        if not found:
            raw = config_module.validate({"targets": [self.entry()]})
            if raw:
                found["form"] = raw[0].split(": ", 1)[-1]
        return found

    def validate(self) -> None:
        found = self.problems()
        for name, label in self._fields.items():
            shown = found.get(name, "") if name in self._touched else ""
            label.setText(safe_text(shown))
            label.setVisible(bool(shown))
        form = found.get("form", "")
        self.summary.setText(safe_text(form))
        self.summary.setVisible(bool(form))
        self.save_button.setEnabled(not found)

    def error_text(self, name: str) -> str:
        """The message currently shown under field `name`."""
        label = self._fields.get(name)
        return label.text() if label is not None and not label.isHidden() else ""

    def spec(self) -> Optional[TargetSpec]:
        """The target entered, or None while the entry has problems."""
        if self.problems():
            return None
        spec = TargetSpec.from_json(self.entry())
        if self.existing is not None:
            spec.extra = copy.deepcopy(self.existing.extra)  # keys this version does not know stay as they were
        return spec

    def accept(self) -> None:
        self._touched.update(self._fields)
        self.validate()
        if not self.problems():
            super().accept()


class ServiceDialog(TargetDialog):
    kind = "service"
    heading = "service"
    intro = "A systemd service is stopped before a game and started again afterwards."

    def __init__(self, config: Config, existing: Optional[TargetSpec] = None,
                 polkit_status: Optional[Callable[[str], Optional[bool]]] = None,
                 parent: Optional["QtWidgets.QWidget"] = None):
        self._polkit_status = polkit_status or default_polkit_status
        super().__init__(config, existing, parent)

    def build_fields(self) -> None:
        self.unit_edit = QtWidgets.QLineEdit()
        self.unit_edit.setPlaceholderText("ollama.service")
        self.add_field("unit", "Unit", self.unit_edit, "The systemd unit name, ending in .service.")
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItems(["system", "user"])
        self.scope_combo.setToolTip("system: managed by systemctl and usually needs a polkit rule. "
                                    "user: managed by systemctl --user and needs no extra permission.")
        self.add_field("scope", "Scope", self.scope_combo,
                       "system services need permission to stop; user services run as you.")

    def build_extra(self) -> None:
        self.access = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(self.access)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE)
        box = Callout("To stop this service without a password prompt, run this once in a terminal.", "")
        self.access_text = box.label
        self.access_state = Badge("", "muted")
        box.layout().insertWidget(0, self.access_state, 0, Qt.AlignLeft)
        row = QtWidgets.QHBoxLayout()
        self.command_edit = QtWidgets.QLineEdit()
        self.command_edit.setReadOnly(True)
        self.command_edit.setFont(mono_font())
        self.command_edit.setToolTip("The command that grants permission through a polkit rule.")
        self.copy_button = button("Copy")
        self.copy_button.setToolTip("Copy the command to the clipboard.")
        row.addWidget(self.command_edit, 1)
        row.addWidget(self.copy_button)
        box.layout().addLayout(row)
        outer.addWidget(box)
        self._layout.addWidget(self.access)
        self.unit_edit.textChanged.connect(self._refresh_access)
        self.scope_combo.currentIndexChanged.connect(lambda _i: (self._touch("scope"), self._refresh_access()))
        self.copy_button.clicked.connect(self.copy_command)
        self._refresh_access()

    def load(self, spec: TargetSpec) -> None:
        with quiet(self.unit_edit, self.scope_combo):
            self.unit_edit.setText(spec.unit)
            self.scope_combo.setCurrentText(spec.scope)
        self._refresh_access()

    def command_text(self) -> str:
        return polkit_command(self.unit_edit.text().strip())

    def _refresh_access(self) -> None:
        system = self.scope_combo.currentText() == "system"
        self.access.setVisible(system)
        unit = self.unit_edit.text().strip()
        self.command_edit.setText(self.command_text())
        allowed = self._polkit_status(unit) if unit and not self.kind_problems().get("unit") else None
        if allowed is True:
            self.access_state.setText("Already allowed")
            self.access_state.setKind("ok")
        elif allowed is False:
            self.access_state.setText("Not allowed yet")
            self.access_state.setKind("warn")
        else:
            self.access_state.setText("Unchecked")
            self.access_state.setKind("muted")

    def copy_command(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.command_text())

    def kind_entry(self) -> dict:
        return {"unit": self.unit_edit.text().strip(), "scope": self.scope_combo.currentText()}

    def kind_problems(self) -> Dict[str, str]:
        unit = self.unit_edit.text().strip()
        if not unit:
            return {"unit": "Enter the unit name, for example ollama.service."}
        if any(c.isspace() or c == "/" for c in unit):
            return {"unit": "A unit name has no spaces or slashes."}
        if not unit.endswith(".service") or unit == ".service":
            return {"unit": "The unit name must end in .service, for example ollama.service."}
        return {}


class CommandDialog(TargetDialog):
    kind = "command"
    heading = "command"
    intro = "Two commands: one frees the GPU before a game, one brings the workload back afterwards."

    def build_fields(self) -> None:
        self.stop_edit = QtWidgets.QLineEdit()
        self.start_edit = QtWidgets.QLineEdit()
        self.check_edit = QtWidgets.QLineEdit()
        for edit in (self.stop_edit, self.start_edit, self.check_edit):
            edit.setFont(mono_font())
        self.stop_edit.setPlaceholderText("systemctl --user stop my-model-server")
        self.start_edit.setPlaceholderText("systemctl --user start my-model-server")
        self.check_edit.setPlaceholderText("pgrep -x my-model-server")
        self.add_field("stop", "Stop command", self.stop_edit,
                       "Runs before the game. One line; quote arguments that contain spaces. No shell is used.")
        self.add_field("start", "Start command", self.start_edit, "Runs after the game. Same format.")
        self.add_field("check", "Check command (optional)", self.check_edit,
                       "Exit status 0 means the workload is running. Without it the target counts as running whenever "
                       "it is enabled.")
        self.timeout_spin = QtWidgets.QDoubleSpinBox()
        low, high = TIMEOUT_RANGE
        self.timeout_spin.setRange(low, high)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setAccessibleName("Time limit in seconds")
        self.timeout_spin.setValue(60)
        self.add_field("timeout", "Time limit", self.timeout_spin,
                       f"Each command is given up on after this long ({low} to {high} seconds).")

    def load(self, spec: TargetSpec) -> None:
        with quiet(self.stop_edit, self.start_edit, self.check_edit, self.timeout_spin):
            self.stop_edit.setText(shlex.join(spec.stop or []))
            self.start_edit.setText(shlex.join(spec.start or []))
            self.check_edit.setText(shlex.join(spec.check) if spec.check else "")
            self.timeout_spin.setValue(spec.timeout_seconds)

    def _argv(self, edit: "QtWidgets.QLineEdit") -> Optional[List[str]]:
        try:
            return split_command(edit.text())
        except ValueError:
            return None

    def kind_entry(self) -> dict:
        entry = {"stop": self._argv(self.stop_edit) or [], "start": self._argv(self.start_edit) or [],
                 "timeout_seconds": self.timeout_spin.value()}
        check = self._argv(self.check_edit)
        if check:
            entry["check"] = check
        return entry

    def kind_problems(self) -> Dict[str, str]:
        found = {}
        for name, edit, required in (("stop", self.stop_edit, True), ("start", self.start_edit, True),
                                     ("check", self.check_edit, False)):
            argv = self._argv(edit)
            if argv is None:
                found[name] = "A quote is not closed. Put matching quotes around arguments that contain spaces."
            elif not argv and required:
                found[name] = "Enter the command, for example systemctl stop my-service."
        return found


class ProcessDialog(TargetDialog):
    kind = "process"
    heading = "process"
    intro = "A running program is asked to close before a game and may be started again afterwards."

    def build_top(self) -> None:
        self.warning = Callout(PROCESS_WARNING, "warn")
        self._layout.addWidget(self.warning)

    def build_fields(self) -> None:
        self.match_edit = QtWidgets.QLineEdit()
        self.match_edit.setPlaceholderText("obs")
        self.add_field("match", "Program name", self.match_edit,
                       "Matched against the process name, for example obs or firefox*. Not a path.")

    def build_extra(self) -> None:
        self.restart_switch = ToggleSwitch(True, accessible_name="Start the program again after the game")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.restart_switch)
        row.addWidget(styled_label("Start it again after the game, with the same command line.", "", wrap=False))
        row.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        self.restart_switch.setToolTip("When off, the program stays closed after the game.")
        self._layout.addWidget(holder)

    def load(self, spec: TargetSpec) -> None:
        with quiet(self.match_edit):
            self.match_edit.setText(spec.match)
        set_switch(self.restart_switch, spec.restart)

    def kind_entry(self) -> dict:
        return {"match": self.match_edit.text().strip(), "restart": self.restart_switch.isChecked()}

    def kind_problems(self) -> Dict[str, str]:
        match = self.match_edit.text().strip()
        if not match:
            return {"match": "Enter the program name, for example obs."}
        if "/" in match:
            return {"match": "Use the program name (such as obs or firefox*), not a path."}
        reason = protected_reason(self.config, match)
        if reason:
            return {"match": reason}
        return {}


class PresetDialog(QtWidgets.QDialog):
    """Lists the shipped presets. Presets that fit this machine are ticked; presets already configured are shown
    as added and cannot be ticked."""

    def __init__(self, config: Config, load: Optional[Callable[[], list]] = None,
                 applicable: Optional[Callable[..., bool]] = None, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.setWindowTitle("Add from preset")
        self.setMinimumWidth(560)
        self.config = config
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(3 * SPACE, 3 * SPACE, 3 * SPACE, 3 * SPACE)
        layout.setSpacing(SPACE + 4)
        layout.addWidget(styled_label("Add from preset", "headline", wrap=False))
        layout.addWidget(styled_label("Ready-made targets. Ticked ones were found on this machine.", "muted"))
        self.checks: Dict[str, QtWidgets.QCheckBox] = {}
        self.presets = list((load or presets.load_presets)())
        found = applicable or presets.applicable
        for preset in self.presets:
            added = config.target(str(preset.spec.get("id"))) is not None
            try:
                here = bool(found(preset))
            except Exception:
                here = False
            layout.addWidget(self._row(preset, here, added))
        if not self.presets:
            layout.addWidget(styled_label("No presets are available.", "muted"))
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = button("Cancel")
        self.add_button = button("Add selected", "primary")
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.add_button)
        layout.addLayout(buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.add_button.clicked.connect(self.accept)
        for check in self.checks.values():
            check.toggled.connect(lambda _c: self._update())
        self._update()

    def _row(self, preset: "presets.Preset", here: bool, added: bool) -> "QtWidgets.QWidget":
        box = QtWidgets.QFrame()
        box.setObjectName("Card")
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(SPACE + 4)
        grid.setVerticalSpacing(4)
        check = QtWidgets.QCheckBox(preset.label)
        font = check.font()
        font.setWeight(QtGui.QFont.DemiBold)
        check.setFont(font)
        check.setChecked(here and not added)
        check.setEnabled(not added)
        check.setToolTip(preset.description)
        self.checks[preset.id] = check
        kind = str(preset.spec.get("kind", ""))
        state = Badge("Already added", "muted") if added else (
            Badge("Found here", "ok") if here else Badge("Not found", "muted"))
        state.setToolTip("A target with this id is already configured." if added else
                         "The unit or command this preset manages exists on this machine." if here else
                         "The unit or command this preset manages was not found on this machine.")
        grid.addWidget(check, 0, 0)
        grid.addWidget(Badge(KIND_TITLE.get(kind, kind), "ok" if kind == "service" else "warn"), 0, 1)
        grid.addWidget(state, 0, 2)
        note = styled_label(preset.description, "small")
        note.setToolTip(preset.description)
        grid.addWidget(note, 1, 0, 1, 3)
        grid.setColumnStretch(0, 1)
        return box

    def _update(self) -> None:
        self.add_button.setEnabled(any(c.isChecked() for c in self.checks.values()))

    def selected(self) -> List[TargetSpec]:
        """The specs of the ticked presets."""
        return [TargetSpec.from_json(p.spec) for p in self.presets if self.checks[p.id].isChecked()]
