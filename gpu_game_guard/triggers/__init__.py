"""Game launch sources. Each one implements the `Trigger` protocol from `gpu_game_guard.model`."""
from __future__ import annotations

from typing import Callable, Dict, List, Sequence

from ..model import Trigger
from ..util import log


def _steam() -> Trigger:
    from .steam import SteamTrigger

    return SteamTrigger()


KNOWN: Dict[str, Callable[[], Trigger]] = {"steam": _steam}


def build(names: Sequence[str]) -> List[Trigger]:
    """Triggers for the given names, in order; an unknown name is logged and skipped."""
    triggers: List[Trigger] = []
    for name in names:
        factory = KNOWN.get(name)
        if factory is None:
            log(f"unknown trigger {name!r}; known: {', '.join(sorted(KNOWN))}")
            continue
        triggers.append(factory())
    return triggers
