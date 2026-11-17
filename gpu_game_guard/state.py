"""What the guard stopped and has not yet started again, kept on disk across restarts and reboots."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Iterable, List, Optional

from .model import Ref
from .util import atomic_write_json, xdg_state_home


def _key(ref: Ref) -> tuple:
    return (ref["kind"], ref["runtime"], ref["name"])


def _normalise(item: Any) -> Optional[Ref]:
    """A stored entry as a Ref. The first version stored bare container names, which were Docker's."""
    if isinstance(item, str) and item:
        return {"kind": "container", "runtime": "docker", "name": item}
    if isinstance(item, dict) and all(isinstance(item.get(k), str) and item[k] for k in ("kind", "runtime", "name")):
        return {"kind": item["kind"], "runtime": item["runtime"], "name": item["name"]}
    return None


_CLOCK_SKEW_SECONDS = 60.0  # how far ahead of the wall clock a stored timestamp may be


def _stopped_list(value: Any) -> List[Ref]:
    if not isinstance(value, list):
        return []
    refs = [_normalise(item) for item in value]
    return [ref for ref in refs if ref is not None]


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
            data = json.loads(self.path.read_text())
            stopped = _stopped_list(data.get("stopped"))
            if stopped:
                self.stopped = stopped
                self.stopped_at = _stopped_at(data.get("stopped_at"), time.time())
                self.game_seen = data.get("game_seen") is True
        except (OSError, ValueError, AttributeError, RecursionError):
            self.stopped, self.stopped_at, self.game_seen = [], 0.0, False

    def add(self, refs: Iterable[Ref], wall_time: float) -> None:
        """Record more stopped targets, keeping those already recorded."""
        merged = {_key(ref): ref for ref in self.stopped}
        for ref in refs:
            merged.setdefault(_key(ref), {"kind": ref["kind"], "runtime": ref["runtime"], "name": ref["name"]})
        self.stopped = [merged[key] for key in sorted(merged)]
        self.stopped_at = wall_time
        self.game_seen = False

    def save(self) -> None:
        if not self.stopped:
            self.path.unlink(missing_ok=True)
            return
        atomic_write_json(self.path, {"stopped": self.stopped, "stopped_at": self.stopped_at, "game_seen": self.game_seen})

    def clear(self) -> None:
        self.stopped, self.stopped_at, self.game_seen = [], 0.0, False
        self.save()
