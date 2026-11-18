"""The state behind the settings window: a working copy of the configuration with dirty tracking, validation and
saving, plus the live views the window needs (status, events, discovered candidates, pause state, service restart).
Everything outside the change signal works without a QApplication, and the file, status directory, discovery and
command runner are injectable so tests never touch the real system."""
from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from .. import config as configmod
from .. import discovery, guard, gpu, runtimes, status as statusmod
from ..config import Config, ConfigError, TargetSpec
from ..discovery import Candidate
from ..model import Target
from ..status import Status
from ..util import log
from .qt import QtCore

SERVICE_UNIT = "gpu-game-guard.service"
RESTART_COMMAND = ["systemctl", "--user", "restart", SERVICE_UNIT]
START_COMMAND = "systemctl --user start gpu-game-guard"
# Settings that the running service reads once at start; a saved change to one of them needs a restart.
_RESTART_FIELDS = ("runtimes", "triggers", "dialog")

Runner = Callable[[List[str]], Tuple[int, str]]
Discover = Callable[[Config], List[Candidate]]


def _default_discover(config: Config) -> List[Candidate]:
    return discovery.discover(config, runtimes.build(config.runtimes, get_config=lambda: config))


def _default_runner(argv: List[str]) -> Tuple[int, str]:
    result = gpu.run_process(argv, timeout=30)
    if result is None:
        return 127, "the command could not be run"
    return result.returncode, (result.stdout + result.stderr).strip()


def _restart_key(config: Config) -> Tuple[Any, ...]:
    return tuple(tuple(getattr(config, name)) if isinstance(getattr(config, name), list) else getattr(config, name)
                 for name in _RESTART_FIELDS)


