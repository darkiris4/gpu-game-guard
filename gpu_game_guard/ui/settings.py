"""The settings window: a header with the service state and the Basic or Advanced switch, the page for the chosen
mode, and a footer with Apply, Revert and the restart notice. `run()` is the entry point of `gpu-game-guard
settings`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import __version__
from ..util import atomic_write_json, log
from . import qt, singleton, theme
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
SIZE_RANGE = (200, 10000)  # the window sizes a preferences file may hold
MAX_PROBLEMS_SHOWN = 8
RESTART_FAILED = "Settings saved, but the service could not be restarted:"
NOT_SAVED = "These settings were not saved:"
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
    """A `Callout` with a button beside its text, or below it when `stacked`; `add_button` puts more beside it."""

    def __init__(self, text: str, button_text: str, kind: str = "", parent: Optional[QtWidgets.QWidget] = None,
                 stacked: bool = False):
        super().__init__(text, kind, parent)
        layout = self.layout()
        self._row = QtWidgets.QHBoxLayout()
        self._row.setSpacing(SPACE)
        if stacked:
            self._row.addStretch(1)
        else:
            layout.removeWidget(self.label)
            self._row.setSpacing(2 * SPACE)
            self._row.addWidget(self.label, 1)
        self.button = theme.button(button_text)
        self._row.addWidget(self.button, 0, Qt.AlignVCenter)
        layout.addLayout(self._row)

    def add_button(self, text: str) -> QtWidgets.QPushButton:
        extra = theme.button(text)
        self._row.addWidget(extra, 0, Qt.AlignVCenter)
        return extra


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


def _confirm_defaults(window: QtWidgets.QWidget) -> bool:
    """Ask whether to replace an unusable settings file with the defaults."""
    box = QtWidgets.QMessageBox(window)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle("Start from the defaults")
    box.setTextFormat(Qt.PlainText)
    box.setText("Start from the default settings? Your games and targets in the unusable file are not carried "
                "over. Applying replaces the file; when it can be read, the old one is kept next to it as a .broken "
                "copy.")
    start = box.addButton("Start from the defaults", QtWidgets.QMessageBox.DestructiveRole)
    keep = box.addButton("Keep the file", QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(keep)
    box.exec_()
    return box.clickedButton() is start


class SettingsWindow(QtWidgets.QMainWindow):
    def __init__(self, model: SettingsModel, prefs_path: Optional[Path] = None):
        super().__init__()
        self.model = model
        self.prefs_path = Path(prefs_path) if prefs_path is not None else model.path.parent / PREFS_FILE
        self.prefs = load_prefs(self.prefs_path)
        self.confirm_unsaved: Callable[[QtWidgets.QWidget], str] = _ask_unsaved
        self.confirm_defaults: Callable[[QtWidgets.QWidget], bool] = _confirm_defaults
        self.notice_text = ""
        self.mode = "basic"
        self._shape: tuple = ()
        self.advanced: Optional[QtWidgets.QWidget] = None
        self.advanced_error = ""
        self.setWindowTitle("GPU Game Guard Settings")
        self.setWindowIcon(theme.app_icon("ok"))
        self.setMinimumSize(*MIN_SIZE)
        size = self.prefs.get("size")
        if (isinstance(size, list) and len(size) == 2
                and all(isinstance(n, int) and not isinstance(n, bool) and SIZE_RANGE[0] <= n <= SIZE_RANGE[1]
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
        centre = self._notice_margins = QtWidgets.QHBoxLayout(self.notices)
        centre.setContentsMargins(3 * SPACE, 2 * SPACE, 3 * SPACE, 0)
        column = QtWidgets.QWidget()
        column.setMaximumWidth(CONTENT_WIDTH)
        centre.addStretch(1)
        centre.addWidget(column, 100)
        centre.addStretch(1)
        notices = QtWidgets.QVBoxLayout(column)
        notices.setContentsMargins(0, 0, 0, 0)
        notices.setSpacing(SPACE)
        self.load_callout = ActionCallout("", "Open in Raw editor to fix", "warn", stacked=True)
        self.load_callout.button.clicked.connect(self.show_raw)
        self.defaults_button = self.load_callout.add_button("Start from the defaults")
        self.defaults_button.clicked.connect(self.start_from_defaults)
        self.not_running = self._build_not_running()
        notices.addWidget(self.load_callout)
        notices.addWidget(self.not_running)
        layout.addWidget(self.notices)

        self.stack = QtWidgets.QStackedWidget()
        self.basic = BasicView(model)
        self.basic.advancedRequested.connect(lambda: self.set_mode("advanced"))
        self.stack.addWidget(self.basic)
        self.basic.scroll.verticalScrollBar().rangeChanged.connect(self._scroll_range_changed)
        layout.addWidget(self.stack, 1)

        layout.addWidget(self._divider())
        layout.addWidget(self._build_footer())

        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+S"), self, activated=self.apply)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.tick)
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
        self.command_label = QtWidgets.QLabel()
        self.command_label.setTextFormat(Qt.PlainText)
        self.command_label.setText(START_COMMAND)
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
        self.notice_callout = theme.Callout("", "")
        self.notice_callout.setVisible(False)
        column.addWidget(self.notice_callout)
        self.restart_banner = ActionCallout("Some changes take effect after the service restarts.",
                                            "Restart service", "warn")
        self.restart_banner.button.clicked.connect(self.restart_service)
        self.problems_callout.setVisible(False)
        self.restart_banner.setVisible(False)
        column.addWidget(self.problems_callout)
        column.addWidget(self.restart_banner)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(SPACE)
        self.version_label = theme.styled_label(f"Version {__version__}", "small", wrap=False)
        self.version_label.setAccessibleName(f"Version {__version__}")
        self.about_button = theme.button("About")
        self.about_button.setProperty("flat", "true")
        self.about_button.setAccessibleName("About GPU Game Guard")
        self.about_button.clicked.connect(self.show_about)
        row.addWidget(self.version_label)
        row.addWidget(self.about_button)
        row.addSpacing(SPACE)
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
        self.align_notices()
        self.fit_minimum()
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

    def _scroll_range_changed(self, _low: int, _high: int) -> None:
        self.align_notices()

    def align_notices(self) -> None:
        """Give the notices the margins of the Basic page's content, which are narrower by the width of its scroll
        bar whenever that is showing, so that the cards above and below the tabs line up."""
        extent = self.basic.scrollbar_extent() if self.mode == "basic" else 0
        self._notice_margins.setContentsMargins(3 * SPACE, 2 * SPACE, 3 * SPACE + extent, 0)

    def screen_limit(self) -> Optional[QtCore.QSize]:
        """The largest minimum size that still fits the screen, or None when there is no screen."""
        screen = QtWidgets.QApplication.primaryScreen()
        return screen.availableGeometry().size() if screen is not None else None

    def fit_minimum(self, limit: Optional[QtCore.QSize] = None) -> None:
        """Raise the minimum window size to what the layout needs at the current font, so that buttons and tab
        titles are never clipped. The size never falls below `MIN_SIZE` and never exceeds `limit`, which is the
        available screen unless given."""
        layout = self.centralWidget().layout()
        self.setMinimumSize(0, 0)
        need = layout.minimumSize()
        width, height = max(MIN_SIZE[0], need.width()), max(MIN_SIZE[1], need.height())
        limit = limit if limit is not None else self.screen_limit()
        if limit is not None:
            width, height = min(width, max(MIN_SIZE[0], limit.width())), min(height, max(MIN_SIZE[1], limit.height()))
        self.setMinimumSize(width, height)
        if self.width() < width or self.height() < height:
            self.resize(max(self.width(), width), max(self.height(), height))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.align_notices()

    def changeEvent(self, event: QtCore.QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.FontChange, QtCore.QEvent.StyleChange):
            QtCore.QTimer.singleShot(0, self.fit_minimum)

    def refresh(self) -> None:
        """Update everything that follows the model and the service: pill, icon, notices and footer."""
        state, _until = service_state(self.model)
        self.pill.setText(PILL_TEXT[state])
        self.pill.setKind(PILL_KIND[state])
        self.logo.setPixmap(theme.app_icon(_ICON_STATE.get(state, "ok")).pixmap(32, 32))
        self.not_running.setVisible(state == STATE_OFF)
        broken = bool(self.model.load_error)
        self.load_callout.setVisible(broken)
        if broken:
            shown = list(self.model.load_problems or [self.model.load_error])
            lines = [f"- {p}" for p in shown[:MAX_PROBLEMS_SHOWN]]
            if len(shown) > MAX_PROBLEMS_SHOWN:
                lines.append(f"- and {len(shown) - MAX_PROBLEMS_SHOWN} more")
            self.load_callout.label.setText(theme.safe_text(
                "The settings file could not be used and was left untouched:\n" + "\n".join(lines)
                + "\nFix it in the Raw config editor, or start from the defaults, which replaces the file "
                "and keeps the unusable one next to it as a .broken copy when it can be read.", keep_newlines=True))
            self.load_callout.button.setVisible(bool(self.model.raw_file_text))
        notice = self.notice_text
        if getattr(self.model, "defaults_pending", False):
            notice = ("Applying replaces the unusable settings file with the defaults. When the old file can be "
                      "read, it is kept next to it as a .broken-<time> copy.")
        self.notice_callout.label.setText(theme.safe_text(notice, keep_newlines=True))
        self.notice_callout.setVisible(bool(notice))
        self.restart_banner.setVisible(self.model.restart_needed())
        shape = (self.not_running.isHidden(), self.load_callout.isHidden(), self.notice_callout.isHidden(),
                 self.restart_banner.isHidden(), self.problems_callout.isHidden())
        if shape != self._shape and self.isVisible():
            self._shape = shape
            QtCore.QTimer.singleShot(0, self.fit_minimum)
        dirty = self.model.dirty
        self.apply_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)
        if dirty:
            self.save_label.setText("You have unsaved changes")
        elif self.save_label.text() == "You have unsaved changes":
            self.save_label.setText("")
        self.basic.refresh_status()

    def tick(self) -> None:
        """The periodic update: pick up a change another program made to the file, then refresh."""
        self.model.sync_with_disk()
        self.refresh()

    def apply(self) -> bool:
        problems = self.model.apply()
        if problems:
            self.show_problems(problems)
            return False
        self.problems_callout.setVisible(False)
        self.notice_text = "\n".join(self.model.notices)
        self.save_label.setText("Saved")
        self.refresh()
        return True

    def show_problems(self, problems: List[str], title: str = NOT_SAVED) -> None:
        self.problems_callout.label.setText(theme.safe_text(title + "\n" + "\n".join(f"- {p}" for p in problems),
                                                            keep_newlines=True))
        self.problems_callout.setVisible(True)

    def revert(self) -> None:
        self.model.revert()
        self.problems_callout.setVisible(False)
        self.notice_text = ""
        self.save_label.setText("")
        self.refresh()

    def restart_service(self) -> None:
        ok, message = self.model.restart_service()
        if ok:
            self.problems_callout.setVisible(False)
            self.save_label.setText("Service restarted")
        else:
            self.show_problems([message], RESTART_FAILED)
        self.refresh()

    def show_about(self) -> None:
        from . import about

        about.show_about(self)

    def show_raw(self) -> None:
        """Open the Raw config tab of Advanced mode."""
        self.set_mode("advanced")
        show = getattr(self.advanced, "show_tab", None)
        if callable(show):
            show("Raw config")

    def start_from_defaults(self) -> None:
        if self.confirm_defaults(self):
            self.model.accept_defaults()

    def save_prefs(self) -> None:
        self.prefs.update(mode=self.mode, size=[self.width(), self.height()])
        save_prefs(self.prefs_path, self.prefs)

    # window events

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self.fit_minimum()
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
        self.model.shutdown()
        super().closeEvent(event)


def run(config_path: Optional[Path] = None) -> int:
    """Open the settings window and run until it closes. `config_path` names the configuration file; the default
    is the user's. A second window is not opened while one is already open."""
    lock = singleton.acquire("settings")
    if lock is None:
        log("settings: the settings window is already open")
        return 0
    try:
        app = qt.app()
        theme.apply_theme(app)
        app.setWindowIcon(theme.app_icon("ok"))
        window = SettingsWindow(SettingsModel(config_path))
        window.show()
        return app.exec_()
    finally:
        lock.release()
