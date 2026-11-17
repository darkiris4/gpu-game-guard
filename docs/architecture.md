# Architecture

GPU Game Guard watches for a game launch. When GPU workloads are running it ends the launch, asks what
to do, stops the workloads on request, starts the game again, and later offers to start the workloads
again. This document is the contract between the modules. The shared types are in `gpu_game_guard/model.py`.

## Rules for every module

- Python 3.9 to 3.13, standard library only. Every module starts with `from __future__ import annotations`.
  No `X | Y` outside annotations, no `match`, no `tomllib`, no third-party imports.
- Comments describe the code: purpose, behaviour, assumptions, constraints. No first or second person,
  no names of people, no mention of requests or sessions.
- External commands go through `subprocess.run` with a list, never a shell. A missing command is an
  `OSError` and must degrade gracefully, never crash the watcher.
- Anything that can fail logs one line through `util.log` and carries on. The watcher must not stop.
- Tests use `unittest`, live in `tests/test_<module>.py`, use fakes instead of real Docker, Steam or
  dialogs, and run with `python3 -m unittest discover -s tests -t .` from the repository root.

## Modules

```
gpu_game_guard/
  __init__.py         version
  __main__.py         python -m gpu_game_guard  ->  cli.main()
  model.py            shared types and protocols (written)
  util.py             log, xdg_config_home, xdg_state_home, atomic_write_json (written)
  config.py           Config, GameRule, ConfigError, load, save, ConfigWatcher, config_path
  state.py            State: what was stopped and not yet restarted
  guard.py            Guard: the state machine
  gpu.py              GPU memory figures for any vendor
  runtimes.py         Docker and Podman as Runtime implementations
  dialogs.py          zenity, kdialog, notification and no-dialog Prompter implementations
  triggers/__init__.py   build(names) -> list[Trigger]
  triggers/steam.py      the Steam trigger
  cli.py              command line
```

### config.py

```python
GAME_ACTIONS = ("ask", "stop", "ignore")
ON_LAUNCH = ("ask", "stop", "ignore")          # what to do when a launch meets GPU workloads
AFTER_GAME = ("ask", "restart", "leave")       # what to do once the game has closed
DIALOGS = ("auto", "zenity", "kdialog", "notify", "none")

@dataclass
class GameRule:
    name: str = ""            # display name, informational
    action: str = "ask"       # one of GAME_ACTIONS

@dataclass
class Config:
    on_launch: str = "ask"
    after_game: str = "ask"
    poll_seconds: float = 1.0
    exit_grace_seconds: float = 8.0
    no_game_timeout_seconds: float = 180.0
    bypass_seconds: float = 120.0
    dialog_retry_seconds: float = 300.0
    heavy_vram_mib: int = 4096
    ignore_containers: List[str] = []        # fnmatch globs on the container name; never touched
    runtimes: List[str] = ["docker", "podman"]   # order of preference; unavailable ones are skipped
    triggers: List[str] = ["steam"]
    dialog: str = "auto"                     # one of DIALOGS
    games: Dict[str, GameRule] = {}          # game_id ("steam:730") -> rule

    def action_for(self, game_id: str) -> str:    # per-game rule if present, else on_launch
    def is_ignored(self, name: str) -> bool:      # matches ignore_containers
    def to_json(self) -> dict                      # the exact on-disk shape, every key present
    @classmethod
    def from_json(cls, data: dict) -> "Config"     # unknown keys ignored with a logged warning; raises ConfigError on bad values

class ConfigError(ValueError): ...
def config_path() -> Path                          # $XDG_CONFIG_HOME/gpu-game-guard/config.json
def validate(data: dict) -> List[str]              # human-readable problems; empty list means valid
def load(path: Path = None) -> Config              # missing file -> defaults; invalid -> ConfigError
def save(config: Config, path: Path = None) -> None   # atomic, pretty-printed, sorted keys
class ConfigWatcher:
    def __init__(self, path: Path = None): ...
    def current(self) -> Config                    # re-reads when the file's mtime changes; on a read error keeps the
                                                    # previous config and logs once per distinct error
```

On-disk shape (JSON, all keys optional on read):

```json
{
  "on_launch": "ask", "after_game": "ask",
  "poll_seconds": 1.0, "exit_grace_seconds": 8.0, "no_game_timeout_seconds": 180.0,
  "bypass_seconds": 120.0, "dialog_retry_seconds": 300.0, "heavy_vram_mib": 4096,
  "ignore_containers": ["backup-*"], "runtimes": ["docker", "podman"], "triggers": ["steam"],
  "dialog": "auto",
  "games": {"steam:730": {"name": "Counter-Strike 2", "action": "ignore"}}
}
```

