"""Ready-made target definitions, shipped as JSON files in this package and read with `pkgutil.get_data`, which
also works from a zipapp. The package is the module: a `presets.py` beside this directory would be shadowed."""
from __future__ import annotations

import json
import pkgutil
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import gpu, util

PACKAGE = "gpu_game_guard.presets"
_KINDS = ("service", "command", "process")

# Listed explicitly because a zipapp cannot be enumerated with the file system.
FILES = (
    "ollama-system-service.json",
    "ollama-user-service.json",
    "ollama-unload.json",
)


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    spec: Dict[str, Any]  # a `targets` entry, ready to add to the config
    detect: Dict[str, Any] = field(default_factory=dict)  # {"unit": ..., "scope": ...} or {"command": ...}


def _parse(text: str) -> Preset:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    spec, detect = data.get("spec"), data.get("detect", {})
    if not isinstance(spec, dict) or spec.get("kind") not in _KINDS or not isinstance(spec.get("id"), str):
        raise ValueError("invalid spec")
    if not isinstance(detect, dict):
        raise ValueError("invalid detect")
    for key in ("id", "label", "description"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"missing {key}")
    return Preset(data["id"], data["label"], data["description"], spec, detect)


def load_presets(files: Optional[Sequence[str]] = None,
                 get_data: Callable[[str, str], Optional[bytes]] = pkgutil.get_data) -> List[Preset]:
    """Every shipped preset in file order. A missing or malformed file is logged and skipped."""
    found = []
    for name in FILES if files is None else files:
        try:
            raw = get_data(PACKAGE, name)
            if raw is None:
                raise ValueError("not found")
            found.append(_parse(raw.decode("utf-8")))
        except Exception as exc:
            util.log(f"preset {name}: skipped ({exc})")
    return found


def applicable(preset: Preset, which: Callable[[str], Optional[str]] = shutil.which,
               run: Optional[Callable[..., Optional[gpu.CommandResult]]] = None) -> bool:
    """True when what the preset manages exists here: its unit is installed, or its command is on PATH."""
    detect = preset.detect or {}
    unit, command = detect.get("unit"), detect.get("command")
    if isinstance(unit, str):
        if which("systemctl") is None:
            return False
        scope = ["--user"] if detect.get("scope") == "user" else []
        result = (run or gpu.run_process)(["systemctl", *scope, "cat", unit])
        return result is not None and result.returncode == 0
    if isinstance(command, str):
        return which(command) is not None
    return True
