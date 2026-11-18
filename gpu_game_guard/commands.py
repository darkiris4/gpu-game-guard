"""Configured commands as a Runtime: a stop command, a start command and an optional running check."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

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

    def installed(self) -> bool:
        return True

    def available(self) -> bool:
        return True

    def _is_running(self, spec: Any) -> bool:
        if not spec.check:
            return True
        result = self._run(list(spec.check), timeout=_timeout(spec))
        return result is not None and result.returncode == 0

    def running_gpu_targets(self) -> List[Target]:
        return [Target("command", self.name, gpu.display_name(spec), "", None, "", {"id": spec.id})
                for spec in gpu.usable_specs(self._get_config, "command") if self._is_running(spec)]

    def _change(self, verb: str, ref: Ref) -> None:
        try:
            spec = gpu.find_spec(self._get_config, "command", ref)
            if spec is None:
                return
            result = self._run(list(spec.stop if verb == "stop" else spec.start), timeout=_timeout(spec))
            if result is None:
                return
            if result.returncode != 0:
                detail = result.stderr.strip().splitlines()
                gpu.log_once(f"command {verb} {spec.id}",
                             f"{spec.id} {verb} command exited with status {result.returncode}"
                             + (f" ({detail[-1]})" if detail else ""))
            else:
                gpu.clear_failure(f"command {verb} {spec.id}")
        except Exception as exc:
            util.log(f"command {verb}: {exc}")

    def stop(self, refs: Sequence[Ref]) -> None:
        own = gpu.own_refs(refs, "command", self.name)
        if not own:
            return
        with ThreadPoolExecutor(max_workers=len(own)) as pool:
            list(pool.map(lambda ref: self._change("stop", ref), own))

    def start(self, refs: Sequence[Ref]) -> None:
        for ref in gpu.own_refs(refs, "command", self.name):
            self._change("start", ref)
