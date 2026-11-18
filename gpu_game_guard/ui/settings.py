"""The settings window: a header with the service state and the Basic or Advanced switch, the page for the chosen
mode, and a footer with Apply, Revert and the restart notice. `run()` is the entry point of `gpu-game-guard
settings`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..util import atomic_write_json, log
from . import qt, theme
from .basic import CONTENT_WIDTH, BasicView
from .qt import QtCore, QtGui, QtWidgets
from .settings_model import (
    PILL_KIND, PILL_TEXT, START_COMMAND, STATE_DISABLED, STATE_OFF, STATE_PAUSED, SettingsModel,
    service_state)

Qt = QtCore.Qt
SPACE = theme.SPACE
MODES = ("basic", "advanced")
MIN_SIZE = (780, 600)
DEFAULT_SIZE = (900, 760)
POLL_MS = 2000
PREFS_FILE = "ui.json"
_ICON_STATE = {STATE_OFF: "off", STATE_PAUSED: "paused", STATE_DISABLED: "off"}


def load_prefs(path: Path) -> Dict[str, Any]:
    """The saved window preferences, or an empty dict when the file is missing or unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(path: Path, prefs: Dict[str, Any]) -> None:
    try:
        atomic_write_json(path, prefs)
    except OSError as exc:
        log(f"settings: cannot save the window preferences ({exc})")


class Segmented(QtWidgets.QFrame):
    """Two or more exclusive text segments in one rounded track, for switching modes."""

    changed = QtCore.pyqtSignal(str)

    class _Segment(QtWidgets.QAbstractButton):
        def __init__(self, value: str, text: str, parent: QtWidgets.QWidget):
            super().__init__(parent)
            self.value = value
            self.setText(text)
            self.setCheckable(True)
            self.setAutoExclusive(True)
            self.setCursor(Qt.PointingHandCursor)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setAccessibleName(text)

        def sizeHint(self) -> QtCore.QSize:
            return QtCore.QSize(self.fontMetrics().horizontalAdvance(self.text()) + 3 * SPACE, 30)

        def paintEvent(self, event: QtGui.QPaintEvent) -> None:
            colors = theme.current_colors()
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            rect = QtCore.QRectF(self.rect()).adjusted(0, 0, -0.5, -0.5)
            if self.isChecked():
                painter.setPen(Qt.NoPen)
                painter.setBrush(QtGui.QColor(colors.accent))
                painter.drawRoundedRect(rect, theme.RADIUS_CONTROL - 2, theme.RADIUS_CONTROL - 2)
                painter.setPen(QtGui.QColor(colors.accent_text))
            else:
                if self.underMouse():
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QtGui.QColor(colors.hover))
                    painter.drawRoundedRect(rect, theme.RADIUS_CONTROL - 2, theme.RADIUS_CONTROL - 2)
                painter.setPen(QtGui.QColor(colors.text))
            font = self.font()
            font.setWeight(QtGui.QFont.DemiBold if self.isChecked() else QtGui.QFont.Normal)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, self.text())
            if self.hasFocus():
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QtGui.QPen(QtGui.QColor(colors.text), 1.5, Qt.DotLine))
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 4, 4)

        def enterEvent(self, event: QtCore.QEvent) -> None:
            self.update()
            super().enterEvent(event)

        def leaveEvent(self, event: QtCore.QEvent) -> None:
            self.update()
            super().leaveEvent(event)

    def __init__(self, options: List[tuple], parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.segments: Dict[str, Segmented._Segment] = {}
        for value, text in options:
            segment = Segmented._Segment(value, text, self)
            segment.toggled.connect(lambda checked, v=value: checked and self.changed.emit(v))
            layout.addWidget(segment)
            self.segments[value] = segment

    def value(self) -> str:
        return next((v for v, s in self.segments.items() if s.isChecked()), "")

    def setValue(self, value: str) -> None:
        for key, segment in self.segments.items():
            segment.blockSignals(True)
            segment.setChecked(key == value)
            segment.blockSignals(False)
            segment.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        colors = theme.current_colors()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(QtGui.QColor(colors.card))
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.mix(colors.border, colors.muted, 0.3)), 1))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)


