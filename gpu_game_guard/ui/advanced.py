"""The Advanced mode of the settings window: one tab per area of the configuration, for people who know what
the settings do. Every tab reads from and writes to the settings model only and reloads when the model changes."""
from __future__ import annotations

import shutil
from typing import Any, Callable, List, Optional

from . import qt
from .advanced_activity import ActivityTab
from .advanced_common import CandidateSource, pick_open_file, pick_save_file
from .advanced_detection import DetectionTab
from .advanced_dialogs import DialogsTab
from .advanced_games import GamesTab
from .advanced_raw import RawTab
from .advanced_targets import TargetsTab
from .advanced_testrun import TestRunTab
from .advanced_timing import TimingTab
from .qt import QtCore, QtWidgets

TAB_TITLES = ("Targets", "Games", "Detection", "Timing", "Dialogs", "Activity", "Test run", "Raw config")


class AdvancedView(QtWidgets.QWidget):
    """The tabs of Advanced mode. `model` is the settings model. The keyword arguments replace what touches the
    machine: `which` finds installed dialog programs, `polkit_status` reads the polkit rule state, `qt_available`
    tells whether the Qt dialog could run, and `file_dialogs` supplies the open and save pickers of the Raw tab.
    Discovery of running workloads is deferred to the event loop unless `defer_discovery` is false."""

    def __init__(self, model: Any, parent: Optional["QtWidgets.QWidget"] = None, *,
                 which: Callable[[str], Optional[str]] = shutil.which,
                 polkit_status: Optional[Callable[[str], Optional[bool]]] = None,
                 qt_available: Callable[[], bool] = qt.available,
                 file_dialogs: Optional[tuple] = None, defer_discovery: bool = True):
        super().__init__(parent)
        self.model = model
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)
        self.source = CandidateSource(model, defer_discovery)
        self.targets = TargetsTab(model, self.source, polkit_status)
        self.games = GamesTab(model)
        self.detection = DetectionTab(model)
        self.timing = TimingTab(model)
        self.dialogs = DialogsTab(model, which, qt_available)
        self.activity = ActivityTab(model)
        self.test_run = TestRunTab(model, self.source)
        self.raw = RawTab(model)
        opener, saver = file_dialogs or (pick_open_file, pick_save_file)
        self.raw.pick_open, self.raw.pick_save = opener, saver
        for title, tab in zip(TAB_TITLES, self.pages()):
            self.tabs.addTab(tab, title)
        self.tabs.setTabToolTip(0, "Everything the guard manages")
        self.tabs.setTabToolTip(1, "Per-game rules")
        self.tabs.setTabToolTip(2, "What is detected, ignored and protected")
        self.tabs.setTabToolTip(3, "Polling, waiting and the master switches")
        self.tabs.setTabToolTip(4, "How questions are asked")
        self.tabs.setTabToolTip(5, "The guard's live status and recent events")
        self.tabs.setTabToolTip(6, "Preview what would happen for a game, without doing it")
        self.tabs.setTabToolTip(7, "The configuration file as JSON")
        if defer_discovery:
            QtCore.QTimer.singleShot(0, self.source.load)

    def pages(self) -> List["QtWidgets.QWidget"]:
        return [self.targets, self.games, self.detection, self.timing, self.dialogs, self.activity, self.test_run,
                self.raw]

    @staticmethod
    def tab_names() -> List[str]:
        return list(TAB_TITLES)

    def current_name(self) -> str:
        return TAB_TITLES[self.tabs.currentIndex()]

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(TAB_TITLES.index(name))

    def refresh(self) -> None:
        for page in self.pages():
            page.refresh()
