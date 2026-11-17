"""GPU memory figures for any vendor.

NVIDIA is read through `nvidia-smi`. AMD and Intel are read from kernel interfaces: `/sys/class/drm` for the
system totals and `/proc/<pid>/fdinfo` for per-process memory. Every function returns an empty result when its
tool or files are missing; nothing here raises.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import util

# A command runner takes an argument list and returns stdout, or None when the command could not be run,
# exited non-zero or timed out. Implementations log at most one line for a failure.
Runner = Callable[..., Optional[str]]

DEFAULT_TIMEOUT = 10.0
PROC = Path("/proc")
DRM = Path("/sys/class/drm")

_CONTAINER_CGROUP = re.compile(
    r"(?:docker|libpod)-([0-9a-f]{64})\.scope"
    r"|/docker/([0-9a-f]{64})"
    r"|libpod-([0-9a-f]{64})(?:/|$)",
    re.MULTILINE,
)
_FDINFO_KEY = re.compile(r"^drm-(?:memory|resident|total)-vram\d*$")
_FDINFO_VALUE = re.compile(r"^(\d+)\s*([KMG]iB)?$", re.IGNORECASE)
_UNIT_TO_KIB = {"kib": 1, "mib": 1024, "gib": 1024 * 1024}


_last_failure: Dict[str, str] = {}


def _report_failure(command: str, message: str) -> None:
    """Log a failure once; the same failure of the same command stays quiet until the command works again."""
    if _last_failure.get(command) != message:
        _last_failure[command] = message
        util.log(f"{command}: {message}")


def run_command(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Run a command without a shell and return its stdout, or None after logging the failure once."""
    command = args[0]
    try:
        done = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    except OSError as exc:
        _report_failure(command, f"cannot run ({exc})")
        return None
    except subprocess.SubprocessError as exc:
        _report_failure(command, str(exc))
        return None
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        _report_failure(command, f"exited with status {done.returncode}" + (f" ({detail[-1]})" if detail else ""))
        return None
    _last_failure.pop(command, None)
    return done.stdout


def container_id_from_cgroup(text: str) -> Optional[str]:
    """The container id a /proc/<pid>/cgroup file places the process in, if it is in a container."""
    match = _CONTAINER_CGROUP.search(text)
    return next(group for group in match.groups() if group) if match else None


def _nvidia_query(run: Runner, *args: str) -> List[List[str]]:
    out = run(["nvidia-smi", *args, "--format=csv,noheader,nounits"])
    if not out:
        return []
    return [[field.strip() for field in line.split(",")] for line in out.splitlines() if line.strip()]


def _nvidia_totals(run: Runner) -> Optional[Tuple[int, int]]:
    used = total = 0
    found = False
    for row in _nvidia_query(run, "--query-gpu=memory.used,memory.total"):
        try:
            used += int(float(row[0]))
            total += int(float(row[1]))
        except (IndexError, ValueError):
            continue
        found = True
    return (used, total) if found and total > 0 else None


def _sysfs_totals(drm: Path) -> Optional[Tuple[int, int]]:
    used = total = 0
    try:
        cards = sorted(drm.glob("card[0-9]*/device"))
    except OSError:
        return None
    for device in cards:
        try:
            card_used = int((device / "mem_info_vram_used").read_text().strip())
            card_total = int((device / "mem_info_vram_total").read_text().strip())
        except (OSError, ValueError):
            continue
        used += card_used
        total += card_total
    if total <= 0:
        return None
    return used // (1024 * 1024), total // (1024 * 1024)


def totals(run: Optional[Runner] = None, drm: Path = DRM) -> Optional[Tuple[int, int]]:
    """(used_mib, total_mib) summed over all GPUs, or None when no backend knows."""
    return _nvidia_totals(run or run_command) or _sysfs_totals(drm)


def _nvidia_by_container(run: Runner, proc: Path) -> Dict[str, int]:
    used: Dict[str, int] = {}
    for row in _nvidia_query(run, "--query-compute-apps=pid,used_memory"):
        try:
            pid, mib = row[0], int(float(row[1]))
            container = container_id_from_cgroup((proc / pid / "cgroup").read_text())
        except (IndexError, ValueError, OSError):
            continue
        if container:
            used[container] = used.get(container, 0) + mib
    return used


def _fdinfo_clients(text: str, fallback: str) -> Optional[Tuple[str, int]]:
    """(client id, KiB of VRAM) for one fdinfo file of a DRM device, or None for anything else."""
    driver = client = None
    kib = 0
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "drm-driver":
            driver = value
        elif key == "drm-client-id":
            client = value
        elif _FDINFO_KEY.match(key):
            match = _FDINFO_VALUE.match(value)
            if match:
                unit = (match.group(2) or "KiB").lower()
                kib = max(kib, int(match.group(1)) * _UNIT_TO_KIB[unit])
    if driver is None:
        return None
    return (client if client is not None else fallback), kib


def _fdinfo_by_container(proc: Path) -> Dict[str, int]:
    used_kib: Dict[str, int] = {}
    try:
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return {}
    for entry in entries:
        try:
            fds = list((entry / "fdinfo").iterdir())
        except OSError:
            continue
        clients: Dict[str, int] = {}
        for fd in fds:
            try:
                parsed = _fdinfo_clients(fd.read_text(errors="replace"), "fd" + fd.name)
            except OSError:
                continue
            if parsed and parsed[0] not in clients:
                clients[parsed[0]] = parsed[1]
        held = sum(clients.values())
        if held <= 0:
            continue
        try:
            container = container_id_from_cgroup((entry / "cgroup").read_text())
        except OSError:
            continue
        if container:
            used_kib[container] = used_kib.get(container, 0) + held
    return {container: kib // 1024 for container, kib in used_kib.items()}


def vram_by_container(proc: Path = PROC, run: Optional[Runner] = None) -> Dict[str, int]:
    """Full container id -> MiB of GPU memory held by its processes, from the first backend with data."""
    return _nvidia_by_container(run or run_command, proc) or _fdinfo_by_container(proc)
