"""Configured processes as a Runtime: matched by name, ended with SIGTERM, started again detached."""
from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import discovery, gpu, util
from .model import Ref, Target

TERM_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class Proc:
    pid: int
    start: str  # field 22 of /proc/<pid>/stat: the start time in clock ticks since boot
    comm: str


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def parse_stat(text: str) -> Optional[Tuple[str, int, str]]:
    """(state, parent pid, start time) from the contents of /proc/<pid>/stat. The command name in the second
    field may contain spaces and parentheses, so fields are counted from the last closing parenthesis."""
    end = text.rfind(")")
    fields = text[end + 2:].split() if end >= 0 else []
    if len(fields) < 20:
        return None
    try:
        return fields[0], int(fields[1]), fields[19]
    except ValueError:
        return None


class ProcessRuntime:
    """A configured `process` target is running while the current user has a process whose name matches."""

    name = "process"

    def __init__(self, get_config: Callable[[], Any], run: Optional[Callable[..., Any]] = None, proc: Path = gpu.PROC,
                 vram: Optional[Callable[[], Optional[Dict[int, int]]]] = None, uid: Optional[int] = None,
                 self_pid: Optional[int] = None, kill: Callable[[int, int], None] = os.kill,
                 popen: Callable[..., Any] = subprocess.Popen, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._get_config = get_config
        self._run = run
        self._proc = proc
        self._vram = vram or (lambda: gpu.vram_by_pid(self._proc))
        self._uid = os.getuid() if uid is None else uid
        self._self_pid = os.getpid() if self_pid is None else self_pid
        self._kill = kill
        self._popen = popen
        self._sleep = sleep
        self._clock = clock

    def installed(self) -> bool:
        return True

    def available(self) -> bool:
        return True

    # Reading /proc

    def _stat(self, pid: int) -> Optional[Tuple[str, int, str]]:
        text = _read(self._proc / str(pid) / "stat")
        return parse_stat(text) if text else None

    def _owned(self, pid: int) -> bool:
        for line in (_read(self._proc / str(pid) / "status") or "").splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                return len(fields) > 1 and fields[1] == str(self._uid)
        return False

    def _ancestry(self) -> Set[int]:
        """This process and its ancestors, which are never signalled."""
        found: Set[int] = set()
        pid = self._self_pid
        while pid > 0 and pid not in found:
            found.add(pid)
            stat = self._stat(pid)
            pid = stat[1] if stat else 0
        return found

    def _all(self) -> List[Proc]:
        """Every live process of the current user."""
        try:
            names = [entry.name for entry in self._proc.iterdir() if entry.name.isdigit()]
        except OSError:
            return []
        found = []
        for name in names:
            pid = int(name)
            stat = self._stat(pid)
            if stat is None or stat[0] in ("Z", "X") or not self._owned(pid):
                continue
            comm = (_read(self._proc / name / "comm") or "").strip()
            if comm:
                found.append(Proc(pid, stat[2], comm))
        return found

    def _cmdline(self, pid: int) -> List[str]:
        return [part for part in (_read(self._proc / str(pid) / "cmdline") or "").split("\0") if part]

    def _cwd(self, pid: int) -> Optional[str]:
        try:
            return os.readlink(self._proc / str(pid) / "cwd")
        except OSError:
            return None

    # Matching

    def _matches(self, spec: Any, everything: Optional[List[Proc]] = None) -> List[Proc]:
        """The current user's processes that match a spec, minus protected names and the guard's own ancestry."""
        pattern = spec.match
        if not any(ch not in "*?[]" for ch in pattern):
            gpu.log_once(f"process pattern {spec.id}", f"process target {spec.id!r}: pattern {pattern!r} is too broad; refused")
            return []
        config = self._get_config()
        skip = self._ancestry()
        found = []
        for item in self._all() if everything is None else everything:
            if not fnmatch.fnmatchcase(item.comm, pattern) or item.pid in skip:
                continue
            if discovery.is_protected(config, item.comm, self._cmdline(item.pid)):
                gpu.log_once(f"process protected {spec.id} {item.comm}",
                             f"process target {spec.id!r}: {item.comm} is on the protect list; left alone")
                continue
            found.append(item)
        return found

    # Runtime protocol

    def running_gpu_targets(self) -> List[Target]:
        specs = gpu.usable_specs(self._get_config, "process")
        everything = self._all() if specs else []
        targets = []
        by_pid: Optional[Dict[int, int]] = None
        measured = False
        for spec in specs:
            matched = self._matches(spec, everything)
            if not matched:
                continue
            if not measured:
                measured = True
                try:
                    by_pid = self._vram()
                except Exception as exc:
                    util.log(f"GPU memory lookup failed ({exc})")
            oldest = min(matched, key=lambda item: (int(item.start) if item.start.isdigit() else 0, item.pid))
            extra: Dict[str, Any] = {"id": spec.id}
            cmdline = self._cmdline(oldest.pid)
            if cmdline:
                extra["cmdline"] = cmdline
            cwd = self._cwd(oldest.pid)
            if cwd:
                extra["cwd"] = cwd
            vram = gpu.memory_for_pids(by_pid, [item.pid for item in matched])
            targets.append(Target("process", self.name, gpu.display_name(spec), "", vram, "", extra))
        return targets

    def _alive(self, item: Proc) -> bool:
        stat = self._stat(item.pid)
        return stat is not None and stat[0] not in ("Z", "X") and stat[2] == item.start

    def stop(self, refs: Sequence[Ref]) -> None:
        signalled: List[Proc] = []
        for ref in gpu.own_refs(refs, "process", self.name):
            try:
                spec = gpu.find_spec(self._get_config, "process", ref)
                if spec is None:
                    continue
                for item in self._matches(spec):
                    if not self._alive(item):  # the pid may have been reused since the scan
                        continue
                    try:
                        self._kill(item.pid, signal.SIGTERM)
                        signalled.append(item)
                    except ProcessLookupError:
                        continue
                    except OSError as exc:
                        util.log(f"process {item.comm} ({item.pid}): cannot signal ({exc})")
            except Exception as exc:
                util.log(f"process stop: {exc}")
        deadline = self._clock() + TERM_WAIT_SECONDS
        remaining = [item for item in signalled if self._alive(item)]
        while remaining and self._clock() < deadline:
            self._sleep(_POLL_SECONDS)
            remaining = [item for item in remaining if self._alive(item)]
        for item in remaining:
            util.log(f"process {item.comm} ({item.pid}) is still running after SIGTERM; left running")

    def start(self, refs: Sequence[Ref]) -> None:
        for ref in gpu.own_refs(refs, "process", self.name):
            try:
                spec = gpu.find_spec(self._get_config, "process", ref)
                if spec is None:
                    continue
                if not getattr(spec, "restart", False):
                    util.log(f"process target {spec.id!r}: restart is off; left stopped")
                    continue
                cmdline = ref.get("cmdline")
                if not isinstance(cmdline, list) or not cmdline or not all(isinstance(part, str) for part in cmdline):
                    util.log(f"process target {spec.id!r}: no command line was recorded; cannot start it again")
                    continue
                if self._matches(spec):
                    continue  # already running again
                cwd = ref.get("cwd")
                self._popen(cmdline, cwd=cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None,
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL)
            except Exception as exc:
                util.log(f"process start: {exc}")
