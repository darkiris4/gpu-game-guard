"""GPU memory figures for any vendor.

NVIDIA is read through `nvidia-smi`. AMD and Intel are read from kernel interfaces: `/sys/class/drm` for the
system totals and `/proc/<pid>/fdinfo` for per-process memory. Every function returns an empty or None result when its
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
_FDINFO_KEY = re.compile(r"^drm-(?:memory|resident|total)-(?:vram|local)\d*$")
_FDINFO_VALUE = re.compile(r"^(\d+)\s*([KMG]iB)?$", re.IGNORECASE)
_UNIT_TO_KIB = {"kib": 1, "mib": 1024, "gib": 1024 * 1024}


_MAX_FAILURE_KEYS = 64
_last_failure: Dict[str, str] = {}


def _command_key(args: Sequence[str]) -> str:
    """The command and its subcommand, which is what a failure is tracked against."""
    return " ".join(str(arg) for arg in list(args)[:2])


def _report_failure(command: str, message: str) -> None:
    """Log a failure once; the same failure of the same command stays quiet until the command works again."""
    if _last_failure.get(command) != message:
        if command not in _last_failure and len(_last_failure) >= _MAX_FAILURE_KEYS:
            _last_failure.clear()
        _last_failure[command] = message
        util.log(f"{command}: {message}")


def run_command(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT, keep_output: bool = False) -> Optional[str]:
    """Run a command without a shell and return its stdout, or None after logging the failure once.

    With `keep_output`, a non-zero exit that still produced stdout is logged but returns that stdout, for
    commands that report partial results while failing for part of their input.
    """
    command = _command_key(args)
    try:
        done = subprocess.run(list(args), capture_output=True, text=True, errors="replace", timeout=timeout)
    except OSError as exc:
        _report_failure(command, f"cannot run ({exc})")
        return None
    except subprocess.SubprocessError as exc:
        _report_failure(command, str(exc))
        return None
    except ValueError as exc:  # an argument the operating system cannot take, such as an embedded NUL
        _report_failure(command, f"cannot run ({exc})")
        return None
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        _report_failure(command, f"exited with status {done.returncode}" + (f" ({detail[-1]})" if detail else ""))
        return done.stdout if keep_output and (done.stdout or "").strip() else None
    _last_failure.pop(command, None)
    return done.stdout


def container_id_from_cgroup(text: str) -> Optional[str]:
    """The container id a /proc/<pid>/cgroup file places the process in, if it is in a container."""
    match = _CONTAINER_CGROUP.search(text)
    return next(group for group in match.groups() if group) if match else None


def _nvidia_query(run: Runner, *args: str) -> Optional[List[List[str]]]:
    """Rows of a query, an empty list when it ran and found nothing, None when it could not run."""
    out = run(["nvidia-smi", *args, "--format=csv,noheader,nounits"])
    if out is None:
        return None
    return [[field.strip() for field in line.split(",")] for line in out.splitlines() if line.strip()]


def _nvidia_totals(run: Runner) -> Optional[Tuple[int, int]]:
    used = total = 0
    found = False
    for row in _nvidia_query(run, "--query-gpu=memory.used,memory.total") or []:
        try:
            row_used, row_total = int(float(row[0])), int(float(row[1]))
        except (IndexError, ValueError, OverflowError):
            continue
        used += row_used
        total += row_total
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


class Measurement(Dict[str, int]):
    """Container id -> MiB of GPU memory. `authoritative` is True when a container missing from it is known to
    hold none (nvidia-smi lists every compute process) and False when the source could not see every
    process (fdinfo of other users' processes is unreadable), so a missing container is unknown."""

    authoritative = True


def _nvidia_by_container(run: Runner, proc: Path) -> Optional[Dict[str, int]]:
    """None when nvidia-smi could not run; otherwise the figures, empty when no compute app is in a container."""
    rows = _nvidia_query(run, "--query-compute-apps=pid,used_memory")
    if rows is None:
        return None
    used = Measurement()
    for row in rows:
        try:
            pid, mib = row[0], int(float(row[1]))
            container = container_id_from_cgroup((proc / pid / "cgroup").read_text())
        except (IndexError, ValueError, OverflowError, OSError):
            continue
        if container:
            used[container] = used.get(container, 0) + mib
    return used


def _fdinfo_clients(text: str, fallback: str) -> Optional[Tuple[str, int]]:
    """(client key, KiB of VRAM) for one fdinfo file of a DRM device, or None for anything else.

    Client ids are only unique per device, so the key includes the device address when the file names one.
    """
    driver = client = pdev = None
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
        elif key == "drm-pdev":
            pdev = value
        elif _FDINFO_KEY.match(key):
            match = _FDINFO_VALUE.match(value)
            if match:
                unit = (match.group(2) or "KiB").lower()
                kib = max(kib, int(match.group(1)) * _UNIT_TO_KIB[unit])
    if driver is None:
        return None
    ident = client if client is not None else fallback
    return (f"{pdev}/{ident}" if pdev else ident), kib


def _fdinfo_by_container(proc: Path) -> Optional[Dict[str, int]]:
    """None when no process exposed a DRM fdinfo file, so nothing could be measured."""
    used_kib: Dict[str, int] = {}
    measured = False
    try:
        entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return None
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
            if parsed is None:
                continue
            measured = True
            if parsed[0] not in clients:
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
    if not measured:
        return None
    result = Measurement((container, kib // 1024) for container, kib in used_kib.items())
    result.authoritative = False
    return result


def vram_by_container(proc: Path = PROC, run: Optional[Runner] = None) -> Optional[Dict[str, int]]:
    """Full container id -> MiB of GPU memory held by its processes.

    None means no backend could measure GPU memory; a dict, possibly empty, means it was measured and
    containers missing from it hold none. A successful nvidia-smi query is authoritative and the fdinfo
    walk only runs when nvidia-smi is unavailable.
    """
    measured = _nvidia_by_container(run or run_command, proc)
    if measured is not None:
        return measured
    return _fdinfo_by_container(proc)
