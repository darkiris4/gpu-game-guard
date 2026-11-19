"""Configured commands as a Runtime: a stop command, a start command and an optional running check.

A target with a `check` is running while the check exits 0. A target without one has nothing to ask, so the runtime
keeps a record in memory: after a `stop` command exits with status 0 the target counts as stopped (not running)
until `start` runs for it or `release` drops it, and it counts as running otherwise. The guard persists the stopped
refs, so `adopt` restores the record of every check-less target among them when the guard starts; without it a new
process would assume every check-less target is running."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Set

from . import gpu, util
from .model import Ref, Target

DEFAULT_TIMEOUT = 60.0


def _timeout(spec: Any) -> float:
    value = getattr(spec, "timeout_seconds", None)
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    return float(value) if ok else DEFAULT_TIMEOUT


class CommandRuntime:
    """A configured `command` target is running while its check exits 0, or always when it has no check."""

    name = "command"

    def __init__(self, get_config: Callable[[], Any], run: Optional[Callable[..., Optional[gpu.CommandResult]]] = None,
                 proc: Path = gpu.PROC):
        self._get_config = get_config
        self._run = run or gpu.run_process
        self._proc = proc
        self._stopped: Set[str] = set()  # ids of targets without a check whose stop command succeeded

    def installed(self) -> bool:
        return True

    def available(self) -> bool:
        return True

    def _is_running(self, spec: Any) -> bool:
        if not spec.check:
            return spec.id not in self._stopped
        result = self._run(list(spec.check), timeout=_timeout(spec))
        return result is not None and result.returncode == 0

    def running_gpu_targets(self) -> List[Target]:
        return [Target("command", self.name, gpu.display_name(spec), "", None, "", {"id": spec.id})
                for spec in gpu.usable_specs(self._get_config, "command") if self._is_running(spec)]

    def _change(self, verb: str, ref: Ref) -> bool:
        """Run the `stop` or `start` command of a ref; True when it exited with status 0. Never raises."""
        try:
            spec = gpu.find_spec(self._get_config, "command", ref, include_disabled=verb == "start")
            if spec is None:
                return False
            result = self._run(list(spec.stop if verb == "stop" else spec.start), timeout=_timeout(spec))
            if result is None:
                return False
            if result.returncode != 0:
                detail = result.stderr.strip().splitlines()
                gpu.log_once(f"command {verb} {spec.id}",
                             f"{spec.id} {verb} command exited with status {result.returncode}"
                             + (f" ({detail[-1]})" if detail else ""))
                return False
            gpu.clear_failure(f"command {verb} {spec.id}")
            if verb == "stop":
                self._stopped.add(spec.id)
            return True
        except Exception as exc:
            util.log(f"command {verb}: {exc}")
            return False

    def adopt(self, refs: Sequence[Ref]) -> None:
        """Mark the check-less targets among the refs loaded from the state file as stopped."""
        for ref in gpu.own_refs(refs, "command", self.name):
            target_id = ref.get("id")
            if not isinstance(target_id, str):
                continue
            spec = gpu.find_spec(self._get_config, "command", ref, include_disabled=True)
            if spec is None or not spec.check:
                self._stopped.add(target_id)

    def release(self, refs: Sequence[Ref]) -> None:
        """Clear the stopped mark of refs the guard dropped without starting them."""
        for ref in gpu.own_refs(refs, "command", self.name):
            if isinstance(ref.get("id"), str):
                self._stopped.discard(ref["id"])

    def stop(self, refs: Sequence[Ref]) -> None:
        own = gpu.own_refs(refs, "command", self.name)
        if not own:
            return
        with ThreadPoolExecutor(max_workers=len(own)) as pool:
            list(pool.map(lambda ref: self._change("stop", ref), own))

    def start(self, refs: Sequence[Ref]) -> Optional[List[Ref]]:
        """Run the start commands; returns the refs whose command failed or whose target is no longer configured.
        Running a start clears the target's stopped mark."""
        failed = []
        for ref in gpu.own_refs(refs, "command", self.name):
            if isinstance(ref.get("id"), str):
                self._stopped.discard(ref["id"])
            if not self._change("start", ref):
                failed.append(ref)
        return failed
