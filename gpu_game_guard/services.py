"""Configured systemd services as a Runtime. Every unit is validated strictly (see `gpu.valid_unit`) and passed to
systemctl after `--`. A start follows the unit and scope recorded in the ref, so it works after the target's spec was
disabled or removed."""
from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import gpu, util
from .model import Ref, Target

CHANGE_TIMEOUT = 120.0  # stopping a service waits for its own shutdown first
QUERY_TIMEOUT = 10.0
_ERROR_MARKERS = ("failed", "error", "denied", "not found")
_LOG_WIDTH = 200  # characters of a failure line
_AUTH_FAILURES = ("interactive authentication required", "access denied", "authentication is required")


def _systemctl(scope: Optional[str], *args: str, unit: str) -> List[str]:
    """A systemctl command line; `--` keeps the unit from being read as an option."""
    return ["systemctl", *(["--user"] if scope == "user" else []), *args, "--", unit]


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
        result = self._run(_systemctl(spec.scope, "is-active", unit=spec.unit), timeout=QUERY_TIMEOUT)
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
            vram = None if by_pid is None else gpu.memory_for_pids(by_pid, self._unit_pids(spec.unit, sorted(gpu.measured_pids(by_pid))))
            extra = {"id": spec.id, "unit": spec.unit, "scope": spec.scope or "system"}
            targets.append(Target("service", self.name, gpu.display_name(spec), "", vram, "", extra))
        return targets

    def _report_failure(self, verb: str, unit: str, scope: str, result: gpu.CommandResult) -> None:
        """Log a failed systemctl call once. The detail is the line that names the error rather than the last
        line, and the polkit hint is kept whole: the detail is cut first to make the line fit."""
        lines = [line.strip() for line in (result.stderr.strip() or result.stdout.strip()).splitlines() if line.strip()]
        marked = [line for line in lines if any(marker in line.lower() for marker in _ERROR_MARKERS)]
        text = marked[0] if marked else lines[-1] if lines else f"exit status {result.returncode}"
        refused = any(marker in " ".join(lines).lower() for marker in _AUTH_FAILURES)
        key = f"systemctl {verb} {unit}"
        if scope == "system" and refused:
            hint = f"; run 'sudo gpu-game-guard polkit allow {unit}' to allow it without a password"
            head = "not authorised ("
            room = max(40, _LOG_WIDTH - len(head) - len(")") - len(hint))
            message = f"{head}{gpu.clip(text, room)}){hint}"
        else:
            message = gpu.clip(text, _LOG_WIDTH)
        gpu.log_once(key, message, limit=max(_LOG_WIDTH, len(message)))

    def _resolve(self, verb: str, ref: Ref) -> Optional[Tuple[str, str]]:
        """(unit, scope) to act on for a ref, or None after logging why it is refused.

        A stop follows the live configuration only. A start (the spec may have been disabled or
        removed since the stop) follows the ref's own unit and scope, falling back to the live configuration for a
        ref that records none. Either way the unit must pass the strict unit rule.
        """
        own_unit, own_scope = ref.get("unit"), ref.get("scope") or "system"
        spec = None
        if verb == "stop" or own_unit is None:
            spec = gpu.find_spec(self._get_config, "service", ref, include_disabled=verb == "start")
        candidates = []
        if verb == "start":
            candidates.append((own_unit, own_scope))
        if spec is not None:
            candidates.append((spec.unit, spec.scope or "system"))
        for unit, scope in candidates:
            if unit is None:
                continue
            if not gpu.valid_unit(unit) or scope not in ("system", "user"):
                gpu.log_once(f"service unit {unit}", f"service: refusing unit {str(unit)[:100]!r} (scope {str(scope)[:20]!r}): not a valid service unit")
                return None
            return unit, scope
        return None

    def _change(self, verb: str, ref: Ref) -> bool:
        """Run `stop` or `start` for one ref; True when systemctl succeeded. Never raises."""
        try:
            resolved = self._resolve(verb, ref)
            if resolved is None:
                return False
            unit, scope = resolved
            result = self._run(_systemctl(scope, "--no-ask-password", verb, unit=unit), timeout=CHANGE_TIMEOUT)
            if result is None:
                return False
            if result.returncode != 0:
                self._report_failure(verb, unit, scope, result)
                return False
            gpu.clear_failure(f"systemctl {verb} {unit}")
            return True
        except Exception as exc:
            util.log(f"service {verb}: {exc}")
            return False

    def stop(self, refs: Sequence[Ref]) -> None:
        own = gpu.own_refs(refs, "service", self.name)
        if not own:
            return
        with ThreadPoolExecutor(max_workers=len(own)) as pool:
            list(pool.map(lambda ref: self._change("stop", ref), own))

    def start(self, refs: Sequence[Ref]) -> Optional[List[Ref]]:
        """Start the services; returns the refs that could not be started."""
        return [ref for ref in gpu.own_refs(refs, "service", self.name) if not self._change("start", ref)]
