"""The state machine: intercept launches that meet GPU workloads, act on the chosen answer, and offer to
start what was stopped once the game has closed."""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import __version__, status as statusmod
from .config import Config, save as save_config
from .model import Launch, Ref, Runtime, Target, Trigger
from .state import State
from .util import log

LaunchKey = Tuple[str, Tuple[Any, ...]]  # (trigger name, Launch.key)
CONTAINER_RUNTIMES = ("docker", "podman")  # runtimes that `auto_containers` switches off


@dataclass
class Plan:
    """What the guard would do for a launch of one game, without doing it."""

    action: str  # "ask", "stop", "ignore", "paused", "disabled" or "nothing-running"
    will_stop: List[Target] = field(default_factory=list)  # targets that are stopped when the launch is answered with stop
    ignored: List[Target] = field(default_factory=list)  # targets excluded by `ignore_containers`
    can_ask: bool = True
    note: str = ""


def plan(config: Config, game_id: str, targets: Sequence[Target], can_ask: bool, paused: bool = False) -> Plan:
    """The behaviour for a launch of `game_id` while `targets` run. Pure: nothing is stopped or written. With
    `action` "ask" and `can_ask` false, the launch is left alone and `will_stop` is empty."""
    considered = [t for t in targets if config.auto_containers or t.runtime not in CONTAINER_RUNTIMES]
    ignored = [t for t in considered if config.is_ignored(t.name)]
    candidates = [t for t in considered if t not in ignored]
    if not config.enabled:
        return Plan("disabled", [], ignored, can_ask, "the guard is switched off; launches are never interrupted")
    if paused:
        return Plan("paused", [], ignored, can_ask, "the guard is paused; launches are never interrupted")
    action = config.action_for(game_id)
    if action == "ignore":
        return Plan("ignore", [], ignored, can_ask, "this game is set to start without stopping anything")
    if not candidates:
        return Plan("nothing-running", [], ignored, can_ask, "nothing the guard manages is using the GPU")
    if action == "ask" and not can_ask:
        return Plan("ask", [], ignored, False, "no dialog is available, so the game starts without anything being stopped")
    note = "the guard asks first" if action == "ask" else "the guard stops these without asking"
    return Plan(action, candidates, ignored, can_ask, note)


