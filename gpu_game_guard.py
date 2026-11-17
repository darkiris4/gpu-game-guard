#!/usr/bin/env python3
"""Ask before a Steam game starts while GPU containers are running.

A watcher polls /proc for Steam's per-game launcher (`reaper SteamLaunch AppId=<id> -- ...`).
When a game starts and containers that use the GPU are running, the launch is ended before the
game window exists and a dialog offers to stop those containers and start the game again. After
the game has exited, a second dialog offers to start the containers that were stopped.

State survives restarts of the watcher and reboots (a container stopped with `docker stop` stays
stopped), so the restart question is asked even if the machine was rebooted in between.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

POLL_SECONDS = 1.0
EXIT_GRACE_SECONDS = 8  # Steam's launcher processes briefly disappear between stages of a launch and an exit
NO_GAME_TIMEOUT_SECONDS = 180  # containers were stopped but the relaunched game never appeared
DIALOG_RETRY_SECONDS = 300  # wait before asking again when no dialog could be shown
BYPASS_SECONDS = 120  # a launch the user already answered for is let through for this long
KILL_WAIT_SECONDS = 2
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gpu-game-guard" / "state.json"
SHORTCUT_FLAG = 0x02000000  # low bits Steam sets on the 64-bit game id of a non-Steam shortcut


def log(message: str) -> None:
    print(message, flush=True)


@dataclass(frozen=True)
class Game:
    pid: int
    start_time: str  # /proc/<pid>/stat field 22; with the pid it identifies one process even if pids are reused
    app_id: int
    flatpak: bool

    @property
    def key(self) -> tuple[int, str]:
        return (self.pid, self.start_time)

    @property
    def game_id(self) -> int:
        """The id `steam://rungameid/` takes: the app id itself, or the 64-bit id of a shortcut."""
        if self.app_id >= 2**31:
            return (self.app_id << 32) | SHORTCUT_FLAG
        return self.app_id


def parse_launch(argv: list[str]) -> tuple[int, bool] | None:
    """Return (app_id, flatpak) for a Steam game launcher command line, else None."""
    if len(argv) < 3 or os.path.basename(argv[0]) != "reaper" or argv[1] != "SteamLaunch":
        return None
    if not argv[2].startswith("AppId="):
        return None
    try:
        app_id = int(argv[2].split("=", 1)[1])
    except ValueError:
        return None
    return app_id, any("com.valvesoftware.Steam" in part for part in argv)


def scan_games(proc: Path = Path("/proc")) -> list[Game]:
    games = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            launch = parse_launch(argv)
            if launch is None:
                continue
            start_time = (entry / "stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            continue
        games.append(Game(int(entry.name), start_time, launch[0], launch[1]))
    return games


def descendants(pid: int, proc: Path = Path("/proc")) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(parent, []).append(int(entry.name))
    found, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie waiting to be reaped."""
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def end_launch(game: Game) -> None:
    """Stop a launch that has only just begun: ask the launcher and everything under it to exit, then
    force whatever is left once the grace period is over."""
    pids = [game.pid, *descendants(game.pid)]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    while time.monotonic() < deadline and any(alive(p) for p in pids):
        time.sleep(0.1)
    for pid in pids:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def is_gpu_container(info: dict) -> bool:
    host = info.get("HostConfig") or {}
    if host.get("Runtime") == "nvidia":
        return True
    return any("gpu" in (request.get("Capabilities") or [[]])[0] for request in host.get("DeviceRequests") or [])


@dataclass(frozen=True)
class GpuContainer:
    name: str
    image: str
    container_id: str
    vram_mib: int | None = None  # GPU memory held by its processes; None when unknown


CONTAINER_CGROUP = re.compile(r"docker-([0-9a-f]{64})\.scope|/docker/([0-9a-f]{64})")


def container_id_from_cgroup(text: str) -> str | None:
    """The container id a /proc/<pid>/cgroup file places the process in, if it is in a container."""
    match = CONTAINER_CGROUP.search(text)
    return (match.group(1) or match.group(2)) if match else None


