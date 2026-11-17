"""Configuration: the on-disk JSON file, its validation and a watcher that picks up edits."""
from __future__ import annotations

import difflib
import fnmatch
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .util import atomic_write_json, log, xdg_config_home

GAME_ACTIONS = ("ask", "stop", "ignore")
ON_LAUNCH = ("ask", "stop", "ignore")  # what to do when a launch meets GPU workloads
AFTER_GAME = ("ask", "restart", "leave")  # what to do once the game has closed
DIALOGS = ("auto", "zenity", "kdialog", "notify", "none")

# key -> (kind, lowest allowed value, highest allowed value)
_NUMBERS = {
    "poll_seconds": ("float", 0.2, 3600),
    "exit_grace_seconds": ("float", 2, 3600),
    "no_game_timeout_seconds": ("float", 30, 86400),
    "bypass_seconds": ("float", 5, 3600),
    "dialog_retry_seconds": ("float", 30, 86400),
    "heavy_vram_mib": ("int", 0, 10000000),
}
_ENUMS = {
    "on_launch": ON_LAUNCH,
    "after_game": AFTER_GAME,
    "dialog": DIALOGS,
}
_STRING_LISTS = ("ignore_containers", "runtimes", "triggers")
_KNOWN_KEYS = set(_NUMBERS) | set(_ENUMS) | set(_STRING_LISTS) | {"games"}
_RULE_KEYS = ("name", "action")


class ConfigError(ValueError):
    """The configuration file holds values the guard cannot use. `reason` never includes the path."""

    def __init__(self, reason: str, path: Optional[Path] = None):
        super().__init__(reason)
        self.reason = reason
        self.path = path

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}" if self.path is not None else self.reason


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
        problems = _problems(data, strict=False)
        if problems:
            raise ConfigError("; ".join(problems))
        unknown = sorted(str(k) for k in data if k not in _KNOWN_KEYS)
        for gid, rule in data.get("games", {}).items():
            unknown.extend(f"{gid}.{k}" for k in rule if k not in _RULE_KEYS)
        if unknown:
            log(f"config: ignoring unknown keys: {', '.join(unknown)}")
        config = cls()
        for key, (kind, _low, _high) in _NUMBERS.items():
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
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True  # of any size; comparing it with a bound never overflows
    return isinstance(value, float) and math.isfinite(value)


def _shown(value: Any) -> str:
    """`repr(value)` cut to a length fit for a message."""
    try:
        text = repr(value)
    except ValueError:  # an integer with more digits than repr allows
        return "a very large number"
    return text if len(text) <= 60 else text[:57] + "..."


def _bound(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def _suggestion(key: str, known: Any) -> str:
    close = difflib.get_close_matches(key, list(known), n=1)
    return f"; did you mean {close[0]!r}?" if close else ""


def validate(data: dict) -> List[str]:
    """Human-readable problems with a decoded config, unknown keys included; an empty list means it is valid."""
    return _problems(data, strict=True)


def _problems(data: Any, strict: bool) -> List[str]:
    if not isinstance(data, dict):
        return [f"the top level must be a JSON object, not {type(data).__name__}"]
    problems: List[str] = []
    if strict:
        for key in data:
            if key not in _KNOWN_KEYS:
                problems.append(f"unknown key {str(key)!r}{_suggestion(str(key), _KNOWN_KEYS)}")
    for key, (kind, low, high) in _NUMBERS.items():
        if key not in data:
            continue
        value = data[key]
        want = "a whole number" if kind == "int" else "a number"
        if not _is_number(value) or (kind == "int" and int(value) != value) or not low <= value <= high:
            problems.append(f'"{key}" must be {want} from {_bound(low)} to {_bound(high)}, got {_shown(value)}')
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
                if strict:
                    for key in rule:
                        if key not in _RULE_KEYS:
                            problems.append(f"{where}: unknown key {str(key)!r}{_suggestion(str(key), _RULE_KEYS)}")
    return problems


def config_path() -> Path:
    return xdg_config_home() / "gpu-game-guard" / "config.json"


def _read(path: Path, missing_ok: bool = True) -> Config:
    """The config in the file at `path`; defaults for a missing file unless `missing_ok` is false."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if missing_ok:
            return Config()
        raise
    except UnicodeDecodeError as exc:
        raise ConfigError(f"is not valid UTF-8: {exc}", path) from exc
    except OSError as exc:
        raise ConfigError(f"cannot be read: {exc}", path) from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ConfigError(f"is not valid JSON: {exc}", path) from exc
    try:
        return Config.from_json(data)
    except ConfigError as exc:
        raise ConfigError(exc.reason, path) from exc


def load(path: Optional[Path] = None) -> Config:
    """The config at `path`; defaults when the file is missing; ConfigError when it cannot be used."""
    return _read(Path(path) if path is not None else config_path())


def save(config: Config, path: Optional[Path] = None) -> None:
    atomic_write_json(Path(path) if path is not None else config_path(), config.to_json())


class ConfigWatcher:
    """Hands out the current config, re-reading the file when its modification time, size or inode changes. A
    file that cannot be read or fails validation leaves the previous config in force, as does a file that
    disappears after having existed."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else config_path()
        self._config = Config()
        self._stamp: Any = object()  # differs from every real stamp, forcing the first read
        self._existed = False
        self._last_error = ""

    def _stat_stamp(self) -> Optional[Tuple[int, int, int]]:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def current(self) -> Config:
        stamp = self._stat_stamp()
        if stamp == self._stamp:
            return self._config
        self._stamp = stamp
        if stamp is None:
            if self._existed:
                log("config: the file is missing; keeping the previous settings until it returns")
            else:
                self._config = Config()
            return self._config
        self._existed = True
        try:
            self._config = _read(self.path, missing_ok=False)
            self._last_error = ""
        except FileNotFoundError:  # removed between the stat and the read
            self._stamp = None
        except ConfigError as exc:
            message = str(exc)
            if message != self._last_error:
                log(f"config: keeping the previous settings; {message}")
                self._last_error = message
        return self._config
