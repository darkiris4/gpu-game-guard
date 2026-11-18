"""The Test run tab: pick a game and the workloads that would be running, and see what the guard would do. The
answer comes from the pure `guard.plan`; nothing is stopped or started."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..guard import CONTAINER_RUNTIMES
from ..model import Target
from .advanced_common import (KIND_RISK, KIND_TITLE, RISK_BADGE, CandidateSource, ScrolledTab, bold_label, clear_layout,
                              divider, first_match, managed_now, mib_text, quiet, setting_row)
from .qt import QtCore, QtWidgets
from .theme import SPACE, Badge, Card, Pill, button, plain_tooltip, safe_text, styled_label

Qt = QtCore.Qt
# action -> (pill text, pill kind, what it means)
OUTCOMES = {
    "ask": ("Ask first", "warn", "The launch is ended and a dialog asks whether to stop these before the game starts."),
    "stop": ("Stop automatically", "danger", "These are stopped without asking, and the game is started again."),
    "ignore": ("Do nothing", "muted", "The game starts normally and nothing is stopped."),
    "paused": ("Paused", "warn", "The guard is paused, so the launch is never interrupted."),
    "disabled": ("Switched off", "muted", "The guard is switched off, so the launch is never interrupted."),
    "nothing-running": ("Nothing to stop", "ok", "Nothing the guard manages is using the GPU."),
}


def target_from_candidate(candidate: Any) -> Target:
    """A guard target for a discovered candidate, named the way the runtimes name it."""
    if candidate.kind == "container":
        parts = str(candidate.key).split(":", 2)
        runtime = parts[1] if len(parts) == 3 else "docker"
        return Target("container", runtime, candidate.name, vram_mib=candidate.vram_mib)
    return Target(candidate.kind, candidate.kind, candidate.label, vram_mib=candidate.vram_mib)


class TestRunTab(ScrolledTab):
    __test__ = False  # not a test case, despite the name

    def __init__(self, model: Any, source: Optional[CandidateSource] = None,
                 parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self.source = source or CandidateSource(model)
        self.last_plan: Any = None
        self.result_lines: List[str] = []
        self._ran = False
        self._candidate_signature: List[tuple] = []
        self.checks: Dict[str, QtWidgets.QCheckBox] = {}
        self._candidates: Dict[str, Any] = {}

        setup = Card("Scenario", "Choose what is running and which game starts. The result is a preview.")
        self.game_combo = QtWidgets.QComboBox()
        self.game_combo.setEditable(True)
        self.game_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.game_combo.lineEdit().setPlaceholderText("steam:730")
        self.game_combo.setMinimumWidth(260)
        self.game_combo.setAccessibleName("Game id")
        setup.addWidget(setting_row("Game", "Pick a recent game or type an id such as steam:730 or a Steam app id.",
                                    self.game_combo, "The game id whose rules are applied."))
        setup.addWidget(divider())
        self.running_box = QtWidgets.QVBoxLayout()
        self.running_box.setSpacing(6)
        holder = QtWidgets.QWidget()
        holder.setLayout(self.running_box)
        self.running_box.setContentsMargins(0, 0, 0, 0)
        self.rescan_button = button("Rescan")
        plain_tooltip(self.rescan_button, "Look for GPU workloads again.")
        setup.addWidget(setting_row("Running now", "Tick the detected workloads to pretend they are running.",
                                    holder, "Candidates found on this machine. Unmanaged ones are not stopped by the "
                                    "real guard.", wide=True))
        setup.addWidget(self.rescan_button, 0, Qt.AlignLeft)
        self.extra_edit = QtWidgets.QLineEdit()
        self.extra_edit.setPlaceholderText("backup-db, ollama")
        setup.addWidget(setting_row("Other containers", "Container names, separated by commas, to pretend are running. "
                                    "Useful to try a pattern from Ignored containers.", self.extra_edit,
                                    "Adds containers that are not running now to the scenario.", wide=True))
        self.ask_check = QtWidgets.QCheckBox("A dialog can be shown")
        self.ask_check.setToolTip("Untick to see what happens when no dialog backend works.")
        setup.addWidget(self.ask_check)
        row = QtWidgets.QHBoxLayout()
        self.run_button = button("Run", "primary")
        self.run_button.setToolTip("Show what the guard would do. Nothing is executed.")
        row.addWidget(self.run_button)
        row.addStretch(1)
        setup.addLayout(row)
        self.column.addWidget(setup)

        self.result = Card("Result", "")
        self.result.setVisible(False)
        self.result_box = QtWidgets.QVBoxLayout()
        self.result_box.setSpacing(SPACE)
        self.result.addLayout(self.result_box)
        self.column.addWidget(self.result)
        self.finish()

        self.run_button.clicked.connect(self.run)
        self.rescan_button.clicked.connect(lambda: self.reload_candidates(True))
        self.refresh()

    # Inputs.
    def game_id(self) -> str:
        text = self.game_combo.currentText().strip()
        index = self.game_combo.findText(text)
        if index >= 0 and self.game_combo.itemData(index):
            return str(self.game_combo.itemData(index))
        return f"steam:{text}" if text.isdigit() else text

    def refresh(self) -> None:
        recent = list(self.model.recent_games())
        wanted = [f"{title or game_id}  ({game_id})" for game_id, title in recent]
        if wanted != [self.game_combo.itemText(i) for i in range(self.game_combo.count())]:
            typed = self.game_combo.currentText()
            with quiet(self.game_combo):
                self.game_combo.clear()
                for (game_id, _title), text in zip(recent, wanted):
                    self.game_combo.addItem(text, game_id)
                self.game_combo.setCurrentIndex(-1)
                self.game_combo.setEditText(typed)
        status = self.model.status()
        if not self._ran:
            self.ask_check.setChecked(True if status is None else bool(status.can_ask))
        self.reload_candidates(False)
        if self._ran:
            self.run()

    def reload_candidates(self, rescan: bool) -> None:
        candidates = self.source.get(rescan)
        config = self.model.config
        managed = {c.key: managed_now(c, config) for c in candidates}
        signature = [(c.key, managed[c.key], c.vram_mib) for c in candidates]
        scanning = self.source.scanning
        self.rescan_button.setEnabled(not scanning)
        self.rescan_button.setText("Scanning..." if scanning else "Rescan")
        if signature == self._candidate_signature:
            return
        self._candidate_signature = signature
        previous = {key: check.isChecked() for key, check in self.checks.items()}
        clear_layout(self.running_box)
        self.checks, self._candidates = {}, {}
        for candidate in candidates:
            line = QtWidgets.QHBoxLayout()
            line.setSpacing(SPACE)
            check = QtWidgets.QCheckBox(safe_text(candidate.label).replace("&", "&&"))
            check.setChecked(previous.get(candidate.key, managed[candidate.key]))
            plain_tooltip(check, candidate.note or candidate.detail)
            risk = KIND_RISK.get(candidate.kind, "medium")
            line.addWidget(check, 1)
            kind_badge = Badge(KIND_TITLE.get(candidate.kind, candidate.kind), RISK_BADGE[risk])
            kind_badge.setFixedWidth(84)
            kind_badge.setAlignment(Qt.AlignCenter)
            line.addWidget(kind_badge)
            memory = styled_label(mib_text(candidate.vram_mib), "small", wrap=False)
            memory.setFixedWidth(68)
            memory.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            line.addWidget(memory)
            note = styled_label("" if managed[candidate.key] else "not managed", "small", wrap=False)
            note.setFixedWidth(84)
            line.addWidget(note)
            holder = QtWidgets.QWidget()
            holder.setLayout(line)
            line.setContentsMargins(0, 0, 0, 0)
            self.running_box.addWidget(holder)
            self.checks[candidate.key] = check
            self._candidates[candidate.key] = candidate
        if not candidates:
            self.running_box.addWidget(styled_label("Scanning..." if scanning or not self.source.loaded
                                                    else "No GPU workloads were detected.", "muted"))

    def targets(self) -> List[Target]:
        """The targets of the scenario: the ticked candidates, then the extra container names."""
        found = [target_from_candidate(self._candidates[key]) for key, check in self.checks.items()
                 if check.isChecked()]
        for name in (part.strip() for part in self.extra_edit.text().split(",")):
            if name:
                found.append(Target("container", "docker", name))
        return found

    # Output.
    def run(self) -> None:
        game_id = self.game_id()
        if not game_id:
            self._show_message("Enter a game id first, for example steam:730.")
            return
        targets = self.targets()
        plan = self.model.plan_for(game_id, targets, self.ask_check.isChecked())
        self.last_plan = plan
        self._ran = True
        self._show_plan(game_id, targets, plan)

    def _show_message(self, text: str) -> None:
        clear_layout(self.result_box)
        self.result_lines = [text]
        self.result_box.addWidget(styled_label(safe_text(text), ""))
        self.result.setVisible(True)

    def _line(self, text: str, role: str = "") -> None:
        self.result_lines.append(text)
        self.result_box.addWidget(styled_label(safe_text(text), role))

    def _target_rows(self, title: str, targets: List[Target], reasons: Optional[Dict[str, str]] = None) -> None:
        self.result_lines.append(title)
        self.result_box.addWidget(bold_label(title))
        for target in targets:
            line = QtWidgets.QHBoxLayout()
            line.setSpacing(SPACE)
            risk = KIND_RISK.get(target.kind, "medium")
            line.addWidget(Badge(KIND_TITLE.get(target.kind, target.kind), RISK_BADGE[risk]))
            name = styled_label(safe_text(target.name), "", wrap=False)
            name.setTextInteractionFlags(Qt.TextSelectableByMouse)
            line.addWidget(name, 1)
            extra = safe_text((reasons or {}).get(target.name) or mib_text(target.vram_mib))
            line.addWidget(styled_label(extra, "small", wrap=False))
            holder = QtWidgets.QWidget()
            holder.setLayout(line)
            line.setContentsMargins(0, 0, 0, 0)
            self.result_box.addWidget(holder)
            self.result_lines.append(f"{target.name}: {extra}")

    def _why_left(self, target: Target, plan: Any) -> str:
        if not self.model.config.auto_containers and target.runtime in CONTAINER_RUNTIMES:
            return "container detection is off"
        if plan.action in ("paused", "disabled", "ignore"):
            return "the guard does not interfere"
        if plan.action == "ask" and not plan.can_ask:
            return "no dialog to ask with"
        return "not selected"

    def _show_plan(self, game_id: str, targets: List[Target], plan: Any) -> None:
        config = self.model.config
        clear_layout(self.result_box)
        self.result_lines = []
        text, kind, meaning = OUTCOMES.get(plan.action, (plan.action, "muted", ""))
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(SPACE)
        head.addWidget(Pill(text, kind))
        head.addWidget(styled_label(safe_text(f"for {game_id}"), "muted", wrap=False))
        head.addStretch(1)
        self.result_box.addLayout(head)
        self.result_lines.append(text)
        self._line(meaning if plan.action != "ask" or plan.can_ask else
                   "No dialog can be shown, so the game starts and nothing is stopped.")
        plain_tooltip(self.result_box.itemAt(self.result_box.count() - 1).widget(), plan.note)
        if plan.will_stop:
            total = sum(t.vram_mib or 0 for t in plan.will_stop)
            self.result_box.addWidget(divider())
            self._target_rows(f"Would be stopped ({len(plan.will_stop)}, {mib_text(total)} of GPU memory)", plan.will_stop)
        excluded = {t.name: "matches ignore pattern " + first_match(t.name, config.ignore_containers)
                    for t in plan.ignored}
        if plan.ignored:
            self.result_box.addWidget(divider())
            self._target_rows(f"Left alone ({len(plan.ignored)})", plan.ignored, excluded)
        leftover = [t for t in targets if t not in plan.will_stop and t not in plan.ignored]
        if leftover:
            reasons = {t.name: self._why_left(t, plan) for t in leftover}
            self.result_box.addWidget(divider())
            self._target_rows(f"Not stopped ({len(leftover)})", leftover, reasons)
        self.result_box.addWidget(divider())
        rule = self.model.effective_action_for_game(game_id)
        own = game_id in config.games
        self._line(f"Rule for this game: {rule} ({'its own rule' if own else 'the default on_launch'}).")
        self._line("A dialog can be shown." if plan.can_ask else "No dialog can be shown on this machine.")
        self._line("Preview only: nothing was stopped or started.", "small")
        self.result.setVisible(True)
