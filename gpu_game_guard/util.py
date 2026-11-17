"""Small helpers shared by every module."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def _new_file_mode(path: Path) -> int:
    """The mode of the file at `path` when it exists, otherwise 0o644 as masked by the umask."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o644 & ~umask


def atomic_write_json(path: Path, data: Any) -> None:
    """Write `data` as JSON so a reader never sees a half-written file. The temporary file is flushed to disk
    before it replaces the target, keeps the target's permissions and never outlives a failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _new_file_mode(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # a directory that cannot be synced leaves the replaced file in place
