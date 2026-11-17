"""Types shared between the modules. Every module depends on this one and on nothing else of its siblings'
internals, which is what lets them be built and tested separately."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

# A reference to something the guard stopped, as stored in the state file and handed back to start it again.
Ref = Dict[str, str]  # {"kind": "container", "runtime": "docker", "name": "ollama"}


@dataclass(frozen=True)
class Target:
    """Something that can be stopped before a game and started again after it."""

    kind: str  # "container" for now
    runtime: str  # "docker" or "podman" for containers
    name: str  # what the runtime calls it
    image: str = ""
    vram_mib: Optional[int] = None  # GPU memory its processes hold; None when unknown
    ref_id: str = ""  # the runtime's full id, used to match processes to it; never stored

    @property
    def ref(self) -> Ref:
        return {"kind": self.kind, "runtime": self.runtime, "name": self.name}


@dataclass(frozen=True)
class Launch:
    """One game launch seen by a trigger."""

    trigger: str  # name of the trigger that saw it, for example "steam"
    key: Tuple[Any, ...]  # identity of the launch, hashable and stable while its process lives
    game_id: str  # "<trigger>:<id>", for example "steam:730"; the key of per-game rules
    title: str  # what dialogs call the game
    payload: Any = None  # trigger-private data


class Trigger(Protocol):
    name: str

    def scan(self) -> List[Launch]:
        """Launches whose processes exist right now."""

    def end(self, launch: Launch) -> None:
        """End a launch that has only just begun, including everything it started."""

    def relaunch(self, launch: Launch) -> None:
        """Start the same game again."""


class Runtime(Protocol):
    name: str

    def available(self) -> bool:
        """True when this runtime's command exists and answers."""

    def running_gpu_targets(self) -> List[Target]:
        """Running containers that have access to a GPU, with `vram_mib` filled in where it can be measured."""

    def stop(self, refs: Sequence[Ref]) -> None:
        """Stop the referenced targets (in parallel). Failures are logged, not raised."""

    def start(self, refs: Sequence[Ref]) -> None:
        """Start the referenced targets. Failures are logged, not raised."""


class Prompter(Protocol):
    name: str

    def available(self) -> bool:
        """True when this dialog backend can be used on this system."""

    def launch(self, launch: Launch, targets: List[Target], totals: Optional[Tuple[int, int]]) -> str:
        """Ask what to do about a launch. Returns "stop", "anyway" or "cancel". `totals` is (used, total) MiB."""

    def restart(self, refs: List[Ref]) -> Optional[bool]:
        """Ask whether to start what was stopped. True to start, False to leave stopped, None when no dialog could be shown."""

    def notify(self, title: str, body: str) -> None:
        """Show a passive notification; never raises."""
