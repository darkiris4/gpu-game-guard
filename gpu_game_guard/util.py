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


PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def _new_file_mode(path: Path, private: bool = False) -> int:
    """The mode of the file at `path` when it exists (0o600 when it is private and belongs to the current
    user), otherwise 0o600 for a private file or 0o644 as masked by the umask."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        return PRIVATE_FILE_MODE if private and _owned(path) else mode
    except OSError:
        if private:
            return PRIVATE_FILE_MODE
        umask = os.umask(0)
        os.umask(umask)
        return 0o644 & ~umask


def _owned(path: Path) -> bool:
    """True when the current user owns `path`; always true on a platform without user ids."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return True
    try:
        return path.stat().st_uid == geteuid()
    except OSError:
        return False


def ensure_dir(path: Path, private: bool = False) -> None:
    """Create `path` and its parents. A private directory gets mode 0o700 when it is created here, and when
    it exists already and belongs to the current user."""
    tighten = private and (not path.exists() or _owned(path))
    path.mkdir(parents=True, exist_ok=True)
    if tighten:
        try:
            os.chmod(path, PRIVATE_DIR_MODE)
        except OSError:
            pass  # a directory whose mode cannot be set keeps its mode


def atomic_write_text(path: Path, text: str, private: bool = False) -> None:
    """Write `text` so a reader never sees a half-written file. The temporary file is flushed to disk before it
    replaces the target, keeps the target's permissions (a private file is 0o600, new or existing) and never
    outlives a failure."""
    path = Path(os.path.realpath(path))  # write through a symlinked file (for example a managed dotfile)
    ensure_dir(path.parent, private)
    mode = _new_file_mode(path, private)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
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


def atomic_write_json(path: Path, data: Any, private: bool = False) -> None:
    """Write `data` as indented JSON with sorted keys; see `atomic_write_text`."""
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", private)
