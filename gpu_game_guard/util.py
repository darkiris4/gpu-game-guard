"""Small helpers shared by every module."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def log(message: str) -> None:
    print(message, flush=True)


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def atomic_write_json(path: Path, data: Any) -> None:
    """Write `data` as JSON so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
    os.replace(tmp.name, path)
