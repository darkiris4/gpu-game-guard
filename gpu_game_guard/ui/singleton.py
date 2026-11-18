"""One running instance per name, and the private per-user directories the Qt layer keeps its small files in.

`acquire(name)` takes an exclusive, non-blocking `flock` on a lock file, so a second process that asks for the same
name learns that the first is still running. The kernel drops the lock when its process ends, however it ends.
This module imports no Qt.
"""
from __future__ import annotations

import errno
import fcntl
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Dict, Mapping, Optional

from ..util import log

DIRECTORY_NAME = "gpu-game-guard"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_held: Dict[str, "SingletonLock"] = {}


def _owned_directory(path: Path) -> None:
    """Raise OSError unless `path` is a real directory (not a symlink) owned by the current user."""
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ENOTDIR, f"{path} is not a directory")
    if info.st_uid != os.getuid():
        raise OSError(errno.EPERM, f"{path} belongs to another user")


def runtime_base(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """`$XDG_RUNTIME_DIR` when it is an absolute path to a directory of the current user, otherwise None."""
    value = (os.environ if environ is None else environ).get("XDG_RUNTIME_DIR") or ""
    if not value or not os.path.isabs(value):
        return None
    path = Path(value)
    try:
        _owned_directory(path)
    except OSError:
        return None
    return path


def _make_private(path: Path) -> None:
    if os.lstat(path).st_mode & 0o077:
        os.chmod(path, 0o700)


def private_directory(base: Path, *names: str) -> Path:
    """The directory `base/names...`, each level created with mode 0700 when missing and checked to be a real
    directory of the current user. Raises OSError when a level is a symlink, belongs to someone else or cannot be
    made private."""
    path = Path(base)
    _owned_directory(path)
    for name in names:
        path = path / name
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        _owned_directory(path)
        _make_private(path)
    return path


def _lock_directory(directory: Optional[Path]) -> Path:
    base = Path(directory) if directory is not None else runtime_base()
    if base is None:
        base = Path(tempfile.gettempdir()) / f"{DIRECTORY_NAME}-{os.getuid()}"
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            pass
        _owned_directory(base)
        _make_private(base)
    return private_directory(base, DIRECTORY_NAME)


class SingletonLock:
    """A held lock. `release()` frees it early; otherwise it lasts until the process ends."""

    def __init__(self, name: str, fd: Optional[int]):
        self.name = name
        self._fd = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if _held.get(self.name) is self:
            del _held[self.name]
        if fd is not None:
            try:
                os.close(fd)  # closing the descriptor drops the flock
            except OSError:
                pass


def acquire(name: str, directory: Optional[Path] = None) -> Optional[SingletonLock]:
    """Take the lock called `name`: `<runtime dir>/gpu-game-guard/<name>.lock`, where the runtime directory is
    `$XDG_RUNTIME_DIR` (or `directory` when given), falling back to a private per-user directory under the
    temporary directory. Returns None when another process holds it. When the lock file cannot be used at all the
    caller is let through with a lock that holds nothing, because a broken runtime directory must not stop the
    program from starting."""
    if not _NAME.match(name):
        raise ValueError(f"invalid lock name {name!r}")
    try:
        path = _lock_directory(directory) / f"{name}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        log(f"cannot create the {name} lock ({exc}); continuing without it")
        return SingletonLock(name, None)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        log(f"cannot lock the {name} lock file ({exc}); continuing without it")
        return SingletonLock(name, None)
    lock = SingletonLock(name, fd)
    _held[name] = lock
    return lock
