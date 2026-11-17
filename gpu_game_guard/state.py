"""What the guard stopped and has not yet started again, kept on disk across restarts and reboots."""
from __future__ import annotations

import json
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


class State:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else xdg_state_home() / "gpu-game-guard" / "state.json"
        self.stopped: List[Ref] = []
        self.stopped_at = 0.0
        self.game_seen = False
        try:
            data = json.loads(self.path.read_text())
            refs = [_normalise(item) for item in data["stopped"]]
            self.stopped = [ref for ref in refs if ref is not None]
            self.stopped_at = float(data["stopped_at"])
            self.game_seen = bool(data["game_seen"])
        except (OSError, ValueError, KeyError, TypeError):
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
