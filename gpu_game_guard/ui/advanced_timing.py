"""The Timing tab: how often the guard looks, how long it waits, and the master switches."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .advanced_common import NUMBER_RANGES, ScrolledTab, divider, quiet, setting_row
from .qt import QtCore, QtWidgets
from .theme import Card, ToggleSwitch

# key -> (title, explanation without the range, decimals)
TIMINGS: Tuple[Tuple[str, str, str, int], ...] = (
    ("poll_seconds", "Poll interval", "How often the guard looks for a game launch. Shorter reacts faster.", 2),
    ("exit_grace_seconds", "Exit grace",
     "How long a launch is given to close after the guard ends it, before it is started again.", 1),
    ("no_game_timeout_seconds", "No-game timeout",
     "How long the guard waits for a game after stopping workloads before it offers to restart them.", 0),
    ("bypass_seconds", "Bypass window",
     "After a Launch anyway answer, the same game is let through for this long.", 0),
    ("dialog_retry_seconds", "Dialog retry",
     "How long to wait before asking again when a restart question went unanswered.", 0),
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
            spin.setKeyboardTracking(False)
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
        with quiet(self.enabled_switch, self.remember_switch):
            self.enabled_switch.setChecked(config.enabled)
            self.remember_switch.setChecked(config.remember_choices)
