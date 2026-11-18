"""The Games tab: one rule per game saying what the guard does when that game launches, and a dialog to add one."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from ..config import GAME_ACTIONS
from .advanced_common import (TabBase, bold_label, centered, error_label, make_table, quiet, table_item)
from .qt import QtCore, QtWidgets
from .theme import SPACE, Badge, button, styled_label

Qt = QtCore.Qt
GAME_ID = re.compile(r"[a-z0-9_-]+:\S+\Z")
ACTION_TIPS = {
    "ask": "Ask each time this game starts.",
    "stop": "Stop what is using the GPU without asking.",
    "ignore": "Start the game and leave everything running.",
}
DEFAULT_BADGE = {"ask": "warn", "stop": "danger", "ignore": "muted"}


def normalise_game_id(text: str) -> str:
    """A game id as typed: a bare number is a Steam app id and becomes `steam:<number>`."""
    text = text.strip()
    return f"steam:{text}" if text.isdigit() else text


def game_id_problem(text: str) -> str:
    """Why `text` cannot be a game id, or an empty string."""
    game_id = normalise_game_id(text)
    if not game_id:
        return "Enter a game id such as steam:730, or just the Steam app id."
    if not GAME_ID.match(game_id):
        return "A game id looks like steam:730: a source, a colon, then the game's id, with no spaces."
    return ""


class AddGameDialog(QtWidgets.QDialog):
    """Asks for a game id, a name and an action. Recent games listed by the model fill in the id and name."""

    def __init__(self, existing_ids: List[str], recent: List[Tuple[str, str]], default_action: str = "ask",
                 parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(parent)
        self.existing_ids = set(existing_ids)
        self.setWindowTitle("Add game rule")
        self.setMinimumWidth(520)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(3 * SPACE, 3 * SPACE, 3 * SPACE, 3 * SPACE)
        layout.setSpacing(SPACE + 4)
        layout.addWidget(styled_label("Add game rule", "headline", wrap=False))
        layout.addWidget(styled_label("A rule overrides the default for one game.", "muted"))
        layout.addWidget(bold_label("Recent games"))
        self.recent_list = QtWidgets.QListWidget()
        self.recent_list.setMaximumHeight(128)
        self.recent_list.setMinimumHeight(64)
        self.recent_list.setAlternatingRowColors(True)
        self.recent_list.setToolTip("Games the guard has seen launch. Pick one to fill in the id and name.")
        for game_id, title in recent:
            item = QtWidgets.QListWidgetItem(f"{title or game_id}   ({game_id})")
            item.setData(Qt.UserRole, (game_id, title))
            item.setSizeHint(QtCore.QSize(0, 30))
            self.recent_list.addItem(item)
        if not recent:
            placeholder = QtWidgets.QListWidgetItem("No games seen yet")
            placeholder.setFlags(Qt.NoItemFlags)
            self.recent_list.addItem(placeholder)
        layout.addWidget(self.recent_list)
        self.id_edit = QtWidgets.QLineEdit()
        self.id_edit.setPlaceholderText("steam:730")
        self.id_edit.setToolTip("The game's id. For a Steam game this is steam: followed by its app id.")
        self.id_hint = styled_label("A bare Steam app id such as 730 is read as steam:730.", "small")
        self.id_error = error_label()
        layout.addWidget(bold_label("Game id"))
        layout.addWidget(self.id_edit)
        layout.addWidget(self.id_hint)
        layout.addWidget(self.id_error)
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("Counter-Strike 2")
        self.name_edit.setToolTip("Only for display; the id decides which game the rule applies to.")
        layout.addWidget(bold_label("Name"))
        layout.addWidget(self.name_edit)
        self.action_combo = QtWidgets.QComboBox()
        for action in GAME_ACTIONS:
            self.action_combo.addItem(action)
            self.action_combo.setItemData(self.action_combo.count() - 1, ACTION_TIPS[action], Qt.ToolTipRole)
        self.action_combo.setCurrentText(default_action if default_action in GAME_ACTIONS else "ask")
        self.action_combo.setToolTip("ask: ask each time. stop: stop without asking. ignore: never interfere.")
        layout.addWidget(bold_label("Action"))
        layout.addWidget(self.action_combo)
        layout.addWidget(styled_label("ask asks each time, stop stops without asking, ignore never interferes.",
                                      "small"))
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = button("Cancel")
        self.add_button = button("Add rule", "primary")
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.add_button)
        layout.addLayout(buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.add_button.clicked.connect(self.accept)
        self.recent_list.itemClicked.connect(self._pick)
        self.id_edit.textChanged.connect(self._validate)
        self._shown = False
        self._validate()

    def _pick(self, item: "QtWidgets.QListWidgetItem") -> None:
        data = item.data(Qt.UserRole)
        if data:
            self.id_edit.setText(data[0])
            self.name_edit.setText(data[1])

    def game_id(self) -> str:
        return normalise_game_id(self.id_edit.text())

    def problem(self) -> str:
        return game_id_problem(self.id_edit.text())

    def _validate(self) -> None:
        text = self.id_edit.text().strip()
        problem = self.problem() if text else ""
        self.id_error.setText(problem)
        self.id_error.setVisible(bool(problem))
        self.add_button.setEnabled(not self.problem())
        if text and not problem and self.game_id() in self.existing_ids:
            self.id_hint.setText("This game already has a rule; adding replaces it.")
        else:
            self.id_hint.setText("A bare Steam app id such as 730 is read as steam:730.")

    def accept(self) -> None:
        self._validate()
        if not self.problem():
            super().accept()

    def values(self) -> Tuple[str, str, str]:
        """(game id, name, action) as entered."""
        return self.game_id(), self.name_edit.text().strip(), self.action_combo.currentText()


class GamesTab(TabBase):
    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        layout.setSpacing(SPACE + 4)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(SPACE)
        head.addWidget(bold_label("Default for games without a rule", "What happens on launch when a game has no rule "
                                  "below. It is set on the Basic page (on_launch)."))
        self.default_badge = Badge("", "muted")
        head.addWidget(self.default_badge)
        self.default_text = styled_label("", "small", wrap=False)
        head.addWidget(self.default_text, 1)
        layout.addLayout(head)
        self.table = make_table(["Game id", "Name", "Action", ""], row_height=46)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Fixed)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Fixed)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 110)
        layout.addWidget(self.table, 1)
        self.empty = styled_label("No game has its own rule yet. Add one to treat a game differently from the default.",
                                  "muted")
        layout.addWidget(self.empty)
        row = QtWidgets.QHBoxLayout()
        self.add_button = button("Add game", "primary")
        self.add_button.setToolTip("Add a rule for one game, for example steam:730.")
        row.addWidget(self.add_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.add_button.clicked.connect(self.add_game)
        self.combos: Dict[str, QtWidgets.QComboBox] = {}
        self.refresh()

    def refresh(self) -> None:
        config = self.model.config
        self.default_badge.setText(config.on_launch)
        self.default_badge.setKind(DEFAULT_BADGE.get(config.on_launch, "muted"))
        self.default_text.setText(ACTION_TIPS.get(config.on_launch, ""))
        games = sorted(config.games.items())
        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(games))
        self.combos = {}
        for index, (game_id, rule) in enumerate(games):
            self.table.setItem(index, 0, table_item(game_id, game_id))
            self.table.setItem(index, 1, table_item(rule.name or "(no name)", rule.name, muted=not rule.name))
            combo = QtWidgets.QComboBox()
            combo.addItems(list(GAME_ACTIONS))
            for position, action in enumerate(GAME_ACTIONS):
                combo.setItemData(position, ACTION_TIPS[action], Qt.ToolTipRole)
            combo.setToolTip(f"What happens when {rule.name or game_id} launches. ask, stop or ignore.")
            with quiet(combo):
                combo.setCurrentText(rule.action)
            combo.currentTextChanged.connect(lambda action, g=game_id, n=rule.name: self.model.set_game(g, n, action))
            self.combos[game_id] = combo
            self.table.setCellWidget(index, 2, combo)
            remove = button("Remove")
            remove.setStyleSheet("padding: 3px 14px; min-height: 0px;")
            remove.setToolTip(f"Delete the rule for {game_id}; the default applies afterwards.")
            remove.clicked.connect(lambda _c=False, g=game_id: self.model.remove_game(g))
            self.table.setCellWidget(index, 3, centered(remove))
        self.table.setUpdatesEnabled(True)
        self.empty.setVisible(not games)

    def add_game(self) -> None:
        dialog = AddGameDialog(list(self.model.config.games), self.model.recent_games(), self.model.config.on_launch,
                               self)
        if self.run_dialog(dialog):
            game_id, name, action = dialog.values()
            self.model.set_game(game_id, name, action)