class Gpu:
    """GPU memory figures from nvidia-smi; every method returns an empty result when it is unavailable."""

    def _query(self, *args: str) -> list[list[str]]:
        try:
            out = subprocess.run(["nvidia-smi", *args, "--format=csv,noheader,nounits"], capture_output=True,
                                 text=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [[field.strip() for field in line.split(",")] for line in out.splitlines() if line.strip()]

    def vram_by_container(self, proc: Path = Path("/proc")) -> dict[str, int]:
        used: dict[str, int] = {}
        for pid, mib in self._query("--query-compute-apps=pid,used_memory"):
            try:
                container = container_id_from_cgroup((proc / pid / "cgroup").read_text())
                if container:
                    used[container] = used.get(container, 0) + int(mib)
            except (OSError, ValueError):
                continue
        return used

    def totals(self) -> tuple[int, int] | None:
        rows = self._query("--query-gpu=memory.used,memory.total")
        try:
            return int(rows[0][0]), int(rows[0][1])
        except (IndexError, ValueError):
            return None


class Docker:
    def __init__(self, gpu: Gpu | None = None):
        self.gpu = gpu or Gpu()

    def running_gpu_containers(self) -> list[GpuContainer]:
        ids = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, check=True).stdout.split()
        if not ids:
            return []
        out = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, check=True).stdout
        found = [info for info in json.loads(out) if is_gpu_container(info)]
        if not found:
            return []
        vram = self.gpu.vram_by_container()
        return sorted((GpuContainer(info["Name"].lstrip("/"), (info.get("Config") or {}).get("Image", ""), info["Id"],
                                    vram.get(info["Id"])) for info in found), key=lambda c: c.name)

    def stop(self, names: list[str]) -> None:
        procs = [subprocess.Popen(["docker", "stop", name], stdout=subprocess.DEVNULL) for name in names]
        for proc in procs:
            proc.wait()

    def start(self, names: list[str]) -> None:
        for name in names:
            subprocess.run(["docker", "start", name], stdout=subprocess.DEVNULL)


def format_mib(mib: int) -> str:
    return f"{mib / 1024:.1f} GB" if mib >= 1024 else f"{mib} MB"


HEAVY_VRAM_MIB = 4096  # container GPU memory at which the warning stops being tentative


def impact_sentence(containers: list[GpuContainer]) -> str:
    held = sum(c.vram_mib or 0 for c in containers)
    if held >= HEAVY_VRAM_MIB:
        return (f"Together they hold <b>{format_mib(held)}</b> of GPU memory and compete for GPU time, so the game could "
                "stutter, load slowly or run out of memory.")
    if held > 0:
        return (f"Together they hold {format_mib(held)} of GPU memory. The impact on the game is probably modest, but "
                "they can still take GPU time.")
    return ("They are idle right now, so the impact on the game should be small. They can take the GPU again as soon "
            "as one of them gets work.")


def launch_message(game_name: str, containers: list[GpuContainer], totals: tuple[int, int] | None) -> str:
    rows = []
    for c in containers:
        memory = f"{format_mib(c.vram_mib)} of GPU memory" if c.vram_mib else "no GPU memory in use right now"
        rows.append(f"<li><b>{escape(c.name)}</b><br><small>{escape(c.image)} &middot; {memory}</small></li>")
    summary = (f"<p><small>GPU memory in use by all programs: {format_mib(totals[0])} of {format_mib(totals[1])}.</small></p>"
               if totals else "")
    return (f"<p><b>GPU Game Guard</b> ended the launch of <b>{escape(game_name)}</b> because these containers have "
            f"access to the GPU:</p><ul>{''.join(rows)}</ul>{summary}"
            f"<p>{impact_sentence(containers)}</p><p>Stop them and start the game?</p>")


def restart_message(names: list[str]) -> str:
    items = "".join(f"<li>{escape(name)}</li>" for name in names)
    return ("<p><b>GPU Game Guard</b>: the game has closed. Restart the containers it stopped before the game "
            f"started?</p><ul>{items}</ul>")


class Dialogs:
    def __init__(self, gpu: Gpu | None = None):
        self.gpu = gpu or Gpu()

    def launch(self, game_name: str, containers: list[GpuContainer]) -> str:
        """Return "stop", "anyway" or "cancel". Without a dialog the game starts anyway."""
        text = launch_message(game_name, containers, self.gpu.totals())
        try:
            code = subprocess.run(["kdialog", "--title", "GPU Game Guard", "--yes-label", "Stop containers and launch",
                                   "--no-label", "Launch anyway", "--cancel-label", "Cancel", "--yesnocancel", text]).returncode
        except OSError as exc:
            log(f"cannot show a dialog ({exc}); launching anyway")
            return "anyway"
        return {0: "stop", 1: "anyway"}.get(code, "cancel")

    def restart(self, names: list[str]) -> bool | None:
        """True to restart, False to leave stopped, None when no dialog could be shown (ask again later)."""
        try:
            return subprocess.run(["kdialog", "--title", "GPU Game Guard", "--yes-label", "Restart",
                                   "--no-label", "Leave stopped", "--yesno", restart_message(names)]).returncode == 0
        except OSError as exc:
            log(f"cannot show a dialog ({exc}); keeping the containers marked as stopped")
            return None