class ActionCallout(theme.Callout):
    """A `Callout` with a button beside its text."""

    def __init__(self, text: str, button_text: str, kind: str = "", parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(text, kind, parent)
        layout = self.layout()
        layout.removeWidget(self.label)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(2 * SPACE)
        row.addWidget(self.label, 1)
        self.button = theme.button(button_text)
        row.addWidget(self.button, 0, Qt.AlignVCenter)
        layout.addLayout(row)


def _ask_unsaved(window: QtWidgets.QWidget) -> str:
    """Ask what to do with unsaved changes: "save", "discard" or "cancel"."""
    box = QtWidgets.QMessageBox(window)
    box.setIcon(QtWidgets.QMessageBox.Question)
    box.setWindowTitle("Unsaved changes")
    box.setText("Save your changes before closing?")
    save = box.addButton("Save", QtWidgets.QMessageBox.AcceptRole)
    discard = box.addButton("Discard", QtWidgets.QMessageBox.DestructiveRole)
    box.addButton("Keep editing", QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(save)
    box.exec_()
    clicked = box.clickedButton()
    return "save" if clicked is save else "discard" if clicked is discard else "cancel"


class SettingsWindow(QtWidgets.QMainWindow):
    def __init__(self, model: SettingsModel, prefs_path: Optional[Path] = None):
        super().__init__()
        self.model = model
        self.prefs_path = Path(prefs_path) if prefs_path is not None else model.path.parent / PREFS_FILE
        self.prefs = load_prefs(self.prefs_path)
        self.confirm_unsaved: Callable[[QtWidgets.QWidget], str] = _ask_unsaved
        self.advanced: Optional[QtWidgets.QWidget] = None
        self.advanced_error = ""
        self.setWindowTitle("GPU Game Guard Settings")
        self.setWindowIcon(theme.app_icon("ok"))
        self.setMinimumSize(*MIN_SIZE)
        size = self.prefs.get("size")
        if (isinstance(size, list) and len(size) == 2 and all(isinstance(n, int) and not isinstance(n, bool)
                                                               for n in size)):
            self.resize(max(size[0], MIN_SIZE[0]), max(size[1], MIN_SIZE[1]))
        else:
            self.resize(*DEFAULT_SIZE)

        root = QtWidgets.QWidget()
        root.setObjectName("Window")
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        layout.addWidget(self._divider())

        self.notices = QtWidgets.QWidget()
        centre = QtWidgets.QHBoxLayout(self.notices)
        centre.setContentsMargins(3 * SPACE, 2 * SPACE, 3 * SPACE, 0)
        column = QtWidgets.QWidget()
        column.setMaximumWidth(CONTENT_WIDTH)
        centre.addStretch(1)
        centre.addWidget(column, 100)
        centre.addStretch(1)
        notices = QtWidgets.QVBoxLayout(column)
        notices.setContentsMargins(0, 0, 0, 0)
        notices.setSpacing(SPACE)
        self.load_callout = ActionCallout("", "Start from the defaults", "warn")
        self.load_callout.button.clicked.connect(self.model.accept_defaults)
        self.not_running = self._build_not_running()
        notices.addWidget(self.load_callout)
        notices.addWidget(self.not_running)
        layout.addWidget(self.notices)

        self.stack = QtWidgets.QStackedWidget()
        self.basic = BasicView(model)
        self.basic.advancedRequested.connect(lambda: self.set_mode("advanced"))
        self.stack.addWidget(self.basic)
        layout.addWidget(self.stack, 1)

        layout.addWidget(self._divider())
        layout.addWidget(self._build_footer())

        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+S"), self, activated=self.apply)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.refresh)
        model.changed.connect(self.refresh)

        mode = self.prefs.get("mode")
        self.mode = "basic"
        self.set_mode(mode if mode in MODES else "basic", save=False)
        self.refresh()

    # construction

    @staticmethod
    def _divider() -> QtWidgets.QFrame:
        line = QtWidgets.QFrame()
        line.setObjectName("Divider")
        return line

    def _build_header(self) -> QtWidgets.QWidget:
        header = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(header)
        row.setContentsMargins(3 * SPACE, 2 * SPACE, 3 * SPACE, 2 * SPACE)
        row.setSpacing(SPACE + 4)
        self.logo = QtWidgets.QLabel()
        self.logo.setFixedSize(36, 36)
        row.addWidget(self.logo)
        row.addWidget(theme.styled_label("GPU Game Guard", "headline", wrap=False))
        self.pill = theme.Pill("Watching", "ok")
        row.addWidget(self.pill, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self.switcher = Segmented([("basic", "Basic"), ("advanced", "Advanced")])
        self.switcher.changed.connect(self.set_mode)
        row.addWidget(self.switcher)
        return header

    def _build_not_running(self) -> theme.Card:
        card = theme.Card("Start the service",
                          "GPU Game Guard watches for games in the background. Run this command in a terminal; "
                          "this window notices by itself once it is running.")
        row = QtWidgets.QHBoxLayout()
        self.command_label = QtWidgets.QLabel(START_COMMAND)
        self.command_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        mono.setFamily("monospace")
        mono.setStyleHint(QtGui.QFont.Monospace)
        self.command_label.setFont(mono)
        row.addWidget(self.command_label, 1)
        self.copy_button = theme.button("Copy")
        self.copy_button.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(START_COMMAND))
        row.addWidget(self.copy_button)
        card.addLayout(row)
        return card

    def _build_footer(self) -> QtWidgets.QWidget:
        footer = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(footer)
        column.setContentsMargins(3 * SPACE, SPACE + 4, 3 * SPACE, SPACE + 4)
        column.setSpacing(SPACE)
        self.problems_callout = theme.Callout("", "danger")
        self.problems_callout.label.setTextFormat(Qt.PlainText)
        self.restart_banner = ActionCallout("Some changes take effect after the service restarts.",
                                            "Restart service", "warn")
        self.restart_banner.button.clicked.connect(self.restart_service)
        self.problems_callout.setVisible(False)
        self.restart_banner.setVisible(False)
        column.addWidget(self.problems_callout)
        column.addWidget(self.restart_banner)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(SPACE)
        self.save_label = theme.styled_label("", "muted", wrap=False)
        row.addWidget(self.save_label, 1)
        self.revert_button = theme.button("Revert")
        self.apply_button = theme.button("Apply", "primary")
        self.revert_button.clicked.connect(self.revert)
        self.apply_button.clicked.connect(self.apply)
        row.addWidget(self.revert_button)
        row.addWidget(self.apply_button)
        column.addLayout(row)
        return footer

    # behaviour

    def set_mode(self, mode: str, save: bool = True) -> None:
        if mode not in MODES:
            return
        if mode == "advanced" and self.advanced is None:
            self.advanced = self._make_advanced()
            self.stack.addWidget(self.advanced)
        self.mode = mode
        self.stack.setCurrentWidget(self.advanced if mode == "advanced" and self.advanced is not None
                                    else self.basic)
        self.switcher.setValue(mode)
        if save:
            self.save_prefs()

    def _make_advanced(self) -> QtWidgets.QWidget:
        try:
            from .advanced import AdvancedView

            return AdvancedView(self.model)
        except Exception as exc:  # a missing or broken module must not take Basic down with it
            self.advanced_error = str(exc) or type(exc).__name__
            log(f"settings: Advanced mode is unavailable ({self.advanced_error})")
            page = QtWidgets.QWidget()
            page.setObjectName("Window")
            box = QtWidgets.QVBoxLayout(page)
            box.setContentsMargins(3 * SPACE, 2 * SPACE, 3 * SPACE, 2 * SPACE)
            box.addWidget(theme.Callout("Advanced mode is unavailable in this installation. "
                                        "Basic mode still works.", "warn"))
            box.addStretch(1)
            return page

    def refresh(self) -> None:
        """Update everything that follows the model and the service: pill, icon, notices and footer."""
        state, _until = service_state(self.model)
        self.pill.setText(PILL_TEXT[state])
        self.pill.setKind(PILL_KIND[state])
        self.logo.setPixmap(theme.app_icon(_ICON_STATE.get(state, "ok")).pixmap(32, 32))
        self.not_running.setVisible(state == STATE_OFF)
        self.load_callout.setVisible(bool(self.model.load_error))
        if self.model.load_error:
            self.load_callout.label.setText(f"The settings file could not be read: {self.model.load_error}. "
                                            "It was left untouched.")
        self.restart_banner.setVisible(self.model.restart_needed())
        dirty = self.model.dirty
        self.apply_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        if dirty:
            self.save_label.setText("You have unsaved changes")
        elif self.save_label.text() == "You have unsaved changes":
            self.save_label.setText("")
        self.basic.refresh_status()

    def apply(self) -> bool:
        problems = self.model.apply()
        if problems:
            self.show_problems(problems)
            return False
        self.problems_callout.setVisible(False)
        self.save_label.setText("Saved")
        self.refresh()
        return True

    def show_problems(self, problems: List[str]) -> None:
        self.problems_callout.label.setText("These settings were not saved:\n" + "\n".join(f"- {p}" for p in problems))
        self.problems_callout.setVisible(True)

    def revert(self) -> None:
        self.model.revert()
        self.problems_callout.setVisible(False)
        self.save_label.setText("")
        self.refresh()

    def restart_service(self) -> None:
        ok, message = self.model.restart_service()
        if ok:
            self.problems_callout.setVisible(False)
            self.save_label.setText("Service restarted")
        else:
            self.show_problems([f"The service could not be restarted: {message}"])
        self.refresh()

    def save_prefs(self) -> None:
        self.prefs.update(mode=self.mode, size=[self.width(), self.height()])
        save_prefs(self.prefs_path, self.prefs)

    # window events

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.model.dirty:
            choice = self.confirm_unsaved(self)
            if choice == "cancel" or (choice == "save" and not self.apply()):
                event.ignore()
                return
        self.save_prefs()
        super().closeEvent(event)


def run() -> int:
    app = qt.app()
    theme.apply_theme(app)
    app.setWindowIcon(theme.app_icon("ok"))
    window = SettingsWindow(SettingsModel())
    window.show()
    return app.exec_()
