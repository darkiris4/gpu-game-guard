"""The status channel: files under the state directory that let other processes (the command line, the tray, the
settings window) see what the guard is doing, read its recent events and pause it."""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .model import Ref
from .util import atomic_write_json, xdg_state_home

STATUS_FILE = "status.json"
EVENTS_FILE = "events.jsonl"
PAUSE_FILE = "pause.json"
MAX_EVENTS = 300  # lines kept in the events file


@dataclass
class Status:
    """What status.json holds."""

    version: str = ""
    pid: int = 0
    updated: float = 0.0  # wall time of the write
    state: str = "watching"  # "watching", "paused", "disabled", "asking", "game-running" or "restart-pending"
    paused_until: Optional[float] = None  # wall time; None when not paused; float("inf") when paused with no end
    paused: bool = False
    game: Optional[dict] = None  # {"id": ..., "title": ...} of the game now running or being asked about
    stopped: List[Ref] = field(default_factory=list)  # stopped by the guard and not yet started again
    config_ok: bool = True
    config_error: Optional[str] = None
    dialog: str = ""  # name of the dialog backend
    can_ask: bool = True  # whether that backend can ask
    targets: int = 0  # configured targets


def status_dir(directory: Optional[Path] = None) -> Path:
    return Path(directory) if directory is not None else xdg_state_home() / "gpu-game-guard"


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def write_status(status: Status, directory: Optional[Path] = None) -> None:
    data = {
        "version": status.version, "pid": status.pid, "updated": status.updated, "state": status.state,
        "paused": status.paused,
        "paused_until": status.paused_until if status.paused_until is not None and math.isfinite(status.paused_until) else None,
        "game": status.game, "stopped": list(status.stopped), "config_ok": status.config_ok,
        "config_error": status.config_error, "dialog": status.dialog, "can_ask": status.can_ask,
        "targets": status.targets,
    }
    atomic_write_json(status_dir(directory) / STATUS_FILE, data)


def read_status(directory: Optional[Path] = None, max_age: float = 15.0) -> Optional[Status]:
    """The status last written, or None when there is none, it cannot be read, or it is older than `max_age`
    seconds (the guard is then taken to be not running)."""
    try:
        data = json.loads((status_dir(directory) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    updated = _number(data.get("updated"), 0.0)
    if time.time() - updated > max_age:
        return None
    paused = data.get("paused") is True
    until = data.get("paused_until")
    paused_until = None
    if paused:
        paused_until = float(until) if _number(until, -1.0) >= 0 else float("inf")
    stopped = data.get("stopped")
    game = data.get("game")
    error = data.get("config_error")
    return Status(
        version=str(data.get("version", "")), pid=int(_number(data.get("pid"), 0)), updated=updated,
        state=str(data.get("state", "watching")), paused_until=paused_until, paused=paused,
        game=game if isinstance(game, dict) else None,
        stopped=[ref for ref in stopped if isinstance(ref, dict)] if isinstance(stopped, list) else [],
        config_ok=data.get("config_ok") is not False, config_error=error if isinstance(error, str) else None,
        dialog=str(data.get("dialog", "")), can_ask=data.get("can_ask") is not False,
        targets=int(_number(data.get("targets"), 0)))


def append_event(kind: str, message: str, directory: Optional[Path] = None, **fields: Any) -> None:
    """Append one JSON line to the events file, keeping only the last MAX_EVENTS lines."""
    path = status_dir(directory) / EVENTS_FILE
    entry: Dict[str, Any] = dict(fields)
    entry.update(t=time.time(), kind=kind, message=message)
    line = json.dumps(entry, sort_keys=True, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    lines.append(line)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write("\n".join(lines[-MAX_EVENTS:]) + "\n")
        os.chmod(tmp_name, 0o644 & ~_umask())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def read_events(limit: int = 100, directory: Optional[Path] = None) -> List[dict]:
    """The newest `limit` events, oldest first; lines that are not JSON objects are skipped."""
    if limit <= 0:
        return []
    try:
        lines = (status_dir(directory) / EVENTS_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: List[dict] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict):
            events.append(entry)
    return events[-limit:]


def pause(seconds: Optional[float] = None, directory: Optional[Path] = None) -> None:
    """Pause the guard for `seconds`, or until `resume` when `seconds` is None."""
    until: Optional[float] = None
    if seconds is not None and math.isfinite(seconds):
        until = time.time() + max(float(seconds), 0.0)
    atomic_write_json(status_dir(directory) / PAUSE_FILE, {"until": until})


def resume(directory: Optional[Path] = None) -> None:
    try:
        (status_dir(directory) / PAUSE_FILE).unlink()
    except FileNotFoundError:
        pass


def paused(directory: Optional[Path] = None, now: Optional[float] = None) -> Tuple[bool, Optional[float]]:
    """(is paused, wall time the pause ends or None when it lasts until resumed). A pause that has ended, or a
    pause file that cannot be read, counts as not paused."""
    try:
        data = json.loads((status_dir(directory) / PAUSE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False, None
    if not isinstance(data, dict) or "until" not in data:
        return False, None
    until = data["until"]
    if until is None:
        return True, None
    if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
        return False, None
    if until <= (time.time() if now is None else now):
        return False, None
    return True, float(until)
