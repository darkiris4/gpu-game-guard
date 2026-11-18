"""The Dialogs tab: which backend asks the questions, which are installed here, and which launchers are watched."""
from __future__ import annotations

import shutil
from typing import Any, Callable, Dict, Optional

from .. import dialogs
from ..config import DIALOGS
from . import qt
from .advanced_common import ListEditor, RestartNote, ScrolledTab, bold_label, quiet, setting_row
from .qt import QtCore, QtWidgets
from .theme import SPACE, Badge, Card, styled_label

BACKEND_NOTES = {
    "auto": "Picks the best installed backend: the Qt window, then zenity or kdialog, then notifications.",
    "qt": "The built-in window with a card per workload. Needs python3-pyqt5 and a display.",
    "zenity": "Plain GTK dialogs through zenity.",
    "kdialog": "Plain KDE dialogs through kdialog.",
    "notify": "Desktop notifications with buttons, through notify-send. Not every desktop shows the buttons.",
    "none": "Never ask. Launches are left alone and nothing is stopped automatically.",
}
TRIGGER_NOTES = "steam is the launcher the guard can watch today."


def trigger_problem(text: str) -> str:
    if not text:
        return "Enter a launcher name such as steam."
    if not text.replace("-", "").replace("_", "").isalnum() or text != text.lower():
        return "Use lowercase letters, digits, - and _ only."
    return ""


def installed_backends(which: Callable[[str], Optional[str]] = shutil.which,
                       qt_available: Callable[[], bool] = qt.available) -> Dict[str, bool]:
    """Whether each backend can be used on this machine: the external ones when their command exists."""
    found = {"auto": True, "none": True, "qt": bool(qt_available())}
    for name, prompter in (("zenity", dialogs.ZenityPrompter), ("kdialog", dialogs.KdialogPrompter),
                           ("notify", dialogs.NotifyPrompter)):
        found[name] = bool(prompter(which).available())
    return found


class DialogsTab(ScrolledTab):
    def __init__(self, model: Any, which: Callable[[str], Optional[str]] = shutil.which,
                 qt_available: Callable[[], bool] = qt.available, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self._which, self._qt_available = which, qt_available
        self.backends = installed_backends(which, qt_available)
        backend = Card("Dialog backend", "How the guard asks what to do about a launch.")
        self.combo = QtWidgets.QComboBox()
        self.combo.setMinimumWidth(190)
        for name in DIALOGS:
            self.combo.addItem(name + ("" if self.backends.get(name) else "  (not installed)"), name)
            self.combo.setItemData(self.combo.count() - 1, BACKEND_NOTES[name], QtCore.Qt.ToolTipRole)
        backend.addWidget(setting_row("Backend", "auto is right for most setups.", self.combo,
                                      "dialog: the backend that shows launch and restart questions."))
        self.note = styled_label("", "small")
        self.note.setVisible(False)
        backend.addWidget(self.note)
        self.badges: Dict[str, Badge] = {}
        for name in DIALOGS:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(SPACE)
            title = bold_label(name)
            title.setFixedWidth(72)
            always = name in ("auto", "none")
            badge = Badge("Built in" if always else "Installed" if self.backends[name] else "Not installed",
                          "ok" if self.backends[name] else "muted")
            badge.setFixedWidth(96)
            badge.setAlignment(QtCore.Qt.AlignCenter)
            self.badges[name] = badge
            row.addWidget(title)
            row.addWidget(badge)
            row.addWidget(styled_label(BACKEND_NOTES[name], "small"), 1)
            holder = QtWidgets.QWidget()
            holder.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            holder.setToolTip(BACKEND_NOTES[name])
            backend.addWidget(holder)
        self.restart_note = RestartNote(self.model)
        backend.addWidget(self.restart_note)
        self.column.addWidget(backend)

        launchers = Card("Launchers", "What the guard watches for game launches.")
        self.triggers = ListEditor("steam", trigger_problem)
        launchers.addWidget(setting_row("Triggers", TRIGGER_NOTES + " Changing them needs a service restart.",
                                        self.triggers, "triggers: the launchers to watch, such as steam.", wide=True))
        self.column.addWidget(launchers)
        self.finish()
        self.combo.currentIndexChanged.connect(lambda _i: self.model.update(dialog=self.combo.currentData()))
        self.triggers.changed.connect(lambda items: self.model.update(triggers=items))
        self.refresh()

    def refresh(self) -> None:
        config = self.model.config
        with quiet(self.combo):
            index = self.combo.findData(config.dialog)
            if index >= 0:
                self.combo.setCurrentIndex(index)
        chosen = config.dialog
        missing = not self.backends.get(chosen, False)
        self.note.setVisible(missing)
        if missing:
            self.note.setText(f"{chosen} is not installed here, so the guard falls back to automatic choice.")
        self.triggers.set_items(list(config.triggers))
        self.restart_note.refresh()