class Guard:
    def __init__(self, watcher, triggers: Sequence[Trigger], runtimes: Sequence[Runtime], prompter, state: State,
                 gpu_totals: Callable[[], Optional[Tuple[int, int]]] = lambda: None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 status_dir: Optional[Path] = None, dialog_name: str = ""):
        self.watcher = watcher
        self.triggers = list(triggers)
        self.runtimes = list(runtimes)
        self.prompter = prompter
        self.state = state
        self.gpu_totals = gpu_totals
        self.clock, self.wall = clock, wall
        self.status_dir = status_dir  # None: the default state directory
        self.dialog_name = dialog_name or str(getattr(prompter, "name", ""))
        self.current_game: Optional[dict] = None  # the game running now, or being asked about
        self.asking = False
        self.watch_only = False  # paused or disabled: launches are adopted, never intercepted
        self.paused_until: Optional[float] = None
        self.config_error: Optional[str] = None
        self.last_config: Config = Config()
        self._channel_failed = False  # a status write failure is logged once
        self.seen: Set[LaunchKey] = set()  # launches already looked at; those running at startup are left alone
        self.ended: Set[LaunchKey] = set()  # launches the guard ended; their processes may linger for a moment
        self.bypass: Dict[str, float] = {}  # game id -> time until which a launch is let through
        self.baseline: Set[LaunchKey] = set()  # launches present when targets were last stopped
        self.unasked: Set[str] = set()  # game ids already logged as impossible to ask about
        self.pending_adoption: Set[str] = {t.name for t in self.triggers}  # triggers not yet scanned successfully
        self.last_active = self.clock()
        self.retry_at = 0.0
        self._scan()  # adopts the launches that are already running

    @property
    def _can_ask(self) -> bool:
        return getattr(self.prompter, "can_ask", True) is not False

    def _scan(self) -> List[Tuple[Launch, LaunchKey]]:
        found = []
        for trigger in self.triggers:
            try:
                launches = trigger.scan()
            except Exception as exc:
                log(f"trigger {trigger.name} failed: {exc!r}")
                # keep what was known about this trigger so one failed scan does not make its launches look new,
                # and adopt whatever it returns next, because a game started during the outage was not seen starting
                self.pending_adoption.add(trigger.name)
                found.extend((None, key) for key in self.seen if key[0] == trigger.name)
                continue
            pairs = [(launch, (trigger.name, launch.key)) for launch in launches]
            if trigger.name in self.pending_adoption:
                # the first successful scan of a trigger: whatever it returns was running before the guard looked
                self.pending_adoption.discard(trigger.name)
                self.seen.update(key for _launch, key in pairs)
            found.extend(pairs)
        return found

    def _targets(self, config: Config) -> List[Target]:
        targets: List[Target] = []
        for runtime in self.runtimes:
            if not config.auto_containers and runtime.name in CONTAINER_RUNTIMES:
                continue
            try:
                targets.extend(runtime.running_gpu_targets())
            except Exception as exc:
                log(f"runtime {runtime.name} failed: {exc!r}")
        return [t for t in targets if not config.is_ignored(t.name)]

    def step(self) -> None:
        try:
            self._step()
        finally:
            self._publish()

    def _refresh(self, config: Config) -> None:
        """Take the pause file and the config watcher's view of the file into account."""
        self.last_config = config
        paused, until = self._read_pause()
        self.watch_only = paused or not config.enabled
        self.paused_until = (float("inf") if until is None else until) if paused else None
        error = getattr(self.watcher, "error", None)
        error = error if isinstance(error, str) and error else None
        if error != self.config_error:
            self.config_error = error
            if error:
                self._event("config", f"the config file has a problem; the previous settings stay in force: {error}")
            else:
                self._event("config", "the config file is valid again")

    def _read_pause(self) -> Tuple[bool, Optional[float]]:
        try:
            return statusmod.paused(self.status_dir, now=self.wall())
        except Exception as exc:
            self._channel_error(exc)
            return False, None

    def _channel_error(self, exc: Exception) -> None:
        if not self._channel_failed:
            self._channel_failed = True
            log(f"cannot write the status files: {exc!r}")

    def _event(self, kind: str, message: str, **fields: Any) -> None:
        try:
            statusmod.append_event(kind, message, self.status_dir, **fields)
        except Exception as exc:
            self._channel_error(exc)

    def _state_name(self) -> str:
        if not self.last_config.enabled:
            return "disabled"
        if self.paused_until is not None:
            return "paused"
        if self.asking:
            return "asking"
        if self.current_game is not None:
            return "game-running"
        if self.state.stopped:
            return "restart-pending"
        return "watching"

    def _publish(self) -> None:
        """Write the status file; a failure is logged once and never stops the guard."""
        try:
            statusmod.write_status(statusmod.Status(
                version=__version__, pid=os.getpid(), updated=self.wall(), state=self._state_name(),
                paused_until=self.paused_until, paused=self.paused_until is not None, game=self.current_game,
                stopped=[dict(ref) for ref in self.state.stopped], config_ok=self.config_error is None,
                config_error=self.config_error, dialog=self.dialog_name, can_ask=self._can_ask,
                targets=len(self.last_config.targets)), self.status_dir)
        except Exception as exc:
            self._channel_error(exc)

    def _step(self) -> None:
        now = self.clock()
        config = self.watcher.current()
        self._refresh(config)
        scanned = self._scan()
        current = {key for _launch, key in scanned}
        self.seen &= current
        self.ended &= current
        active: List[LaunchKey] = []
        for launch, key in scanned:
            if key in self.ended:
                continue
            if launch is None:
                active.append(key)
                continue
            if key not in self.seen:
                self.seen.add(key)
                if not self.watch_only and self._should_intercept(launch, config, now):
                    targets = self._targets(config)
                    if targets and self._askable(launch, config):
                        self.ended.add(key)
                        self._intercept(launch, targets, config, current)
                        continue
            active.append(key)
        self.current_game = next(
            ({"id": launch.game_id, "title": launch.title} for launch, key in scanned
             if launch is not None and key in active), None)
        if active:
            self.last_active = now
            if self.state.stopped and not self.state.game_seen and any(key not in self.baseline for key in active):
                self.state.game_seen = True
                try:
                    self.state.save()
                except Exception as exc:
                    log(f"could not save the state: {exc!r}")
            return
        self._maybe_restart(now, config)

    def _askable(self, launch: Launch, config: Config) -> bool:
        """False, with one log line per game, when the launch calls for a question that cannot be shown."""
        if config.action_for(launch.game_id) != "ask" or self._can_ask:
            return True
        if launch.game_id not in self.unasked:
            self.unasked.add(launch.game_id)
            log(f"cannot ask about {launch.game_id} ({launch.title}): no dialog is available; letting it start")
        return False

    def _should_intercept(self, launch: Launch, config: Config, now: float) -> bool:
        if config.action_for(launch.game_id) == "ignore":
            return False
        return self.bypass.get(launch.game_id, 0) <= now

    def _trigger(self, name: str) -> Optional[Trigger]:
        return next((t for t in self.triggers if t.name == name), None)

    def _intercept(self, launch: Launch, targets: List[Target], config: Config, present: Set[LaunchKey]) -> None:
        names = ", ".join(t.name for t in targets)
        action = config.action_for(launch.game_id)
        shown = launch.title or launch.game_id
        log(f"launch of {launch.game_id} ({launch.title}) with GPU workloads running: {names}")
        self._event("intercept", f"{shown} started while {names} used the GPU", game=launch.game_id)
        trigger = self._trigger(launch.trigger)
        try:
            if trigger is not None:
                trigger.end(launch)
        except Exception as exc:
            log(f"could not end the launch of {launch.game_id}: {exc!r}")
            self._event("failure", f"could not end the launch of {shown}", game=launch.game_id)
        if action == "stop":
            choice = "stop"
        else:
            self.asking = True
            self.current_game = {"id": launch.game_id, "title": launch.title}
            self._publish()
            try:
                choice = self.prompter.launch(launch, targets, self.gpu_totals())
            except Exception as exc:
                log(f"dialog failed: {exc!r}")
                self._event("failure", "the dialog failed; the game starts", game=launch.game_id)
                choice = "anyway"
            finally:
                self.asking = False
            if choice not in ("stop", "cancel"):
                choice = "anyway"  # fail safe: an unknown or missing answer lets the game start
            self._remember(launch, choice, config)
        log(f"answer: {choice}")
        self._event("answer", f"{shown}: {choice}", game=launch.game_id, choice=choice)
        if choice == "cancel":
            return
        if choice == "stop":
            stopped = self._stop(targets, present)
            if not stopped:
                self._event("failure", f"could not stop {names} for {shown}", game=launch.game_id)
                self._notify("GPU Game Guard", f"Could not stop {names} for {shown}.")
            else:
                self._event("stop", f"stopped {', '.join(stopped)} for {shown}", game=launch.game_id)
                if action == "stop":
                    self._notify("GPU Game Guard", f"Stopped {', '.join(stopped)} for {shown}.")
        self.bypass[launch.game_id] = self.clock() + config.bypass_seconds
        try:
            if trigger is not None:
                trigger.relaunch(launch)
        except Exception as exc:
            log(f"could not relaunch {launch.game_id}: {exc!r}")
            self._event("failure", f"could not start {shown} again", game=launch.game_id)
            self._notify("GPU Game Guard", f"Could not start {shown} again.")
        self._publish()

    def _remember(self, launch: Launch, choice: str, config: Config) -> None:
        """Save the per-game action the dialog was asked to remember. Never raises."""
        if not config.remember_choices:
            return
        try:
            consume = getattr(self.prompter, "consume_remember", None)
            action = consume() if callable(consume) else None
            if action not in ("stop", "ignore"):
                return
            path = getattr(self.watcher, "path", None)
            if path is None:
                path = getattr(getattr(self.watcher, "watcher", None), "path", None)
            if path is None:
                return  # the watcher has no file to write back to
            save_config(config.with_game(launch.game_id, launch.title, action), path)
            log(f"remembered: {launch.game_id} -> {action}")
            self._event("remember", f"{launch.title or launch.game_id} will always {action}",
                        game=launch.game_id, action=action)
        except Exception as exc:
            log(f"could not remember the choice for {launch.game_id}: {exc!r}")

    def _stop(self, targets: List[Target], present: Set[LaunchKey]) -> List[str]:
        """Stop the targets and record those that are no longer running; returns their names."""
        refs = [t.ref for t in targets]
        self._each_runtime(refs, "stop")
        still = self._running_keys()
        stopped = [ref for ref in refs if (ref["kind"], ref["runtime"], ref["name"]) not in still]
        survivors = [ref["name"] for ref in refs if ref not in stopped]
        if survivors:
            log(f"still running after the stop: {', '.join(survivors)}")
        if not stopped:
            return []
        self.baseline |= present
        self.state.add(stopped, self.wall())
        try:
            self.state.save()
        except Exception as exc:  # the stop already happened; the restart offer works from memory
            log(f"could not save the state: {exc!r}")
        return [ref["name"] for ref in stopped]

    def _running_keys(self) -> Set[Tuple[str, str, str]]:
        keys = set()
        for runtime in self.runtimes:
            try:
                keys.update((t.kind, t.runtime, t.name) for t in runtime.running_gpu_targets())
            except Exception as exc:
                log(f"runtime {runtime.name} failed: {exc!r}")
        return keys

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
                self._event("failure", f"{verb} through {name} failed")

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
        after_game = config.after_game
        if after_game == "ask" and not self._can_ask:
            after_game = "restart"
        if after_game == "leave":
            log(f"leaving stopped: {names}")
            self._event("leave", f"left stopped: {names}")
        elif after_game == "restart":
            log(f"restarting {names}")
            self._start(refs)
            self._event("restart", f"started {names} again")
            self._notify("GPU Game Guard", f"Started {names} again.")
        else:
            self.asking = True
            self._publish()
            try:
                answer = self.prompter.restart(refs)
            except Exception as exc:
                log(f"dialog failed: {exc!r}")
                self._event("failure", "the restart dialog failed; it is asked again later")
                answer = None
            finally:
                self.asking = False
            if answer is None:
                self.retry_at = now + config.dialog_retry_seconds
                return
            if answer:
                log(f"restarting {names}")
                self._start(refs)
                self._event("restart", f"started {names} again")
            else:
                log("leaving containers stopped")
                self._event("leave", f"left stopped: {names}")
        try:
            self.state.clear()
        except Exception as exc:  # the state clears itself in memory before it touches the disk
            log(f"could not clear the state file: {exc!r}")
        self.baseline.clear()
        self.last_active = self.clock()

    def _poll_delay(self) -> float:
        """The configured poll interval limited to a range `time.sleep` accepts and a sensible one."""
        try:
            seconds = float(self.watcher.current().poll_seconds)
        except Exception:
            return 1.0
        return min(max(seconds, 0.2), 60.0) if math.isfinite(seconds) else 1.0

    def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception as exc:  # keep watching; a failed pass must not end the service
                log(f"error: {exc!r}")
            try:
                time.sleep(self._poll_delay())
            except Exception as exc:
                log(f"error: {exc!r}")
                time.sleep(1.0)
