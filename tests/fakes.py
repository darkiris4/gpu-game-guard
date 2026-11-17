"""Fakes for the Trigger, Runtime and Prompter protocols and for the config watcher, shared by the unit and
integration tests."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from gpu_game_guard.config import Config
from gpu_game_guard.model import Launch, Ref, Target


def make_launch(pid: int, app_id: int = 730, trigger: str = "steam", title: str = "") -> Launch:
    return Launch(trigger, (pid, str(pid * 10)), f"{trigger}:{app_id}", title or f"Game {app_id}")


class FakeTrigger:
    def __init__(self, name: str = "steam"):
        self.name = name
        self.launches: List[Launch] = []
        self.ended: List[Launch] = []
        self.relaunched: List[Launch] = []
        self.fail_scan = False

    def scan(self) -> List[Launch]:
        if self.fail_scan:
            raise RuntimeError("scan failed")
        return list(self.launches)

    def end(self, launch: Launch) -> None:
        self.ended.append(launch)

    def relaunch(self, launch: Launch) -> None:
        self.relaunched.append(launch)


class FakeRuntime:
    def __init__(self, name: str = "docker", running: Sequence[str] = ()):
        self.name = name
        self.running: List[str] = list(running)
        self.stopped: List[str] = []
        self.started: List[str] = []
        self.fail_scan = False
        self.is_available = True

    def installed(self) -> bool:
        return True

    def available(self) -> bool:
        return self.is_available

    def running_gpu_targets(self) -> List[Target]:
        if self.fail_scan:
            raise RuntimeError("listing failed")
        return [Target("container", self.name, n, "image:1", None, n) for n in self.running]

    def stop(self, refs: Sequence[Ref]) -> None:
        for ref in refs:
            if ref["runtime"] == self.name:
                self.stopped.append(ref["name"])
                self.running = [n for n in self.running if n != ref["name"]]

    def start(self, refs: Sequence[Ref]) -> None:
        for ref in refs:
            if ref["runtime"] == self.name:
                self.started.append(ref["name"])


class FakePrompter:
    def __init__(self, launch: Optional[str] = "stop", restart: Optional[bool] = True):
        self.name = "fake"
        self.launch_answer, self.restart_answer = launch, restart
        self.launch_asked: List[List[str]] = []
        self.restart_asked: List[List[str]] = []
        self.totals_seen: List[Optional[Tuple[int, int]]] = []
        self.notifications: List[Tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def launch(self, launch, targets, totals):
        self.launch_asked.append([t.name for t in targets])
        self.totals_seen.append(totals)
        return self.launch_answer

    def restart(self, refs):
        self.restart_asked.append([r["name"] for r in refs])
        return self.restart_answer

    def notify(self, title: str, body: str) -> None:
        self.notifications.append((title, body))


class FakeWatcher:
    """Holds a Config that a test may replace or mutate between steps."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config if config is not None else Config()

    def current(self) -> Config:
        return self.config
