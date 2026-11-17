"""Configuration: the on-disk JSON file, its validation and a watcher that picks up edits."""
from __future__ import annotations

import fnmatch
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import atomic_write_json, log, xdg_config_home

GAME_ACTIONS = ("ask", "stop", "ignore")
ON_LAUNCH = ("ask", "stop", "ignore")  # what to do when a launch meets GPU workloads
AFTER_GAME = ("ask", "restart", "leave")  # what to do once the game has closed
DIALOGS = ("auto", "zenity", "kdialog", "notify", "none")

# key -> (kind, minimum allowed value, whether zero is allowed)
_NUMBERS = {
    "poll_seconds": ("float", False),
    "exit_grace_seconds": ("float", True),
    "no_game_timeout_seconds": ("float", True),
    "bypass_seconds": ("float", True),
    "dialog_retry_seconds": ("float", True),
    "heavy_vram_mib": ("int", True),
}
_ENUMS = {
    "on_launch": ON_LAUNCH,
    "after_game": AFTER_GAME,
    "dialog": DIALOGS,
}
_STRING_LISTS = ("ignore_containers", "runtimes", "triggers")
_KNOWN_KEYS = set(_NUMBERS) | set(_ENUMS) | set(_STRING_LISTS) | {"games"}


class ConfigError(ValueError):
    """The configuration file holds values the guard cannot use."""


@dataclass
class GameRule:
    name: str = ""  # display name, informational
    action: str = "ask"  # one of GAME_ACTIONS


@dataclass
class Config:
    on_launch: str = "ask"
    after_game: str = "ask"
    poll_seconds: float = 1.0
    exit_grace_seconds: float = 8.0
    no_game_timeout_seconds: float = 180.0
    bypass_seconds: float = 120.0
    dialog_retry_seconds: float = 300.0
    heavy_vram_mib: int = 4096
    ignore_containers: List[str] = field(default_factory=list)  # fnmatch globs on the container name
    runtimes: List[str] = field(default_factory=lambda: ["docker", "podman"])
    triggers: List[str] = field(default_factory=lambda: ["steam"])
    dialog: str = "auto"
    games: Dict[str, GameRule] = field(default_factory=dict)  # game_id ("steam:730") -> rule

    def action_for(self, game_id: str) -> str:
        """The per-game rule when one exists, otherwise `on_launch`."""
        rule = self.games.get(game_id)
        return rule.action if rule is not None else self.on_launch

    def is_ignored(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.ignore_containers)

    def to_json(self) -> dict:
        return {
            "on_launch": self.on_launch,
            "after_game": self.after_game,
            "poll_seconds": self.poll_seconds,
            "exit_grace_seconds": self.exit_grace_seconds,
            "no_game_timeout_seconds": self.no_game_timeout_seconds,
            "bypass_seconds": self.bypass_seconds,
            "dialog_retry_seconds": self.dialog_retry_seconds,
            "heavy_vram_mib": self.heavy_vram_mib,
            "ignore_containers": list(self.ignore_containers),
            "runtimes": list(self.runtimes),
            "triggers": list(self.triggers),
            "dialog": self.dialog,
            "games": {gid: {"name": rule.name, "action": rule.action} for gid, rule in self.games.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "Config":
        problems = validate(data)
        if problems:
            raise ConfigError("; ".join(problems))
        unknown = sorted(str(k) for k in data if k not in _KNOWN_KEYS)
        if unknown:
            log(f"config: ignoring unknown keys: {', '.join(unknown)}")
        config = cls()
        for key, (kind, _zero) in _NUMBERS.items():
            if key in data:
                setattr(config, key, int(data[key]) if kind == "int" else float(data[key]))
        for key in _ENUMS:
            if key in data:
                setattr(config, key, data[key])
        for key in _STRING_LISTS:
            if key in data:
                setattr(config, key, list(data[key]))
        for gid, rule in data.get("games", {}).items():
            config.games[gid] = GameRule(name=rule.get("name", ""), action=rule.get("action", "ask"))
        return config


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(data: dict) -> List[str]:
    """Human-readable problems with a decoded config; an empty list means it is valid."""
    if not isinstance(data, dict):
        return [f"the top level must be a JSON object, not {type(data).__name__}"]
    problems: List[str] = []
    for key, (kind, zero_ok) in _NUMBERS.items():
        if key not in data:
            continue
        value = data[key]
        want = "a whole number" if kind == "int" else "a number"
        if not _is_number(value) or (kind == "int" and int(value) != value):
            problems.append(f'"{key}" must be {want} of {"zero or more" if zero_ok else "more than zero"}, got {value!r}')
        elif value < 0 or (value == 0 and not zero_ok):
            problems.append(f'"{key}" must be {"zero or more" if zero_ok else "more than zero"}, got {value!r}')
    for key, allowed in _ENUMS.items():
        if key in data and data[key] not in allowed:
            problems.append(f'"{key}" must be one of {", ".join(allowed)}; got {data[key]!r}')
    for key in _STRING_LISTS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f'"{key}" must be a list of strings, got {value!r}')
    if "games" in data:
        games = data["games"]
        if not isinstance(games, dict):
            problems.append(f'"games" must be an object mapping game ids such as "steam:730" to rules, got {games!r}')
        else:
            for gid, rule in games.items():
                where = f'"games" entry {gid!r}'
                if not isinstance(rule, dict):
                    problems.append(f'{where} must be an object like {{"name": "...", "action": "ask"}}, got {rule!r}')
                    continue
                if "name" in rule and not isinstance(rule["name"], str):
                    problems.append(f'{where}: "name" must be a string, got {rule["name"]!r}')
                if "action" in rule and rule["action"] not in GAME_ACTIONS:
                    problems.append(f'{where}: "action" must be one of {", ".join(GAME_ACTIONS)}; got {rule["action"]!r}')
    return problems


def config_path() -> Path:
    return xdg_config_home() / "gpu-game-guard" / "config.json"


def _read(path: Path) -> Config:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return Config.from_json(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load(path: Optional[Path] = None) -> Config:
    """The config at `path`; defaults when the file is missing; ConfigError when it cannot be used."""
    return _read(Path(path) if path is not None else config_path())


def save(config: Config, path: Optional[Path] = None) -> None:
    atomic_write_json(Path(path) if path is not None else config_path(), config.to_json())


class ConfigWatcher:
    """Hands out the current config, re-reading the file when its modification time changes. A file that
    cannot be read or fails validation leaves the previous config in force."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else config_path()
        self._config = Config()
        self._stamp: Any = object()  # differs from every real stamp, forcing the first read
        self._last_error = ""

    def _stat_stamp(self) -> Optional[int]:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def current(self) -> Config:
        stamp = self._stat_stamp()
        if stamp == self._stamp:
            return self._config
        self._stamp = stamp
        try:
            self._config = _read(self.path)
            self._last_error = ""
        except ConfigError as exc:
            message = str(exc)
            if message != self._last_error:
                log(f"config: keeping the previous settings; {message}")
                self._last_error = message
        return self._config