class SettingsModel(QtCore.QObject):
    changed = QtCore.pyqtSignal()  # working config, saved state, status or candidates changed

    def __init__(self, path: Optional[Path] = None, status_dir: Optional[Path] = None,
                 discover: Optional[Discover] = None, runner: Optional[Runner] = None,
                 parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.path = Path(path) if path is not None else configmod.config_path()
        self.status_dir = Path(status_dir) if status_dir is not None else None
        self._discover = discover or _default_discover
        self._runner = runner or _default_runner
        self.load_error = ""
        self._saved = self._load()
        self._working = copy.deepcopy(self._saved)
        self.problems: List[str] = []
        self._refresh_problems()
        self._restart_baseline = _restart_key(self._saved)
        self._candidates: Optional[List[Candidate]] = None
        self._defaults_accepted = False  # a broken file was given up on; the next save replaces it

    def _load(self) -> Config:
        try:
            return configmod.load(self.path)
        except ConfigError as exc:
            self.load_error = str(exc)
        except Exception as exc:  # an unexpected failure reading the file is treated like an unreadable file
            self.load_error = f"{self.path}: {exc}"
        return Config()

    # state

    @property
    def config(self) -> Config:
        return self._working

    @property
    def saved(self) -> Config:
        return self._saved

    @property
    def dirty(self) -> bool:
        return self._working != self._saved or self._defaults_accepted

    def _refresh_problems(self) -> None:
        self.problems = configmod.validate(self._working.to_json())

    def _replace(self, cfg: Config) -> None:
        self._working = cfg
        self._refresh_problems()
        self.changed.emit()

    def accept_defaults(self) -> None:
        """Give up on a file that could not be read: the working copy stays at the defaults, and the next apply
        replaces the file with it."""
        if self.load_error:
            self.load_error = ""
            self._defaults_accepted = True
            self.changed.emit()

    def set_config(self, cfg: Config) -> None:
        self._replace(copy.deepcopy(cfg))

    def update(self, **fields: Any) -> None:
        self._replace(dataclasses.replace(copy.deepcopy(self._working), **fields))

    def set_target(self, spec: TargetSpec) -> None:
        self._replace(self._working.with_target(spec))

    def remove_target(self, target_id: str) -> None:
        self._replace(self._working.without_target(target_id))

    def set_game(self, game_id: str, name: str, action: str) -> None:
        self._replace(self._working.with_game(game_id, name, action))

    def remove_game(self, game_id: str) -> None:
        updated = copy.deepcopy(self._working)
        updated.games.pop(game_id, None)
        self._replace(updated)

    def apply(self) -> List[str]:
        """Validate and save the working copy. Returns the problems; an empty list means it was saved."""
        self._refresh_problems()
        problems = list(self.problems)
        if self.load_error:
            problems.insert(0, f"The settings file could not be read ({self.load_error}). Saving would replace it "
                               "with the current settings; choose to start from the defaults first.")
        if problems:
            return problems
        try:
            configmod.save(self._working, self.path)
        except (OSError, ValueError) as exc:
            return [f"The settings could not be saved: {exc}"]
        self._saved = copy.deepcopy(self._working)
        self._defaults_accepted = False
        self.changed.emit()
        return []

    def revert(self) -> None:
        self._defaults_accepted = False
        self._replace(copy.deepcopy(self._saved))

    # discovery

    def candidates(self, refresh: bool = False) -> List[Candidate]:
        """What could be paused, cached after the first lookup; `refresh` looks again."""
        if self._candidates is None or refresh:
            try:
                self._candidates = list(self._discover(self._working))
            except Exception as exc:
                log(f"settings: discovery failed ({exc})")
                self._candidates = []
            self.changed.emit()
        return list(self._candidates)

    # the service

    def status(self) -> Optional[Status]:
        return statusmod.read_status(self.status_dir)

    def events(self, limit: int = 100) -> List[dict]:
        return statusmod.read_events(limit, self.status_dir)

    def recent_games(self) -> List[Tuple[str, str]]:
        """(game id, title) of the games the guard intercepted, newest first, each game once."""
        found: List[Tuple[str, str]] = []
        seen = set()
        for event in reversed(self.events(300)):
            game_id = event.get("game")
            if event.get("kind") != "intercept" or not isinstance(game_id, str) or not game_id or game_id in seen:
                continue
            seen.add(game_id)
            found.append((game_id, self._title_for(game_id, event)))
        return found

    def _title_for(self, game_id: str, event: dict) -> str:
        title = event.get("title")
        if isinstance(title, str) and title:
            return title
        message = event.get("message")
        if isinstance(message, str) and " started while " in message:
            head = message.split(" started while ", 1)[0].strip()
            if head:
                return head
        rule = self._working.games.get(game_id)
        return rule.name if rule is not None and rule.name else game_id

    def pause(self, seconds: Optional[float] = None) -> None:
        statusmod.pause(seconds, self.status_dir)
        self.changed.emit()

    def resume(self) -> None:
        statusmod.resume(self.status_dir)
        self.changed.emit()

    def paused(self) -> Tuple[bool, Optional[float]]:
        return statusmod.paused(self.status_dir)

    def restart_needed(self) -> bool:
        """True when runtimes, triggers or the dialog were saved with a value the running service has not seen."""
        return _restart_key(self._saved) != self._restart_baseline

    def restart_service(self) -> Tuple[bool, str]:
        try:
            code, output = self._runner(list(RESTART_COMMAND))
        except Exception as exc:
            code, output = 1, str(exc)
        ok = code == 0
        if ok:
            self._restart_baseline = _restart_key(self._saved)
            self.changed.emit()
        message = output.strip() or ("The service was restarted." if ok else f"systemctl exited with status {code}.")
        return ok, message

    def plan_for(self, game_id: str, targets: Sequence[Target], can_ask: bool = True) -> guard.Plan:
        return guard.plan(self._working, game_id, targets, can_ask, paused=self.paused()[0])

    def effective_action_for_game(self, game_id: str) -> str:
        return self._working.action_for(game_id)


# What the service is doing, shared by the header pill and the Basic status card.
STATE_WATCHING, STATE_PAUSED, STATE_OFF, STATE_RESTART, STATE_DISABLED = (
    "watching", "paused", "not-running", "restart-pending", "disabled")
PILL_TEXT = {STATE_WATCHING: "Watching", STATE_PAUSED: "Paused", STATE_OFF: "Not running",
             STATE_RESTART: "Restart pending", STATE_DISABLED: "Switched off"}
PILL_KIND = {STATE_WATCHING: "ok", STATE_PAUSED: "warn", STATE_OFF: "danger", STATE_RESTART: "warn",
             STATE_DISABLED: "muted"}


def service_state(model: SettingsModel, status: Optional[Status] = None,
                  now: Optional[float] = None) -> Tuple[str, Optional[float]]:
    """(state, pause end) for the service as the model sees it. The pause file is read directly, so a pause
    shows at once instead of after the service's next pass. `pause end` is None for a pause with no end."""
    status = model.status() if status is None else status
    if status is None:
        return STATE_OFF, None
    is_paused, until = statusmod.paused(model.status_dir, now=now)
    if model.restart_needed() or status.state == "restart-pending":
        return STATE_RESTART, until if is_paused else None
    if is_paused or status.paused:
        return STATE_PAUSED, until if is_paused else (None if status.paused_until in (None, float("inf"))
                                                      else status.paused_until)
    if not model.saved.enabled or status.state == "disabled":
        return STATE_DISABLED, None
    return STATE_WATCHING, None

