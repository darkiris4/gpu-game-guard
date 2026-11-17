"""The state machine: intercept launches that meet GPU workloads, act on the chosen answer, and offer to
start what was stopped once the game has closed."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .config import Config
from .model import Launch, Ref, Runtime, Target, Trigger
from .state import State
from .util import log

LaunchKey = Tuple[str, Tuple[Any, ...]]  # (trigger name, Launch.key)


class Guard:
    def __init__(self, watcher, triggers: Sequence[Trigger], runtimes: Sequence[Runtime], prompter, state: State,
                 gpu_totals: Callable[[], Optional[Tuple[int, int]]] = lambda: None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.watcher = watcher
        self.triggers = list(triggers)
        self.runtimes = list(runtimes)
        self.prompter = prompter
        self.state = state
        self.gpu_totals = gpu_totals
        self.clock, self.wall = clock, wall
        self.seen: Set[LaunchKey] = set()  # launches already looked at; those running at startup are left alone
        self.ended: Set[LaunchKey] = set()  # launches the guard ended; their processes may linger for a moment
        self.bypass: Dict[str, float] = {}  # game id -> time until which a launch is let through
        self.last_active = self.clock()
        self.retry_at = 0.0
        self.seen = {key for _launch, key in self._scan()}

    def _scan(self) -> List[Tuple[Launch, LaunchKey]]:
        found = []
        for trigger in self.triggers:
            try:
                launches = trigger.scan()
            except Exception as exc:
                log(f"trigger {trigger.name} failed: {exc!r}")
                # keep what was known about this trigger so one failed scan does not make its launches look new
                found.extend((None, key) for key in self.seen if key[0] == trigger.name)
                continue
            found.extend((launch, (trigger.name, launch.key)) for launch in launches)
        return found

    def _targets(self, config: Config) -> List[Target]:
        targets: List[Target] = []
        for runtime in self.runtimes:
            try:
                targets.extend(runtime.running_gpu_targets())
            except Exception as exc:
                log(f"runtime {runtime.name} failed: {exc!r}")
        return [t for t in targets if not config.is_ignored(t.name)]

    def step(self) -> None:
        now = self.clock()
        config = self.watcher.current()
        scanned = self._scan()
        current = {key for _launch, key in scanned}
        self.seen &= current
        self.ended &= current
        active = False
        for launch, key in scanned:
            if key in self.ended:
                continue
            if launch is None:
                active = True
                continue
            if key not in self.seen:
                self.seen.add(key)
                if self._should_intercept(launch, config, now):
                    targets = self._targets(config)
                    if targets:
                        self.ended.add(key)
                        self._intercept(launch, targets, config)
                        continue
            active = True
        if active:
            self.last_active = now
            if self.state.stopped and not self.state.game_seen:
                self.state.game_seen = True
                self.state.save()
            return
        self._maybe_restart(now, config)

    def _should_intercept(self, launch: Launch, config: Config, now: float) -> bool:
        if config.action_for(launch.game_id) == "ignore":
            return False
        return self.bypass.get(launch.game_id, 0) <= now

    def _trigger(self, name: str) -> Optional[Trigger]:
        return next((t for t in self.triggers if t.name == name), None)

    def _intercept(self, launch: Launch, targets: List[Target], config: Config) -> None:
        names = ", ".join(t.name for t in targets)
        action = config.action_for(launch.game_id)
        log(f"launch of {launch.game_id} ({launch.title}) with GPU workloads running: {names}")
        trigger = self._trigger(launch.trigger)
        try:
            if trigger is not None:
                trigger.end(launch)
        except Exception as exc:
            log(f"could not end the launch of {launch.game_id}: {exc!r}")
        if action == "stop":
            choice = "stop"
        else:
            try:
                choice = self.prompter.launch(launch, targets, self.gpu_totals())
            except Exception as exc:
                log(f"dialog failed: {exc!r}")
                choice = "anyway"
            if choice not in ("stop", "cancel"):
                choice = "anyway"  # fail safe: an unknown or missing answer lets the game start
        log(f"answer: {choice}")
        if choice == "cancel":
            return
        if choice == "stop":
            self._stop(targets)
            if action == "stop":
                self._notify("GPU Game Guard", f"Stopped {names} for {launch.title or launch.game_id}.")
        self.bypass[launch.game_id] = self.clock() + config.bypass_seconds
        try:
            if trigger is not None:
                trigger.relaunch(launch)
        except Exception as exc:
            log(f"could not relaunch {launch.game_id}: {exc!r}")

    def _stop(self, targets: List[Target]) -> None:
        refs = [t.ref for t in targets]
        self._each_runtime(refs, "stop")
        self.state.add(refs, self.wall())
        self.state.save()

    def _start(self, refs: List[Ref]) -> None:
        self._each_runtime(refs, "start")

    def _each_runtime(self, refs: List[Ref], verb: str) -> None:
        for name in dict.fromkeys(ref["runtime"] for ref in refs):
            mine = [ref for ref in refs if ref["runtime"] == name]
            runtime = next((r for r in self.runtimes if r.name == name), None)
            if runtime is None:
                log(f"cannot {verb} {', '.join(r['name'] for r in mine)}: runtime {name} is not available")
                continue
            try:
                getattr(runtime, verb)(mine)
            except Exception as exc:
                log(f"{verb} through {name} failed: {exc!r}")

    def _notify(self, title: str, body: str) -> None:
        try:
            self.prompter.notify(title, body)
        except Exception as exc:
            log(f"notification failed: {exc!r}")

    def _maybe_restart(self, now: float, config: Config) -> None:
        if not self.state.stopped:
            return
        if self.state.game_seen:
            due = now - self.last_active >= config.exit_grace_seconds
        else:
            due = self.wall() - self.state.stopped_at >= config.no_game_timeout_seconds
        if not due or now < self.retry_at:
            return
        refs = [dict(ref) for ref in self.state.stopped]
        names = ", ".join(ref["name"] for ref in refs)
        if config.after_game == "leave":
            log(f"leaving stopped: {names}")
        elif config.after_game == "restart":
            log(f"restarting {names}")
            self._start(refs)
            self._notify("GPU Game Guard", f"Started {names} again.")
        else:
            try:
                answer = self.prompter.restart(refs)
            except Exception as exc:
                log(f"dialog failed: {exc!r}")
                answer = None
            if answer is None:
                self.retry_at = now + config.dialog_retry_seconds
                return
            if answer:
                log(f"restarting {names}")
                self._start(refs)
            else:
                log("leaving containers stopped")
        self.state.clear()
        self.last_active = self.clock()

    def run(self) -> None:
        while True:
            delay = 1.0
            try:
                delay = self.watcher.current().poll_seconds
                self.step()
            except Exception as exc:  # keep watching; a failed pass must not end the service
                log(f"error: {exc!r}")
            time.sleep(delay)
