"""The Activity tab: a live view of the guard's status and its most recent events, refreshed every two seconds
while the tab is visible."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .advanced_common import BadgeDelegate, TabBase, bold_label, clock_text, make_table, table_item
from .qt import QtCore, QtWidgets
from .theme import SPACE, Card, Pill, styled_label

Qt = QtCore.Qt
REFRESH_MS = 2000
STATE_BADGES = {
    "watching": ("Watching", "ok"), "paused": ("Paused", "warn"), "disabled": ("Switched off", "muted"),
    "asking": ("Asking", "warn"), "game-running": ("Game running", "ok"), "restart-pending": ("Restart pending", "warn"),
}
DANGER_WORDS = ("error", "fail", "problem", "refus", "crash")
WARN_WORDS = ("ask", "pause", "skip", "bypass", "intercept", "ignore", "remember")
OK_WORDS = ("stop", "start", "restart", "resume", "answer", "launch", "adopt")


def event_kind(kind: str) -> str:
    """The badge colour for an event kind, from words in its name."""
    lowered = kind.lower()
    for words, badge in ((DANGER_WORDS, "danger"), (WARN_WORDS, "warn"), (OK_WORDS, "ok")):
        if any(w in lowered for w in words):
            return badge
    return "muted"


def ref_name(ref: dict) -> str:
    return str(ref.get("name") or ref.get("unit") or ref.get("id") or "?")


class ActivityTab(TabBase):
    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self._event_signature: List[Tuple] = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2 * SPACE, 2 * SPACE, 2 * SPACE, 2 * SPACE)
        layout.setSpacing(SPACE + 4)
        status = Card("Service", "")
        top = QtWidgets.QHBoxLayout()
        self.state_pill = Pill("", "muted")
        self.state_pill.setToolTip("What the guard is doing right now.")
        self.state_line = styled_label("", "muted")
        top.addWidget(self.state_pill)
        top.addWidget(self.state_line, 1)
        status.addLayout(top)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(2 * SPACE)
        grid.setVerticalSpacing(6)
        self.values: Dict[str, QtWidgets.QLabel] = {}
        tips = {
            "paused": "Whether the guard is paused, and until when.",
            "game": "The game running now or being asked about.",
            "stopped": "Workloads the guard stopped and has not started again.",
            "dialog": "The dialog backend and whether it can ask.",
            "config": "Whether the guard accepted the configuration file.",
        }
        for row, (key, title) in enumerate((("paused", "Paused"), ("game", "Game"), ("stopped", "Stopped by the guard"),
                                            ("dialog", "Dialog"), ("config", "Configuration"))):
            heading = bold_label(title)
            heading.setToolTip(tips[key])
            value = styled_label("", "", wrap=True)
            value.setToolTip(tips[key])
            value.setIndent(0)
            value.setMargin(0)
            self.values[key] = value
            grid.addWidget(heading, row, 0, Qt.AlignTop)
            grid.addWidget(value, row, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 150)
        status.addLayout(grid)
        layout.addWidget(status)
        events = Card("Recent events", "Newest first. The list refreshes every two seconds while this tab is open.")
        self.table = make_table(["Time", "Event", "Message"], row_height=34)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        self.table.setColumnWidth(0, 96)
        self.table.setColumnWidth(1, 140)
        self.table.setItemDelegateForColumn(1, BadgeDelegate(self.table))
        self.empty = styled_label("No events yet.", "muted")
        events.addWidget(self.table)
        events.addWidget(self.empty)
        layout.addWidget(events, 1)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event: Any) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        self._refresh_status()
        self._refresh_events()

    def _refresh_status(self) -> None:
        status = self.model.status()
        if status is None:
            self.state_pill.setText("Not running")
            self.state_pill.setKind("danger")
            self.state_line.setText("The guard service has not reported in the last few seconds.")
            for value in self.values.values():
                value.setText("-")
            return
        text, kind = STATE_BADGES.get(status.state, (status.state, "muted"))
        self.state_pill.setText(text)
        self.state_pill.setKind(kind)
        self.state_line.setText(f"Version {status.version or '?'}, process {status.pid}, {status.targets} configured "
                                f"target{'' if status.targets == 1 else 's'}.")
        if status.paused:
            until = status.paused_until
            paused = "Until resumed" if until is None or not math.isfinite(until) else "Until " + clock_text(until)
        else:
            paused = "No"
        game = status.game or {}
        stopped = ", ".join(ref_name(r) for r in status.stopped)
        self.values["paused"].setText(paused)
        self.values["game"].setText(f"{game.get('title') or game.get('id')} ({game.get('id')})" if game else "None")
        self.values["stopped"].setText(stopped or "Nothing")
        self.values["dialog"].setText(f"{status.dialog or 'none'}, "
                                      f"{'can ask' if status.can_ask else 'cannot ask'}")
        self.values["config"].setText("Accepted" if status.config_ok else f"Rejected: {status.config_error or 'unknown'}")

    def _refresh_events(self) -> None:
        events = list(reversed(self.model.events(100)))
        signature = [(e.get("t"), e.get("kind"), e.get("message")) for e in events]
        self.empty.setVisible(not events)
        if signature == self._event_signature:
            return
        self._event_signature = signature
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(events))
        for index, event in enumerate(events):
            when = event.get("t")
            kind = str(event.get("kind", ""))
            message = str(event.get("message", ""))
            self.table.setItem(index, 0, table_item(clock_text(when) if isinstance(when, (int, float)) else "-",
                                                    muted=True))
            self.table.setItem(index, 1, table_item(kind, kind=event_kind(kind)))
            self.table.setItem(index, 2, table_item(message, message))
        self.table.setUpdatesEnabled(True)