STEAM_ROOTS = [Path.home() / ".local/share/Steam", Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]


def game_name(app_id: int, roots: list[Path] | None = None) -> str:
    """The title Steam shows for an app id: an installed game's manifest name or a shortcut's name."""
    roots = STEAM_ROOTS if roots is None else roots
    libraries = list(roots)
    for root in roots:
        try:
            libraries += [Path(p) for p in re.findall(r'"path"\s+"([^"]+)"', (root / "steamapps/libraryfolders.vdf").read_text())]
        except OSError:
            pass
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


class Steam:
    def launch(self, game: Game) -> None:
        url = f"steam://rungameid/{game.game_id}"
        command = ["flatpak", "run", "com.valvesoftware.Steam", url] if game.flatpak else ["steam", url]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class State:
    """Containers the guard stopped, kept on disk until the user answers the restart question."""

    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.stopped: list[str] = []
        self.stopped_at = 0.0
        self.game_seen = False
        try:
            data = json.loads(path.read_text())
            self.stopped = list(data["stopped"])
            self.stopped_at = float(data["stopped_at"])
            self.game_seen = bool(data["game_seen"])
        except (OSError, ValueError, KeyError):
            pass

    def save(self) -> None:
        if not self.stopped:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False) as tmp:
            json.dump({"stopped": self.stopped, "stopped_at": self.stopped_at, "game_seen": self.game_seen}, tmp)
        os.replace(tmp.name, self.path)

    def clear(self) -> None:
        self.stopped, self.stopped_at, self.game_seen = [], 0.0, False
        self.save()


class Guard:
    def __init__(self, docker=None, dialogs=None, steam=None, state=None, scan=scan_games, end=end_launch,
                 clock=time.monotonic, wall=time.time):
        self.docker = docker or Docker()
        self.dialogs = dialogs or Dialogs()
        self.steam = steam or Steam()
        self.state = state or State()
        self.scan, self.end, self.clock, self.wall = scan, end, clock, wall
        self.seen: set[tuple[int, str]] = {g.key for g in self.scan()}  # launchers already looked at; those running at startup are left alone
        self.ended: set[tuple[int, str]] = set()  # launchers the guard ended; they may linger for a moment
        self.bypass: dict[int, float] = {}  # app id -> time until which a launch is let through
        self.last_active = self.clock()
        self.retry_at = 0.0

    def step(self) -> None:
        now = self.clock()
        games = self.scan()
        current = {g.key for g in games}
        self.seen &= current
        self.ended &= current
        active = []
        for game in games:
            if game.key in self.ended:
                continue
            if game.key not in self.seen:
                self.seen.add(game.key)
                if self.bypass.get(game.app_id, 0) <= now:
                    running = self.docker.running_gpu_containers()
                    if running:
                        self.ended.add(game.key)
                        self.intercept(game, running)
                        continue
            active.append(game)
        if active:
            self.last_active = now
            if self.state.stopped and not self.state.game_seen:
                self.state.game_seen = True
                self.state.save()
            return
        self.maybe_ask_restart(now)

    def intercept(self, game: Game, containers: list[GpuContainer]) -> None:
        names = [c.name for c in containers]
        log(f"launch of app {game.app_id} with GPU containers running: {', '.join(names)}")
        self.end(game)
        choice = self.dialogs.launch(game_name(game.app_id), containers)
        log(f"answer: {choice}")
        if choice == "cancel":
            return
        if choice == "stop":
            self.docker.stop(names)
            self.state.stopped = sorted(set(self.state.stopped) | set(names))
            self.state.stopped_at = self.wall()
            self.state.game_seen = False
            self.state.save()
        self.bypass[game.app_id] = self.clock() + BYPASS_SECONDS
        self.steam.launch(game)

    def maybe_ask_restart(self, now: float) -> None:
        if not self.state.stopped:
            return
        if self.state.game_seen:
            due = now - self.last_active >= EXIT_GRACE_SECONDS
        else:
            due = self.wall() - self.state.stopped_at >= NO_GAME_TIMEOUT_SECONDS
        if not due or now < self.retry_at:
            return
        names = list(self.state.stopped)
        answer = self.dialogs.restart(names)
        if answer is None:
            self.retry_at = now + DIALOG_RETRY_SECONDS
            return
        if answer:
            log(f"restarting {', '.join(names)}")
            self.docker.start(names)
        else:
            log("leaving containers stopped")
        self.state.clear()
        self.last_active = self.clock()

    def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception as exc:  # keep watching; a failed check must not end the service
                log(f"error: {exc!r}")
            time.sleep(POLL_SECONDS)


def main() -> int:
    guard = Guard()
    log("watching for Steam game launches")
    guard.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