### state.py

```python
class State:
    def __init__(self, path: Path = None): ...        # default $XDG_STATE_HOME/gpu-game-guard/state.json
    stopped: List[Ref]; stopped_at: float; game_seen: bool
    def add(self, refs, wall_time): ...                # union by (kind, runtime, name); sets stopped_at; game_seen=False
    def save(self): ...                                # removes the file when nothing is stopped
    def clear(self): ...
```

The loader accepts the previous format (`"stopped": ["name", ...]`) and turns each name into
`{"kind": "container", "runtime": "docker", "name": name}`.

### guard.py

```python
class Guard:
    def __init__(self, watcher, triggers, runtimes, prompter, state, gpu_totals=lambda: None,
                 clock=time.monotonic, wall=time.time): ...
    def step(self) -> None        # one pass; never raises for expected failures
    def run(self) -> None         # step() every config.poll_seconds, catching and logging every exception
```

`watcher` is anything with `.current() -> Config`. Behaviour, unchanged from the first version unless noted:

- Launches already running when the guard is constructed are adopted and never intercepted.
- A new launch is intercepted when GPU targets exist (all runtimes' `running_gpu_targets()` combined, minus
  `config.is_ignored(name)`), the effective action is not `ignore`, and the launch is not in the bypass window.
- Effective action: `config.action_for(launch.game_id)`.
  - `ignore`: do nothing at all (no launch ending, no dialog).
  - `ask`: `trigger.end(launch)`, then `prompter.launch(...)` with `gpu_totals()`; `stop` stops the targets, records
    them in `State`, relaunches; `anyway` relaunches with a bypass; `cancel` does nothing more.
  - `stop`: like `ask` answered `stop`, without a dialog; `prompter.notify` says what was stopped.
- Relaunch sets a bypass for that `game_id` for `bypass_seconds` so the relaunch is not intercepted again.
- After the game: when there are recorded targets and the game has been absent for `exit_grace_seconds`
  (or `no_game_timeout_seconds` after the stop if the game never reappeared), act on `after_game`:
  - `ask`: `prompter.restart(refs)`; `True` starts them, `False` leaves them, `None` retries after
    `dialog_retry_seconds` with the state kept.
  - `restart`: start them and notify.
  - `leave`: forget them without asking.
- The state file persists across restarts. Targets are started through the runtime named in each ref.
- A killed launch's lingering processes must not trigger a second prompt.
- When the prompter has no dialog (`NullPrompter`), `launch` returns "anyway".

### gpu.py

```python
def totals() -> Optional[Tuple[int, int]]            # (used_mib, total_mib) for the whole system; None if unknown
def vram_by_container() -> Dict[str, int]            # full container id -> MiB held by its processes
def container_id_from_cgroup(text: str) -> Optional[str]
```

Backends, tried in order, results from the first that yields data:

- NVIDIA: `nvidia-smi` (`--query-gpu=memory.used,memory.total`, `--query-compute-apps=pid,used_memory`),
  summing across GPUs for `totals()`.
- AMD and Intel: kernel interfaces only. `totals()` from `/sys/class/drm/card*/device/mem_info_vram_used` and
  `mem_info_vram_total` (bytes). Per process, `/proc/<pid>/fdinfo/*` lines `drm-memory-vram:`,
  `drm-resident-vram0:` and similar (values with a KiB or MiB unit), counting each `drm-client-id` once.
- cgroup parsing accepts Docker (`docker-<64 hex>.scope`, `/docker/<64 hex>`) and Podman
  (`libpod-<64 hex>.scope`, `libpod-<64 hex>/`) paths.
- Every backend returns empty results when its tool or files are missing. Nothing raises.

### runtimes.py

```python
def is_gpu_container(info: dict) -> bool    # a single `inspect` record
def build(names: Sequence[str]) -> List[Runtime]   # known names: "docker", "podman"; unknown names are logged and skipped
class DockerLikeRuntime:                           # one class parametrised by the command name
    def __init__(self, command: str, vram=gpu.vram_by_container): ...
```

A container counts as a GPU container when any of these holds:

- `HostConfig.DeviceRequests` has an entry whose `Capabilities` (a list of lists) contains `gpu`, or whose
  `Driver` is `cdi` and a `DeviceIDs` value contains `gpu`.
