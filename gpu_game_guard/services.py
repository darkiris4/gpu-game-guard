"""Configured systemd services as a Runtime."""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import gpu, util
from .model import Ref, Target

CHANGE_TIMEOUT = 120.0  # stopping a service waits for its own shutdown first
QUERY_TIMEOUT = 10.0
_AUTH_FAILURES = ("interactive authentication required", "access denied", "failed to stop")


def _systemctl(spec: Any, *args: str) -> List[str]:
    return ["systemctl", *(["--user"] if getattr(spec, "scope", None) == "user" else []), *args]


class ServiceRuntime:
    """A configured `service` target is running while `systemctl is-active` says so."""

    name = "service"

    def __init__(self, get_config: Callable[[], Any], run: Optional[Callable[..., Optional[gpu.CommandResult]]] = None,
                 proc: Path = gpu.PROC, vram: Optional[Callable[[], Optional[Dict[int, int]]]] = None,
                 which: Callable[[str], Optional[str]] = shutil.which):
        self._get_config = get_config
        self._run = run or gpu.run_process
        self._proc = proc
        self._vram = vram or (lambda: gpu.vram_by_pid(self._proc))
        self._which = which

    def installed(self) -> bool:
        return True

    def available(self) -> bool:
        return self._which("systemctl") is not None

    def _is_active(self, spec: Any) -> bool:
        result = self._run(_systemctl(spec, "is-active", spec.unit), timeout=QUERY_TIMEOUT)
        return result is not None and result.stdout.strip() == "active"

    def _unit_pids(self, unit: str, pids: Sequence[int]) -> List[int]:
        found = []
        for pid in pids:
            try:
                if gpu.cgroup_in_unit((self._proc / str(pid) / "cgroup").read_text(), unit):
                    found.append(pid)
            except OSError:
                continue
        return found

    def running_gpu_targets(self) -> List[Target]:
        active = [spec for spec in gpu.usable_specs(self._get_config, "service") if self._is_active(spec)]
        if not active:
            return []
        try:
            by_pid = self._vram()
        except Exception as exc:
            util.log(f"GPU memory lookup failed ({exc})")
            by_pid = None
        targets = []
        for spec in active:
            vram = None if by_pid is None else gpu.memory_for_pids(by_pid, self._unit_pids(spec.unit, list(by_pid)))
            extra = {"id": spec.id, "unit": spec.unit, "scope": spec.scope or "system"}
            targets.append(Target("service", self.name, gpu.display_name(spec), "", vram, "", extra))
        return targets

    def _report_failure(self, verb: str, spec: Any, result: gpu.CommandResult) -> None:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        text = detail[-1] if detail else f"exit status {result.returncode}"
        refused = any(marker in " ".join(detail).lower() for marker in _AUTH_FAILURES)
        if (spec.scope or "system") == "system" and refused:
            gpu.log_once(f"service {verb} {spec.unit}",
                         f"systemctl {verb} {spec.unit}: not authorised ({text}); "
                         f"run 'sudo gpu-game-guard polkit allow {spec.unit}' to allow it without a password")
        else:
            gpu.log_once(f"service {verb} {spec.unit}", f"systemctl {verb} {spec.unit}: {text}")

    def _change(self, verb: str, ref: Ref) -> None:
        try:
            spec = gpu.find_spec(self._get_config, "service", ref)
            if spec is None:
                return
            result = self._run(_systemctl(spec, "--no-ask-password", verb, spec.unit), timeout=CHANGE_TIMEOUT)
            if result is None:
                return
            if result.returncode != 0:
                self._report_failure(verb, spec, result)
            else:
                gpu.clear_failure(f"service {verb} {spec.unit}")
        except Exception as exc:
            util.log(f"service {verb}: {exc}")

    def stop(self, refs: Sequence[Ref]) -> None:
        own = gpu.own_refs(refs, "service", self.name)
        if not own:
            return
        with ThreadPoolExecutor(max_workers=len(own)) as pool:
            list(pool.map(lambda ref: self._change("stop", ref), own))

    def start(self, refs: Sequence[Ref]) -> None:
        for ref in gpu.own_refs(refs, "service", self.name):
            self._change("start", ref)
