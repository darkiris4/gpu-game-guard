"""Finding what could be paused: GPU containers, then host services and processes that hold GPU memory."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import gpu, util
from .model import Runtime

# Names that are never offered and never stopped: display servers and compositors, audio and session plumbing,
# login managers, session daemons, Steam and its helpers, terminals and shells, file managers, and the guard itself.
BUILTIN_PROTECT: Tuple[str, ...] = (
    "Xorg*", "X", "Xwayland*", "Xvfb", "kwin*", "plasmashell", "plasma*", "gnome-shell*", "gnome-session*", "mutter*",
    "cosmic-*", "sway*", "Hyprland", "hyprland*", "weston*", "labwc", "wayfire", "river", "niri", "picom", "xfwm4",
    "xfce4-*", "openbox", "i3", "cinnamon*", "muffin", "budgie-*", "marco", "mate-*", "lxqt-*",
    "pipewire*", "wireplumber", "pulseaudio", "pulse*", "jackd", "systemd*", "init", "dbus*", "polkit*", "udisks*",
    "upower*", "NetworkManager*", "wpa_supplicant", "bluetoothd", "logind", "elogind*", "agetty", "login",
    "gnome-keyring*", "xdg-desktop-portal*", "org.gnome.Shell*", "ibus*", "fcitx*", "at-spi*", "gvfs*", "cups*",
    "accounts-daemon", "colord", "rtkit*", "gamescope*",
    "gdm*", "sddm*", "lightdm*", "greetd", "ly", "lxdm", "display-manager*",
    "steam*", "reaper", "pressure-vessel*", "srt-*", "steamwebhelper",
    "bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "ssh*", "sshd*", "tmux*", "screen", "konsole",
    "gnome-terminal*", "xterm", "kitty", "alacritty", "wezterm*", "foot", "terminator", "tilix", "xfce4-terminal",
    "nautilus", "dolphin", "thunar", "nemo", "pcmanfm*", "caja", "krusader", "doublecmd", "files",
    "gpu-game-guard*",
)

_GUARD_MARKERS = ("gpu-game-guard", "gpu_game_guard")
_PYTHON = re.compile(r"python[0-9.]*")
_GLOB_CHARS = re.compile(r"[*?\[\]/]")
_ID_CHARS = re.compile(r"[^a-z0-9_-]+")

MIN_MATCH = 3  # a process name shorter than this cannot be managed (see processes.MIN_LITERAL)

RISK_LOW, RISK_MEDIUM, RISK_HIGH = "low", "medium", "high"


def is_guard_cmdline(cmdline: Optional[Sequence[str]]) -> bool:
    """True when a command line is the guard itself (a script or module named after it)."""
    return any(marker in arg for arg in cmdline or [] for marker in _GUARD_MARKERS)


def matches_any(name: str, patterns: Sequence[str]) -> bool:
    """True when `name` matches one of the globs, ignoring case."""
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, str(pattern).lower()) for pattern in patterns)


def _protect_names(unit: str) -> List[str]:
    """The names a service unit is checked under: the unit, its stem and, for a desktop-launched application
    (`app-<name>@<instance>.service`), the bare application name without the `app-` prefix and the instance."""
    stem = unit[:-len(".service")] if unit.endswith(".service") else unit
    names = [unit, stem]
    if stem.startswith("app-"):
        names.append(stem[len("app-"):].partition("@")[0])
    return names


def stable_stem(unit: str) -> str:
    """The unit's name without `.service` and, for `app-<name>@<instance>`, without the prefix and the random
    instance, so that an id made from it is the same on every launch."""
    stem = unit[:-len(".service")] if unit.endswith(".service") else unit
    if stem.startswith("app-") and "@" in stem:
        return stem[len("app-"):].partition("@")[0] or stem
    return stem


def is_protected(config: Any, name: str, cmdline: Optional[Sequence[str]] = None) -> bool:
    """True when a process or unit name must never be offered or stopped: the built-in list, the configured
    additions, and a Python interpreter running the guard."""
    if matches_any(name, BUILTIN_PROTECT):
        return True
    if _PYTHON.fullmatch(name.lower()) and is_guard_cmdline(cmdline):
        return True
    check = getattr(config, "is_protected", None)
    if callable(check):
        try:
            return bool(check(name))
        except Exception as exc:
            util.log(f"protect list: {exc}")
    return False


@dataclass
class Candidate:
    key: str
    kind: str  # "container", "service" or "process"
    name: str
    label: str
    detail: str
    vram_mib: Optional[int]
    managed: bool
    risk: str  # RISK_LOW, RISK_MEDIUM or RISK_HIGH
    spec: Optional[dict]  # a `targets` entry that would manage it; None for auto-detected containers
    note: str


def _slug(text: str, taken: Set[str]) -> str:
    base = _ID_CHARS.sub("-", text.lower()).strip("-_") or "target"
    base = base[:40].rstrip("-_") or "target"
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        suffix = f"-{number}"
        candidate = base[:40 - len(suffix)] + suffix
    taken.add(candidate)
    return candidate


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _cmdline(proc: Path, pid: int) -> List[str]:
    text = _read(proc / str(pid) / "cmdline")
    return [part for part in (text or "").split("\0") if part]


def _containers(config: Any, runtimes: Sequence[Runtime]) -> List[Candidate]:
    found = []
    for runtime in runtimes:
        try:
            targets = runtime.running_gpu_targets()
        except Exception as exc:
            util.log(f"discovery: {getattr(runtime, 'name', '?')}: {exc}")
            continue
        for target in targets:
            if target.kind != "container":
                continue
            ignored = bool(getattr(config, "is_ignored", lambda name: False)(target.name))
            managed = bool(getattr(config, "auto_containers", True)) and not ignored
            note = "Stopped before a game and started again afterwards. Applications using it lose their connection meanwhile."
            if ignored:
                note = "Ignored through ignore_containers. " + note
            found.append(Candidate(f"container:{target.runtime}:{target.name}", "container", target.name, target.name,
                                   f"{target.runtime} container" + (f", image {target.image}" if target.image else ""),
                                   target.vram_mib, managed, RISK_LOW, None, note))
    return sorted(found, key=lambda c: (-(c.vram_mib or 0), c.name))


def _managed_specs(config: Any) -> List[Any]:
    targets = getattr(config, "targets", None)
    return list(targets) if isinstance(targets, (list, tuple)) else []


def _owner(proc: Path, pid: int) -> Optional[int]:
    """The real uid of a process from its /proc status file, or None when unreadable."""
    for line in (_read(proc / str(pid) / "status") or "").splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            if len(fields) > 1 and fields[1].isdigit():
                return int(fields[1])
    return None


def discover(config: Any, runtimes: Sequence[Runtime], proc: Path = gpu.PROC, run: Optional[gpu.Runner] = None,
             uid: Optional[int] = None) -> List[Candidate]:
    """What could be paused, safest first: auto containers, then services, then processes. Protected names are
    left out, for a service also when one of its GPU processes has a protected name; what a configured target
    already manages is marked `managed` rather than omitted. Only the current user's processes and user services
    are offered, together with system services."""
    candidates = _containers(config, runtimes)
    try:
        by_pid = gpu.vram_by_pid(proc, run)
    except Exception as exc:
        util.log(f"discovery: GPU memory lookup failed ({exc})")
        by_pid = None
    if not by_pid:
        return candidates
    specs = _managed_specs(config)
    taken = {str(getattr(spec, "id", "")) for spec in specs}
    own_pid = os.getpid()
    own_uid = os.getuid() if uid is None else uid
    units: Dict[Tuple[str, str], List[int]] = {}
    unit_comms: Dict[Tuple[str, str], Set[str]] = {}
    unit_cmdlines: Dict[Tuple[str, str], List[List[str]]] = {}
    processes: Dict[str, List[int]] = {}
    for pid in sorted(by_pid):
        if pid == own_pid:
            continue
        cgroup = _read(proc / str(pid) / "cgroup")
        if cgroup is None or gpu.container_id_from_cgroup(cgroup):
            continue
        comm = (_read(proc / str(pid) / "comm") or "").strip()
        cmdline = _cmdline(proc, pid)
        service = gpu.service_from_cgroup(cgroup)
        if service:
            manager = gpu.user_manager_uid(cgroup)
            if manager is not None and manager != own_uid:
                continue
            units.setdefault(service, []).append(pid)
            unit_comms.setdefault(service, set()).add(comm)
            unit_cmdlines.setdefault(service, []).append(cmdline)
        elif (comm and len(comm) >= MIN_MATCH and _owner(proc, pid) == own_uid and not is_protected(config, comm, cmdline)
              and not _GLOB_CHARS.search(comm)):
            processes.setdefault(comm, []).append(pid)
    for service in list(units):
        names = _protect_names(service[1]) + sorted(unit_comms[service])
        if any(is_protected(config, name, cmdline) for name in names for cmdline in unit_cmdlines[service]):
            del units[service]

    def total(pids: List[int]) -> int:
        return sum(by_pid.get(pid, 0) for pid in pids)

    services = []
    for (scope, unit), pids in units.items():
        stem = stable_stem(unit)
        managed = any(getattr(spec, "kind", None) == "service" and getattr(spec, "unit", None) == unit
                      and (getattr(spec, "scope", None) or "system") == scope for spec in specs)
        spec_dict = {"id": _slug(stem, taken), "kind": "service", "label": stem, "unit": unit, "scope": scope}
        note = (f"Stopped with systemctl{' --user' if scope == 'user' else ''} and started again afterwards. "
                + ("" if scope == "user" else f"Without a password rule this may fail: run 'sudo gpu-game-guard polkit allow {unit}'. ")
                + "Anything relying on the service stops working meanwhile.")
        services.append(Candidate(f"service:{scope}:{unit}", "service", unit, stem,
                                  f"{scope} service {unit}, {len(pids)} GPU process{'es' if len(pids) != 1 else ''}",
                                  total(pids), managed, RISK_LOW, spec_dict, note))
    procs = []
    for comm, pids in processes.items():
        managed = any(getattr(spec, "kind", None) == "process" and isinstance(getattr(spec, "match", None), str)
                      and fnmatch.fnmatchcase(comm, spec.match) for spec in specs)
        spec_dict = {"id": _slug(comm, taken), "kind": "process", "label": comm, "match": comm, "restart": True}
        note = ("Stopping a process ends it, so unsaved work in it may be lost. It is started again afterwards with "
                "the same command line, which may not restore its previous state.")
        procs.append(Candidate(f"process:{comm}", "process", comm, comm,
                               f"{len(pids)} process{'es' if len(pids) != 1 else ''} using the GPU (pid {', '.join(map(str, pids[:5]))})",
                               total(pids), managed, RISK_HIGH, spec_dict, note))
    order = lambda c: (-(c.vram_mib or 0), c.name)  # noqa: E731
    return candidates + sorted(services, key=order) + sorted(procs, key=order)
