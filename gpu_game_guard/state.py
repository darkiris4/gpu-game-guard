"""What the guard stopped and has not yet started again, kept on disk across restarts and reboots."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional

from .model import Ref
from .util import atomic_write_json, log, xdg_state_home


def ref_key(ref: Ref) -> tuple:
    """What identifies a stopped target: its kind, runtime and id, or its name when it has no id. Two targets
    that share a label differ by id."""
    return (ref["kind"], ref["runtime"], ref.get("id") or ref["name"])


_key = ref_key


_STR_KEYS = ("scope", "unit", "id", "cwd")


def _clean(item: dict) -> Ref:
    """The required keys of a valid ref plus those optional keys that hold a value of the right type."""
    ref: Ref = {"kind": item["kind"], "runtime": item["runtime"], "name": item["name"]}
    for key in _STR_KEYS:
        if isinstance(item.get(key), str):
            ref[key] = item[key]
    cmdline = item.get("cmdline")
    if isinstance(cmdline, list) and all(isinstance(part, str) for part in cmdline):
        ref["cmdline"] = list(cmdline)
    return ref


def _normalise(item: Any) -> Optional[Ref]:
    """A stored entry as a Ref. The first version stored bare container names, which were Docker's."""
    if isinstance(item, str) and item:
        return {"kind": "container", "runtime": "docker", "name": item}
    if isinstance(item, dict) and all(isinstance(item.get(k), str) and item[k] for k in ("kind", "runtime", "name")):
        return _clean(item)
    return None


_CLOCK_SKEW_SECONDS = 60.0  # how far ahead of the wall clock a stored timestamp may be


def _stopped_list(value: Any) -> List[Ref]:
    if not isinstance(value, list):
        return []
    unique = {}
    for item in value:
        ref = _normalise(item)
        if ref is not None:
            unique.setdefault(_key(ref), ref)
    return list(unique.values())


def _stopped_at(value: Any, wall_time: float) -> float:
    """A stored timestamp when it is a finite number that is not in the future, otherwise the wall time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return wall_time
    if (isinstance(value, float) and not math.isfinite(value)) or value > wall_time + _CLOCK_SKEW_SECONDS:
        return wall_time
    return float(value) if value > -1e18 else wall_time


class State:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else xdg_state_home() / "gpu-game-guard" / "state.json"
        self.stopped: List[Ref] = []
        self.stopped_at = 0.0
        self.game_seen = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the top level is not a JSON object")
            stopped = _stopped_list(data.get("stopped"))
            if stopped:
                self.stopped = stopped
                self.stopped_at = _stopped_at(data.get("stopped_at"), time.time())
                self.game_seen = data.get("game_seen") is True
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, RecursionError) as exc:
            log(f"state: ignoring {self.path}; it cannot be read ({exc.strerror if isinstance(exc, OSError) and exc.strerror else exc})")
            self.stopped, self.stopped_at, self.game_seen = [], 0.0, False

    def add(self, refs: Iterable[Ref], wall_time: float) -> None:
        """Record more stopped targets, keeping those already recorded."""
        merged = {_key(ref): ref for ref in self.stopped}
        for ref in refs:
            merged.setdefault(_key(ref), _clean(ref))
        self.stopped = [merged[key] for key in sorted(merged)]
        self.stopped_at = wall_time
        self.game_seen = False

    def remove(self, refs: Iterable[Ref]) -> None:
        """Forget the given targets, for example those that were started again, and the state file when none
        remain. Only memory changes until `save`."""
        gone = {_key(ref) for ref in refs}
        self.stopped = [ref for ref in self.stopped if _key(ref) not in gone]
        if not self.stopped:
            self.stopped_at, self.game_seen = 0.0, False

    def save(self) -> None:
        if not self.stopped:
            self.path.unlink(missing_ok=True)
            return
        atomic_write_json(self.path, {"stopped": self.stopped, "stopped_at": self.stopped_at, "game_seen": self.game_seen},
                          private=True)

    def clear(self) -> None:
        self.stopped, self.stopped_at, self.game_seen = [], 0.0, False
        self.save()
