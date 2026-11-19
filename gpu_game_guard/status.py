"""The status channel: files under the state directory that let other processes (the command line, the tray, the
settings window) see what the guard is doing, read its recent events and pause it."""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .model import Ref
from .util import atomic_write_json, atomic_write_text, xdg_state_home

STATUS_FILE = "status.json"
EVENTS_FILE = "events.jsonl"
PAUSE_FILE = "pause.json"
MAX_EVENTS = 300  # lines kept in the events file
MAX_VALID_FOR = 3600.0  # the longest validity a reader honours, whatever the file claims


@dataclass
class Status:
    """What status.json holds."""

    version: str = ""
    pid: int = 0
    pid_start: int = 0  # the process's start time in clock ticks since boot (/proc), 0 when unknown
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
    valid_for: float = 15.0  # seconds after `updated` for which a reader may take this status as current


def process_start(pid: int) -> int:
    """The start time of process `pid` in clock ticks since boot, read from /proc/<pid>/stat; 0 when it cannot
    be read, for example off Linux or when the process is gone."""
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
        # the command name (field 2) may hold spaces and parentheses, so count fields after its closing one
        return int(text[text.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError, OverflowError):
        return 0


def pid_alive(pid: int) -> bool:
    """True when a process with this id exists; a process of another user counts as existing."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _same_process(pid: int, recorded_start: int) -> bool:
    """False when `pid` is gone or now belongs to a process that started at another time than recorded."""
    if not pid_alive(pid):
        return False
    if recorded_start > 0:
        current = process_start(pid)
        if current > 0 and current != recorded_start:
            return False
    return True


def status_dir(directory: Optional[Path] = None) -> Path:
    return Path(directory) if directory is not None else xdg_state_home() / "gpu-game-guard"


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def write_status(status: Status, directory: Optional[Path] = None) -> None:
    data = {
        "version": status.version, "pid": status.pid, "pid_start": status.pid_start, "updated": status.updated, "state": status.state,
        "paused": status.paused,
        "paused_until": status.paused_until if status.paused_until is not None and math.isfinite(status.paused_until) else None,
        "game": status.game, "stopped": list(status.stopped), "config_ok": status.config_ok,
        "config_error": status.config_error, "dialog": status.dialog, "can_ask": status.can_ask,
        "targets": status.targets, "valid_for": status.valid_for,
    }
    atomic_write_json(status_dir(directory) / STATUS_FILE, data, private=True)


def read_status(directory: Optional[Path] = None, max_age: float = 15.0) -> Optional[Status]:
    """The status last written, or None when there is none, it cannot be read, it is older than `max_age`
    seconds, or than the status's own `valid_for` when that is longer, or the process that wrote it is gone or
    has been replaced by another one with the same id (the guard is then taken to be not running).
    A guard that blocks on a dialog publishes a long `valid_for` so it is not mistaken for a stopped one."""
    try:
        data = json.loads((status_dir(directory) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    updated = _number(data.get("updated"), 0.0)
    valid_for = min(max(_number(data.get("valid_for"), 15.0), 0.0), MAX_VALID_FOR)
    if time.time() - updated > max(max_age, valid_for):
        return None
    pid = int(_number(data.get("pid"), 0))
    pid_start = int(_number(data.get("pid_start"), 0))
    if pid > 0 and not _same_process(pid, pid_start):
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
        version=str(data.get("version", "")), pid=pid, pid_start=pid_start, updated=updated,
        state=str(data.get("state", "watching")), paused_until=paused_until, paused=paused,
        game=game if isinstance(game, dict) else None,
        stopped=[ref for ref in stopped if isinstance(ref, dict)] if isinstance(stopped, list) else [],
        config_ok=data.get("config_ok") is not False, config_error=error if isinstance(error, str) else None,
        dialog=str(data.get("dialog", "")), can_ask=data.get("can_ask") is not False,
        targets=int(_number(data.get("targets"), 0)), valid_for=valid_for)


def remove_status(directory: Optional[Path] = None, pid: Optional[int] = None) -> None:
    """Delete the status file, so a stopped guard never reads as running. With `pid`, a file written by another
    process is left alone. A file that is already gone or cannot be removed is ignored."""
    path = status_dir(directory) / STATUS_FILE
    if pid is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            data = None
        if isinstance(data, dict) and int(_number(data.get("pid"), 0)) not in (0, pid):
            return
    try:
        path.unlink()
    except OSError:
        pass


def append_event(kind: str, message: str, directory: Optional[Path] = None, **fields: Any) -> None:
    """Append one JSON line to the events file, keeping only the last MAX_EVENTS lines."""
    path = status_dir(directory) / EVENTS_FILE
    entry: Dict[str, Any] = dict(fields)
    entry.update(t=time.time(), kind=kind, message=message)
    line = json.dumps(entry, sort_keys=True, default=str)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    lines.append(line)
    atomic_write_text(path, "\n".join(lines[-MAX_EVENTS:]) + "\n", private=True)


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
    atomic_write_json(status_dir(directory) / PAUSE_FILE, {"until": until}, private=True)


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
