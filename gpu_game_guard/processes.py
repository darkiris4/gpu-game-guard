"""Configured processes as a Runtime: matched by name, ended with SIGTERM, started again detached.

A `match` is a glob on the kernel's process name (`comm`, at most 15 characters), so a pattern needs at least three
literal characters and at most 15 characters in all, and one that matches more than 25 of the user's processes is
refused as too broad. The command line and working directory a restart needs are read from /proc immediately before
the SIGTERM and written into the ref passed to `stop`; a process that rewrote its argv[0] is started from its
executable path instead, and a command line that cannot be reconstructed is recorded as an empty list, which makes
the restart fail rather than guess. The restarted program is started in its own systemd scope when the user manager
is reachable, so that stopping or restarting the guard does not end it."""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import discovery, gpu, util
from .model import Ref, Target

TERM_WAIT_SECONDS = 5.0
START_WATCH_SECONDS = 2.0  # how long a restarted program is watched for an immediate failure
MIN_LITERAL = 3  # literal characters a pattern needs
MAX_MATCH = 15  # the kernel truncates comm to 15 characters
MAX_PROCESSES = 25  # a pattern matching more of the user's processes is too broad
_POLL_SECONDS = 0.1
_SESSION_VARIABLES = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
_MANAGER_STATES = ("running", "degraded", "starting", "initializing")


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
                 clock: Callable[[], float] = time.monotonic, which: Callable[[str], Optional[str]] = shutil.which,
                 wall: Callable[[], float] = time.time, environ: Optional[Dict[str, str]] = None):
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
        self._which = which
        self._wall = wall
        self._environ = environ

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

    def _exe(self, pid: int) -> Optional[str]:
        try:
            target = os.readlink(self._proc / str(pid) / "exe")
        except OSError:
            return None
        return target[:-len(" (deleted)")] if target.endswith(" (deleted)") else target

    # Matching

    def _pattern_problem(self, spec: Any) -> Optional[str]:
        pattern = spec.match
        if sum(1 for ch in pattern if ch not in "*?[]") < MIN_LITERAL:
            return f"pattern {pattern!r} has fewer than {MIN_LITERAL} literal characters"
        if len(pattern) > MAX_MATCH:
            return f"pattern {pattern!r} is longer than {MAX_MATCH} characters, the most the kernel keeps of a process name"
        return None

    def _matches(self, spec: Any, everything: Optional[List[Proc]] = None) -> List[Proc]:
        """The current user's processes that match a spec, minus protected names and the guard's own ancestry."""
        pattern = spec.match
        problem = self._pattern_problem(spec)
        if problem:
            gpu.log_once(f"process pattern {spec.id}", f"process target {spec.id!r}: {problem}; refused")
            return []
        config = self._get_config()
        skip = self._ancestry()
        found = []
        candidates = [item for item in (self._all() if everything is None else everything)
                      if fnmatch.fnmatchcase(item.comm, pattern)]
        if len(candidates) > MAX_PROCESSES:
            gpu.log_once(f"process pattern {spec.id}",
                         f"process target {spec.id!r}: pattern {pattern!r} matches {len(candidates)} processes; too broad, refused")
            return []
        for item in candidates:
            if item.pid in skip:
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

    def _executable(self, command: str, cwd: Optional[str]) -> bool:
        """True when `command` names an executable file: a path (relative ones to `cwd`) or a name on PATH."""
        if "/" in command:
            path = command if os.path.isabs(command) else os.path.join(cwd or "", command) if cwd else ""
            return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
        return self._which(command) is not None

    def _restart_command(self, cmdline: List[str], exe: Optional[str], cwd: Optional[str]) -> List[str]:
        """The command line to start a process again, or an empty list when none can be told. A command line whose
        first entry is not an executable and looks like a rewritten title (it contains a colon or a space) is
        replaced by the executable path with the remaining arguments."""
        if not cmdline:
            return []
        if self._executable(cmdline[0], cwd):
            return list(cmdline)
        rewritten = ":" in cmdline[0] or " " in cmdline[0]
        if rewritten and exe and self._executable(exe, None):
            return [exe, *cmdline[1:]]
        return []

    def _record_command(self, ref: Ref, item: Proc) -> None:
        """Write the restart command line and working directory of a process into the ref, read from /proc now.
        What the scan recorded stays as the fallback when the process can no longer be read."""
        if not self._alive(item):
            return
        scanned = ref.get("cmdline")
        cmdline = self._cmdline(item.pid) or (scanned if isinstance(scanned, list) else [])
        cwd = self._cwd(item.pid) or (ref.get("cwd") if isinstance(ref.get("cwd"), str) else None)
        ref["cmdline"] = self._restart_command([part for part in cmdline if isinstance(part, str)], self._exe(item.pid), cwd)
        if cwd:
            ref["cwd"] = cwd
        if not ref["cmdline"]:
            util.log(f"process {item.comm} ({item.pid}): the command line cannot be told; it will not be started again")

    def stop(self, refs: Sequence[Ref]) -> None:
        signalled: List[Proc] = []
        for ref in gpu.own_refs(refs, "process", self.name):
            try:
                spec = gpu.find_spec(self._get_config, "process", ref)
                if spec is None:
                    continue
                matched = self._matches(spec)
                if matched:
                    self._record_command(ref, min(matched, key=lambda item: (int(item.start) if item.start.isdigit() else 0, item.pid)))
                for item in matched:
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

    # Starting again

    def _user_manager_answers(self) -> bool:
        result = (self._run or gpu.run_process)(["systemctl", "--user", "--no-ask-password", "is-system-running"], timeout=5.0)
        return result is not None and result.stdout.strip() in _MANAGER_STATES

    def _environment(self) -> Dict[str, str]:
        """The guard's environment plus the session variables a graphical program needs, taken from the user
        manager when the guard's own environment lacks them."""
        env = dict(os.environ if self._environ is None else self._environ)
        missing = [name for name in _SESSION_VARIABLES if not env.get(name)]
        if missing and self._which("systemctl"):
            result = (self._run or gpu.run_process)(["systemctl", "--user", "show-environment"], timeout=5.0)
            if result is not None and result.returncode == 0:
                for line in result.stdout.splitlines():
                    name, sep, value = line.partition("=")
                    if sep and name in missing and value:
                        env[name] = value
        return env

    def _launch(self, spec_id: str, cmdline: List[str], cwd: Optional[str]) -> Any:
        """Start the program, in its own systemd scope when systemd-run exists and the user manager answers."""
        command = list(cmdline)
        if self._which("systemd-run") and self._which("systemctl") and self._user_manager_answers():
            unit = f"gpu-game-guard-{re.sub(r'[^A-Za-z0-9_-]+', '-', spec_id)}-{int(self._wall())}"
            command = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "--", *cmdline]
        return self._popen(command, cwd=cwd, env=self._environment(), start_new_session=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

    def _failed_at_once(self, child: Any) -> bool:
        """True when the started process exits with a non-zero status within the watch period."""
        poll = getattr(child, "poll", None)
        if poll is None:
            return False
        deadline = self._clock() + START_WATCH_SECONDS
        while True:
            code = poll()
            if code is not None:
                return code != 0
            if self._clock() >= deadline:
                return False
            self._sleep(_POLL_SECONDS)

    def _start_one(self, ref: Ref) -> bool:
        """Start one ref again; False when it could not be started."""
        spec = gpu.find_spec(self._get_config, "process", ref, include_disabled=True)
        if spec is None:
            return False
        if not getattr(spec, "restart", False):
            util.log(f"process target {spec.id!r}: restart is off; left stopped")
            return True
        cmdline = ref.get("cmdline")
        if not isinstance(cmdline, list) or not cmdline or not all(isinstance(part, str) and part for part in cmdline):
            util.log(f"process target {spec.id!r}: no usable command line was recorded; cannot start it again")
            return False
        if self._matches(spec):
            return True  # already running again
        cwd = ref.get("cwd")
        child = self._launch(spec.id, cmdline, cwd if isinstance(cwd, str) and os.path.isdir(cwd) else None)
        if self._failed_at_once(child):
            util.log(f"process target {spec.id!r}: the restarted program exited at once")
            return False
        return True

    def start(self, refs: Sequence[Ref]) -> Optional[List[Ref]]:
        """Start the processes again; returns the refs that could not be started."""
        failed = []
        for ref in gpu.own_refs(refs, "process", self.name):
            try:
                if not self._start_one(ref):
                    failed.append(ref)
            except Exception as exc:
                util.log(f"process start: {exc}")
                failed.append(ref)
        return failed