- `HostConfig.Runtime` is `nvidia`.
- `HostConfig.Devices` maps a host path that is `/dev/nvidia*`, `/dev/kfd`, or under `/dev/dri/`, or a CDI
  name containing `gpu` (Podman: `nvidia.com/gpu=all`, `amd.com/gpu=all`).
- `Config.Annotations` or `HostConfig.Annotations` has a key starting `cdi.k8s.io/` whose value contains `gpu`.

`running_gpu_targets()` runs `<command> ps -q` then `<command> inspect <ids>` once, builds `Target`s
(sorted by name, `ref_id` the full id, `vram_mib` from the vram function keyed by full id, `None` when not
found). `stop` runs `<command> stop <name>` for each ref of this runtime in parallel and waits. `start` runs
`<command> start <name>` for each. Refs for other runtimes are ignored. The Podman command line is the same
as Docker's for these calls.

### dialogs.py

```python
def choose(preference: str = "auto", environ=os.environ, which=shutil.which) -> Prompter
class ZenityPrompter, KdialogPrompter, NotifyPrompter, NullPrompter
def launch_message(launch, targets, totals, markup) -> str     # markup: "html" | "pango" | "plain"
def restart_message(names, markup) -> str
def impact_sentence(targets, markup) -> str
def format_mib(mib) -> str
```

- `auto` order: KDE (`XDG_CURRENT_DESKTOP` contains KDE or `KDE_FULL_SESSION` set) tries kdialog then zenity;
  anything else tries zenity then kdialog; then the notification backend; then `NullPrompter`.
- `kdialog`: rich text (HTML subset), three buttons through `--yesnocancel` with custom labels.
- `zenity`: Pango markup (`<b>`, `<small>`, no `<ul>`, `<li>` or `<br>`; use newlines and bullets), `--question`
  with `--ok-label`, `--cancel-label` and `--extra-button`; the extra button's label is printed on stdout with
  exit status 1, OK is exit 0, a dismissed window is exit 1 with empty output.
- `notify`: only when `notify-send --help` lists `--action`; otherwise it can only inform, so `launch` returns
  "anyway" and `restart` returns None. Never raises.
- `NullPrompter`: `launch` -> "anyway"; `restart` -> None; `notify` -> logs.
- Window title `GPU Game Guard`. Message text names the guard, the game, each container with image and GPU
  memory, the overall GPU use when known, and the graded impact sentence (idle, modest, or heavy at
  `heavy_vram_mib` which is passed to `impact_sentence`/`launch_message` through a module-level default
  `HEAVY_VRAM_MIB = 4096` that the guard may overwrite from config).
- Every user-supplied string (container names, images, game titles) is escaped for the chosen markup.
- A backend whose command fails to start (`OSError`) fails safe: `launch` -> "anyway", `restart` -> None.

### triggers

```python
# triggers/__init__.py
def build(names: Sequence[str]) -> List[Trigger]     # known: "steam"; unknown names logged and skipped
# triggers/steam.py
class SteamTrigger:  name = "steam"; scan(); end(launch); relaunch(launch)
```

`SteamTrigger` is the first version's Steam code: it detects `reaper SteamLaunch AppId=<id>` processes,
native and Flatpak; `Launch.key` is `(pid, start_time)`; `game_id` is `steam:<app id>`; `title` comes from
Steam's manifests or non-Steam shortcuts (`game_name`); `end` terminates the whole tree, then force-ends
stragglers after two seconds; `relaunch` runs `steam steam://rungameid/<id>` or the Flatpak equivalent,
using the 64-bit game id for shortcuts. The payload carries the first version's `Game` dataclass.

### cli.py

```
gpu-game-guard [--config PATH] [run]            run the watcher (default)
gpu-game-guard check                            diagnose and print: version, config path and whether it is valid,
                                                dialog backend chosen, runtimes found, triggers, the launches visible
                                                now, the GPU containers detected now (name, image, VRAM), GPU totals
gpu-game-guard config path | show | validate | init     (init writes the defaults to the config path, refusing to
                                                overwrite; show prints the effective config as JSON)
gpu-game-guard --version
```

Exit status 0 on success, 1 on a failed check or invalid config, 2 on bad usage. `main(argv=None) -> int`.

## Packaging

`scripts/build-zipapp.sh` builds a single executable `gpu-game-guard` (a `zipapp` with
`#!/usr/bin/env python3`) from the package. `install.sh` downloads the tarball of a git ref, builds the
zipapp and installs it to `~/.local/bin/gpu-game-guard` along with the systemd user unit; `GPU_GAME_GUARD_SRC`
installs from a local checkout instead. The service runs `gpu-game-guard run`.
