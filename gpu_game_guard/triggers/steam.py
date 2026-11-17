"""Steam as a game launch source.

Steam starts every game through a per-game launcher process, `reaper SteamLaunch AppId=<id> -- ...`.
Scanning /proc for that command line finds launches for native Steam and for the Flatpak build, and for
non-Steam shortcuts, which carry a large app id.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..model import Launch
from ..util import log

KILL_WAIT_SECONDS = 2  # time the launcher tree gets to exit after SIGTERM before it is force-ended
SHORTCUT_FLAG = 0x02000000  # low bits Steam sets on the 64-bit game id of a non-Steam shortcut
FLATPAK_APP = "com.valvesoftware.Steam"


@dataclass(frozen=True)
class Game:
    pid: int
    start_time: str  # /proc/<pid>/stat field 22; with the pid it identifies one process even if pids are reused
    app_id: int
    flatpak: bool

    @property
    def key(self) -> Tuple[int, str]:
        return (self.pid, self.start_time)

    @property
    def game_id(self) -> int:
        """The id `steam://rungameid/` takes: the app id itself, or the 64-bit id of a shortcut."""
        if self.app_id >= 2 ** 31:
            return (self.app_id << 32) | SHORTCUT_FLAG
        return self.app_id


def default_roots() -> List[Path]:
    return [Path.home() / ".local/share/Steam", Path.home() / ".var/app" / FLATPAK_APP / ".local/share/Steam"]


def parse_launch(argv: Sequence[str]) -> Optional[Tuple[int, bool]]:
    """Return (app_id, flatpak) for a Steam game launcher command line, else None."""
    if len(argv) < 3 or os.path.basename(argv[0]) != "reaper" or argv[1] != "SteamLaunch":
        return None
    if not argv[2].startswith("AppId="):
        return None
    try:
        app_id = int(argv[2].split("=", 1)[1])
    except ValueError:
        return None
    return app_id, any(FLATPAK_APP in part for part in argv)


def _stat_fields(entry: Path) -> List[str]:
    """Fields of /proc/<pid>/stat after the command name, starting at the state (field 3)."""
    return (entry / "stat").read_text().rsplit(")", 1)[1].split()


def scan_games(proc: Path = Path("/proc")) -> List[Game]:
    games = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            launch = parse_launch(argv)
            if launch is None:
                continue
            start_time = _stat_fields(entry)[19]
        except (OSError, IndexError):
            continue
        games.append(Game(int(entry.name), start_time, launch[0], launch[1]))
    return games


def descendants(pid: int, proc: Path = Path("/proc")) -> List[int]:
    children: Dict[int, List[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent = int(_stat_fields(entry)[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(parent, []).append(int(entry.name))
    found: List[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


def alive(pid: int, proc: Path = Path("/proc")) -> bool:
    """True while the process exists and is not a zombie waiting to be reaped."""
    try:
        return _stat_fields(proc / str(pid))[0] != "Z"
    except (OSError, IndexError):
        return False


def end_launch(game: Game, proc: Path = Path("/proc"), kill: Callable[[int, int], None] = os.kill,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               wait_seconds: float = KILL_WAIT_SECONDS) -> None:
    """Stop a launch that has only just begun: ask the launcher and everything under it to exit, then
    force whatever is left once the grace period is over."""
    pids = [game.pid] + descendants(game.pid, proc)
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = clock() + wait_seconds
    while clock() < deadline and any(alive(p, proc) for p in pids):
        sleep(0.1)
    for pid in pids:
        if alive(pid, proc):
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass


def _read_library_paths(root: Path) -> List[Path]:
    try:
        text = (root / "steamapps/libraryfolders.vdf").read_text()
    except OSError:
        return []
    return [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', text)]


def game_name(app_id: int, roots: Optional[Sequence[Path]] = None) -> str:
    """The title Steam shows for an app id: an installed game's manifest name or a shortcut's name."""
    roots = default_roots() if roots is None else list(roots)
    libraries = list(roots)
    for root in roots:
        libraries += _read_library_paths(root)
    for library in libraries:
        try:
            match = re.search(r'"name"\s+"([^"]+)"', (library / f"steamapps/appmanifest_{app_id}.acf").read_text())
        except OSError:
            continue
        if match:
            return match.group(1)
    for root in roots:
        for shortcuts in root.glob("userdata/*/config/shortcuts.vdf"):
            try:
                data = shortcuts.read_bytes()
            except OSError:
                continue
            for entry in re.finditer(rb"\x02appid\x00(.{4})", data, re.S):
                if int.from_bytes(entry.group(1), "little") == app_id:
                    name = re.search(rb"AppName\x00([^\x00]*)", data[entry.end():entry.end() + 600])
                    if name:
                        return name.group(1).decode(errors="replace")
    return f"Game {app_id}"


class SteamTrigger:
    name = "steam"

    def __init__(self, proc: Path = Path("/proc"), roots: Optional[Sequence[Path]] = None,
                 kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep,
                 popen: Callable[..., object] = subprocess.Popen):
        self.proc = Path(proc)
        self.roots = default_roots() if roots is None else [Path(r) for r in roots]
        self.kill = kill
        self.sleep = sleep
        self.popen = popen
        self._titles: Dict[int, str] = {}

    def _title(self, app_id: int) -> str:
        # Only a found name is cached, so a manifest written after the launch is picked up on the next scan.
        title = self._titles.get(app_id)
        if title is None:
            title = game_name(app_id, self.roots)
            if title != f"Game {app_id}":
                self._titles[app_id] = title
        return title

    def scan(self) -> List[Launch]:
        try:
            games = scan_games(self.proc)
        except OSError as exc:
            log(f"steam: cannot scan {self.proc}: {exc}")
            return []
        return [Launch(self.name, game.key, f"steam:{game.app_id}", self._title(game.app_id), game) for game in games]

    def end(self, launch: Launch) -> None:
        end_launch(launch.payload, self.proc, kill=self.kill, sleep=self.sleep)

    def relaunch(self, launch: Launch) -> None:
        game = launch.payload
        url = f"steam://rungameid/{game.game_id}"
        command = ["flatpak", "run", FLATPAK_APP, url] if game.flatpak else ["steam", url]
        try:
            self.popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            log(f"steam: cannot relaunch {launch.title}: {exc}")
