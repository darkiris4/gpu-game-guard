"""The Timing tab: how often the guard looks, how long it waits, and the master switches."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .advanced_common import NUMBER_RANGES, ScrolledTab, divider, quiet, set_switch, setting_row
from .qt import QtCore, QtWidgets
from .theme import Card, ToggleSwitch

# key -> (title, explanation without the range, decimals)
TIMINGS: Tuple[Tuple[str, str, str, int], ...] = (
    ("poll_seconds", "Poll interval",
     "How often the guard looks for a game launch and for a game that is still running. Shorter reacts faster.", 2),
    ("exit_grace_seconds", "Exit grace",
     "Once a game has been seen after the guard stopped workloads, how long no game may be running before the "
     "guard starts them again or asks about it.", 1),
    ("no_game_timeout_seconds", "No-game timeout",
     "When no game has been seen since the guard stopped workloads, how long it waits before it starts them again "
     "or asks about it.", 0),
    ("bypass_seconds", "Bypass window",
     "After the guard has handled a launch (stopped workloads or let the game through) and started the game again, "
     "the same game is not interrupted again for this long. Not applied after Cancel.", 0),
    ("dialog_retry_seconds", "Dialog retry",
     "How long to wait before trying again when the restart question could not be shown or a workload could not be "
     "started.", 0),
)


def range_text(key: str) -> str:
    _kind, low, high = NUMBER_RANGES[key]
    return f"{low:g} to {high:g} seconds"


class TimingTab(ScrolledTab):
    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None):
        super().__init__(model, parent)
        self.spins: Dict[str, QtWidgets.QDoubleSpinBox] = {}
        timing = Card("Timing", "All values are in seconds. They take effect once the settings are saved.")
        for index, (key, title, explanation, decimals) in enumerate(TIMINGS):
            _kind, low, high = NUMBER_RANGES[key]
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(float(low), float(high))
            spin.setDecimals(decimals)
            spin.setSuffix(" s")
            spin.setKeyboardTracking(True)
            spin.setAccessibleName(f"{title} in seconds")
            spin.setMinimumWidth(150)
            spin.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.spins[key] = spin
            tip = f"{key}: {explanation} Allowed: {range_text(key)}."
            if index:
                timing.addWidget(divider())
            timing.addWidget(setting_row(title, f"{explanation} Range {range_text(key)}.", spin, tip))
            spin.valueChanged.connect(lambda value, k=key: self.model.update(**{k: float(value)}))
        self.column.addWidget(timing)

        switches = Card("Behaviour", "")
        self.enabled_switch = ToggleSwitch(True, accessible_name="Guard enabled")
        self.remember_switch = ToggleSwitch(True, accessible_name="Offer to remember choices")
        switches.addWidget(setting_row(
            "Guard enabled", "The master switch. When off the guard only watches and never interrupts a launch.",
            self.enabled_switch, "enabled: false means watch only."))
        switches.addWidget(divider())
        switches.addWidget(setting_row(
            "Remember choices", "Offer a tick box in the dialog to remember the answer for that game.",
            self.remember_switch, "remember_choices: show the remember option in the launch dialog."))
        self.column.addWidget(switches)
        self.finish()
        self.enabled_switch.toggled.connect(lambda on: self.model.update(enabled=on))
        self.remember_switch.toggled.connect(lambda on: self.model.update(remember_choices=on))
        self.refresh()

    def refresh(self) -> None:
        config = self.model.config
        for key, spin in self.spins.items():
            value = float(getattr(config, key))
            with quiet(spin):
                if spin.value() != value:
                    spin.setValue(value)
        set_switch(self.enabled_switch, config.enabled)
        set_switch(self.remember_switch, config.remember_choices)
