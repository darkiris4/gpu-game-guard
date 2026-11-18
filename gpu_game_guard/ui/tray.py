"""The system tray application: an icon that shows the guard's state and a menu to pause it and open the settings.

`TrayController` holds all the behaviour and takes its inputs as callables, so it runs without a real tray:
`status_reader()` returns a `Status`-like object (None when the service is not running) and `TrayActions` carries
the four things the menu can do.
"""
from __future__ import annotations

import dataclasses
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..util import log
from . import qt
from .prompt import self_command

REFRESH_MS = 2000
PAUSE_HOUR_SECONDS = 3600.0
NOT_RUNNING = "GPU Game Guard is not running"


@dataclass
class TrayActions:
    pause: Callable[[Optional[float]], None]  # seconds, or None for until resumed
    resume: Callable[[], None]
    open_settings: Callable[[], None]
    quit: Callable[[], None]


def _clock_text(wall: float) -> str:
    return time.strftime("%H:%M", time.localtime(wall))


def describe(status: Any) -> Tuple[str, str, str]:
    """(icon state, menu status line, tooltip) for a status object, or for None when the service is not running."""
    if status is None:
        return "off", NOT_RUNNING, NOT_RUNNING
    if not getattr(status, "config_ok", True):
        return "attention", "Settings file has a problem", "GPU Game Guard: the settings file has a problem"
    state = getattr(status, "state", "watching")
    if getattr(status, "paused", False) or state == "paused":
        until = getattr(status, "paused_until", None)
        if until is None or until == float("inf"):
            return "paused", "Paused until resumed", "GPU Game Guard: paused until resumed"
        clock = _clock_text(until)
        return "paused", f"Paused until {clock}", f"GPU Game Guard: paused until {clock}"
    if state == "disabled":
        return "off", "Switched off in the settings", "GPU Game Guard: switched off in the settings"
    game = getattr(status, "game", None) or {}
    title = str(game.get("title") or "a game")
    stopped = len(getattr(status, "stopped", None) or [])
    if state == "asking":
        return "attention", f"Waiting for your answer about {title}", f"GPU Game Guard: waiting for your answer ({title})"
    if state == "restart-pending":
        text = f"{stopped} stopped, waiting to be restarted" if stopped else "Waiting to restart what was stopped"
        return "attention", text, f"GPU Game Guard: {text}"
    if state == "game-running":
        text = f"{title} is running" + (f"; {stopped} stopped" if stopped else "")
        return "ok", text, f"GPU Game Guard: {text}"
    return "ok", "Watching for games", "GPU Game Guard: watching for games"


class TrayController:
    """The tray icon, its menu and the logic that keeps both in step with the status."""

    def __init__(self, status_reader: Callable[[], Any], actions: TrayActions, tray: Any = None):
        QtWidgets = qt.QtWidgets
        from . import theme

        self._theme = theme
        self._reader, self.actions = status_reader, actions
        self.tray = tray if tray is not None else QtWidgets.QSystemTrayIcon()
        self.menu = QtWidgets.QMenu()
        self.state = ""
        self.tooltip = ""
        self._icons: Dict[str, Any] = {}
        self._reader_failed = False
        self.status_action = self.menu.addAction("")
        self.status_action.setEnabled(False)
        self.pause_hour_action = self.menu.addAction("Pause for 1 hour")
        self.pause_until_action = self.menu.addAction("Pause until resumed")
        self.resume_action = self.menu.addAction("Resume")
        self.menu.addSeparator()
        self.settings_action = self.menu.addAction("Open settings")
        self.quit_action = self.menu.addAction("Quit")
        self.pause_hour_action.triggered.connect(lambda: self.actions.pause(PAUSE_HOUR_SECONDS))
        self.pause_until_action.triggered.connect(lambda: self.actions.pause(None))
        self.resume_action.triggered.connect(lambda: self.actions.resume())
        self.settings_action.triggered.connect(lambda: self.actions.open_settings())
        self.quit_action.triggered.connect(lambda: self.actions.quit())
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self._timer = qt.QtCore.QTimer()
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def _activated(self, reason: Any) -> None:
        if reason == qt.QtWidgets.QSystemTrayIcon.Trigger:
            self.actions.open_settings()

    def start(self) -> None:
        self._timer.start(REFRESH_MS)
        self.tray.show()

    def read(self) -> Any:
        try:
            status = self._reader()
            self._reader_failed = False
            return status
        except Exception as exc:  # a broken status file must not stop the tray
            if not self._reader_failed:
                log(f"cannot read the status ({exc})")
            self._reader_failed = True
            return None

    def refresh(self) -> str:
        """Re-read the status and update icon, tooltip and menu. Returns the icon state."""
        status = self.read()
        state, line, tooltip = describe(status)
        if state != self.state:
            if state not in self._icons:
                self._icons[state] = self._theme.app_icon(state)
            self.tray.setIcon(self._icons[state])
            self.state = state
        self.tooltip = tooltip
        self.tray.setToolTip(tooltip)
        self.status_action.setText(line)
        paused = state == "paused"
        self.resume_action.setVisible(paused)
        return state

    def menu_summary(self) -> List[Tuple[str, bool]]:
        """(text, enabled) of the visible menu entries; a separator is ("-", False)."""
        items = []
        for action in self.menu.actions():
            if action.isSeparator():
                items.append(("-", False))
            elif action.isVisible():
                items.append((action.text(), action.isEnabled()))
        return items


def open_settings() -> None:
    """Start the settings window as a separate, detached process."""
    try:
        subprocess.Popen([*self_command(), "settings"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log(f"cannot open the settings ({exc})")


def real_status() -> Any:
    """The service's status with the pause file applied, so a pause shows without waiting for the next write."""
    from .. import status

    current = status.read_status()
    if current is None:
        return None
    is_paused, until = status.paused()
    if is_paused != bool(current.paused):
        current = dataclasses.replace(current, paused=is_paused, paused_until=until)
    return current


def run() -> int:
    """Run the tray application until Quit. Without a system tray it logs that and returns 0."""
    application = qt.app()
    from . import theme

    theme.apply_theme(application)
    if not qt.QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        log("no system tray is available; the tray icon is not started")
        return 0
    application.setQuitOnLastWindowClosed(False)
    holder: List[TrayController] = []

    def after(action: Callable[..., None]) -> Callable[..., None]:
        def wrapper(*args: Any) -> None:
            try:
                action(*args)
            except Exception as exc:  # the tray keeps running when a pause cannot be written
                log(f"tray action failed ({exc})")
            if holder:
                holder[0].refresh()
        return wrapper

    def pause(seconds: Optional[float]) -> None:
        from .. import status

        status.pause(seconds)

    def resume() -> None:
        from .. import status

        status.resume()

    controller = TrayController(real_status, TrayActions(after(pause), after(resume), open_settings,
                                                         application.quit))
    holder.append(controller)
    controller.start()
    return int(application.exec_())
