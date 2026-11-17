# Architecture

GPU Game Guard watches for a game launch. When GPU workloads are running it ends the launch (usually
before the game window appears; the poll interval is one second), asks what to do, stops the workloads
on request, starts the game again, and later offers to start the workloads again. When no way to ask
exists it leaves the launch untouched. This document is the contract between the modules. The shared types are in `gpu_game_guard/model.py`.

## Rules for every module

- Python 3.9 to 3.13, standard library only. Every module starts with `from __future__ import annotations`.
  No `X | Y` outside annotations, no `match`, no `tomllib`, no third-party imports.
- Comments describe the code: purpose, behaviour, assumptions, constraints. No first or second person,
  no names of people, no mention of requests or sessions.
- External commands go through `subprocess.run` with a list, never a shell. A missing command is an
  `OSError` and must degrade gracefully, never crash the watcher.
- Anything that can fail logs one line through `util.log` and carries on. The watcher must not stop.
- Tests use `unittest`, use fakes instead of real Docker, Steam or dialogs, and run with
  `python3 -m unittest discover -s tests -t .` from the repository root. Most modules have a
  `tests/test_<module>.py`; `tests/test_integration.py` drives the guard end to end,
  `tests/test_steam_trigger.py` covers the Steam trigger, and `tests/fakes.py` holds the shared fakes.
  `tests/test_installer.sh` covers the installer.
- Logs go to stderr, which the systemd journal captures. Standard output carries only command results.
- External command failures are logged once per command and subcommand (`gpu.run_command`), so a
  missing or failing tool does not fill the log on every poll.

## Modules

```
gpu_game_guard/
  __init__.py         version
  __main__.py         python -m gpu_game_guard  ->  cli.main()
  model.py            shared types and protocols
  util.py             log, xdg_config_home, xdg_state_home, atomic_write_json
  config.py           Config, GameRule, ConfigError, load, save, ConfigWatcher, config_path
  state.py            State: what was stopped and not yet restarted
  guard.py            Guard: the state machine
  gpu.py              GPU memory figures for any vendor
  runtimes.py         Docker and Podman as Runtime implementations
  dialogs.py          zenity, kdialog, notification and no-dialog Prompter implementations
  triggers/__init__.py   build(names) -> list[Trigger]
  triggers/steam.py      the Steam trigger
  cli.py              command line and LiveConfig
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

    def action_for(self, game_id: str) -> str:    # per-game rule's action if a rule exists (default "ask"), else on_launch
    def is_ignored(self, name: str) -> bool:      # matches ignore_containers
    def to_json(self) -> dict                      # the exact on-disk shape, every key present
    @classmethod
    def from_json(cls, data: dict) -> "Config"     # unknown keys ignored with a warning on stderr; raises ConfigError on bad values

class ConfigError(ValueError):                     # ConfigError(reason, path): reason is the problem, path the file
    reason: str; path: Optional[Path]
def config_path() -> Path                          # $XDG_CONFIG_HOME/gpu-game-guard/config.json
def validate(data: dict) -> List[str]              # human-readable problems, unknown keys included (with a
                                                    # did-you-mean hint); empty list means valid
def load(path: Path = None) -> Config              # missing file -> defaults; invalid -> ConfigError
def save(config: Config, path: Path = None) -> None   # atomic, pretty-printed, sorted keys
class ConfigWatcher:
    def __init__(self, path: Path = None): ...
    def current(self) -> Config                    # re-reads when the file's stamp (mtime, size, inode) changes; on a
                                                    # read error, or when the file briefly disappears, keeps the
                                                    # previous settings and logs once per distinct error
```

Validation ranges (inclusive):

| Key | Range |
| --- | --- |
| `poll_seconds` | 0.2 to 60 |
| `exit_grace_seconds` | 2 to 3600 |
| `no_game_timeout_seconds` | 30 to 86400 |
| `bypass_seconds` | 5 to 3600 |
| `dialog_retry_seconds` | 30 to 86400 |
| `heavy_vram_mib` | 0 to 10000000 |

A wrong type, a value outside its range or outside the allowed set makes the config invalid.
`config validate` also reports unknown keys (for example `on_lauch`) with a did-you-mean hint and exits
1, while `load` accepts them and logs a warning to stderr. A game rule without `action` uses `"ask"`;
it does not inherit `on_launch`. `save` and `util.atomic_write_json` write through a temporary file and
keep the existing file's permissions (a new file gets 0644 as masked by the umask; a symlinked file is written through).

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
    def save(self): ...                                # atomic; removes the file when nothing is stopped
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

`watcher` is anything with `.current() -> Config`. `heavy_vram_mib` is not applied by the guard:
`cli.LiveConfig` wraps the watcher and applies it to the dialog module on every `current()` call.
Behaviour:

- Launches already running when the guard is constructed are adopted and never intercepted.
- A new launch is intercepted when GPU targets exist (all runtimes' `running_gpu_targets()` combined, minus
  `config.is_ignored(name)`), the effective action is not `ignore`, and the launch is not in the bypass window.
  Ignored names are never stopped.
- Effective action: `config.action_for(launch.game_id)`.
  - `ignore`: do nothing at all (no launch ending, no dialog).
  - `ask` with `prompter.can_ask`: `trigger.end(launch)`, then `prompter.launch(...)` with `gpu_totals()`;
    `stop` stops the targets, records them in `State`, relaunches; `anyway` relaunches with a bypass;
    `cancel` does nothing more.
  - `ask` without `prompter.can_ask`: the launch is not ended at all. The game starts untouched, nothing is
    stopped, and the log says so once per game.
  - `stop`: `trigger.end(launch)`, stop the targets, record them, relaunch; `prompter.notify` says what
    was stopped. Works whether or not the prompter can ask.
- Relaunch sets a bypass for that `game_id` for `bypass_seconds` so the relaunch is not intercepted again.
- After the game: when there are recorded targets and the game has been absent for `exit_grace_seconds`
  (or `no_game_timeout_seconds` after the stop if the game never reappeared), act on `after_game`:
  - `ask` with `prompter.can_ask`: `prompter.restart(refs)`; `True` starts them, `False` leaves them,
    `None` retries after `dialog_retry_seconds` with the state kept.
  - `ask` without `prompter.can_ask`: the recorded containers are started again and a notification says so.
  - `restart`: start them and notify.
  - `leave`: forget them without asking.
- The state file persists across restarts. Targets are started through the runtime named in each ref.
  Refs already recorded as stopped are started again even if an ignore pattern matching them was added
  since.
- A killed launch's lingering processes must not trigger a second prompt.
- `can_ask` is False for `NullPrompter` and for the notification backend without action support; every
  other backend has it True. The guard never relies on a prompter's answer to decide whether to end a
  launch.

### gpu.py

```python
def totals() -> Optional[Tuple[int, int]]            # (used_mib, total_mib) for the whole system; None if unknown
def vram_by_container() -> Optional[Dict[str, int]]   # full container id -> MiB held by its processes; None = unmeasurable
def container_id_from_cgroup(text: str) -> Optional[str]
```

Backends, tried in order, results from the first that yields data:

- NVIDIA: `nvidia-smi` (`--query-gpu=memory.used,memory.total`, `--query-compute-apps=pid,used_memory`),
  summing across GPUs for `totals()`. Per-container memory is counted through compute processes only;
  graphics-only workloads are not counted.
- AMD and Intel: kernel interfaces only. `totals()` from `/sys/class/drm/card*/device/mem_info_vram_used` and
  `mem_info_vram_total` (bytes). Per process, `/proc/<pid>/fdinfo/*` lines `drm-memory-vram:`,
  `drm-resident-vram0:`, `drm-total-vram` and similar (values with a KiB, MiB or GiB unit), counting each
  `drm-client-id` once.
- cgroup parsing accepts Docker (`docker-<64 hex>.scope`, `/docker/<64 hex>`) and Podman
  (`libpod-<64 hex>.scope`, `libpod-<64 hex>/`) paths.
- Every backend returns empty results when its tool or files are missing. Nothing raises.

VRAM semantics: a container's figure is measured (a number; 0 means measured and none is held) or unknown
(`None`; it could not be measured). Unknown is never treated as zero.

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
- `HostConfig.Runtime` is `nvidia`, unless the container's `NVIDIA_VISIBLE_DEVICES` environment value is
  `void` or `none`.
- `HostConfig.Devices` maps a host path that is `/dev/nvidia*`, `/dev/kfd`, `/dev/dri` itself or anything
  under it, or a CDI name containing `gpu` (Podman: `nvidia.com/gpu=all`, `amd.com/gpu=all`).
- `Config.Annotations` or `HostConfig.Annotations` has a key starting `cdi.k8s.io/` whose value contains `gpu`.

Containers that map `/dev/dri` only for video decoding (Plex, Jellyfin, Frigate) are detected; the remedy is
`ignore_containers`. Privileged containers are not detected.

`running_gpu_targets()` runs `<command> ps -q` then `<command> inspect <ids>` once, builds `Target`s
(sorted by name, `ref_id` the full id, `vram_mib` from the vram function keyed by full id: a number when
measured, `None` when unknown). `installed()` reports whether the runtime's command is on `PATH`; `available()`
whether it also answers. `stop` runs `<command> stop <name>` for each ref of this runtime in parallel and waits. `start` runs
`<command> start <name>` for each. Refs for other runtimes are ignored. The Podman command line is the same
as Docker's for these calls.

### dialogs.py

```python
def choose(preference: str = "auto", environ=os.environ, which=shutil.which) -> Prompter
class ZenityPrompter, KdialogPrompter, NotifyPrompter, NullPrompter   # each has name, can_ask, available()
def launch_message(launch, targets, totals, markup) -> str     # markup: "html" | "pango" | "plain"
def restart_message(names, markup) -> str
def impact_sentence(targets, markup) -> str
def format_mib(mib) -> str
```

- `auto` order: KDE (`XDG_CURRENT_DESKTOP` contains KDE or `KDE_FULL_SESSION` set) tries kdialog then zenity;
  anything else tries zenity then kdialog; then the notification backend; then `NullPrompter`.
- Buttons. Launch dialog: **Stop containers and launch** (`stop`), **Launch anyway** (`anyway`), **Cancel**
  (`cancel`). Restart dialog: **Restart** (True), **Ask me later** (None), **Leave stopped** (False).
  A backend that fails to start or crashes is never a deliberate answer: `launch` is `anyway` and `restart`
  is None. zenity cannot tell a closed window from a failure (both exit 1 with empty output), so closing
  its launch dialog is `anyway` and closing its restart dialog is None. kdialog can (a crash such as a
  missing display aborts with status 134, never 0, 1 or 2), so its Esc and window-close status 2 is
  `cancel` at launch and None (ask later) after the game.
- `kdialog`: rich text (HTML subset), three buttons through `--yesnocancel` with custom labels. Launch:
  yes `stop`, no `anyway`, cancel `cancel`, any other status `anyway`. Restart: yes True (**Restart**), no False
  (**Leave stopped**), cancel None (**Ask me later**), any other status None.
- `zenity`: Pango markup (`<b>`, `<small>`, no `<ul>`, `<li>` or `<br>`; use newlines and bullets), `--question`
  with `--ok-label`, `--cancel-label` and `--extra-button`; the extra button's label is printed on stdout with
  exit status 1, OK is exit 0, a dismissed window is exit 1 with empty output.
- `notify`: `can_ask` only when `notify-send --help` lists `--action`; otherwise it can only inform and
  `can_ask` is False. Never raises.
- `NullPrompter`: `can_ask` is False; `notify` logs.
- Window title `GPU Game Guard`. Message text names the guard, the game, each container with image and GPU
  memory, the overall GPU use when known, and the graded impact sentence (modest, or heavy at
  `heavy_vram_mib`, passed to `impact_sentence`/`launch_message` through a module-level default
  `HEAVY_VRAM_MIB = 4096` that `cli.LiveConfig` overwrites from config). A container whose memory is
  unknown is shown as "GPU memory use unknown" and nothing is ever called idle. Units are GiB and MiB.
- Every user-supplied string (container names, images, game titles) is escaped for the chosen markup.
- A backend whose command fails to start (`OSError`) fails safe: `launch` -> "anyway", `restart` -> None;
  it is never a deliberate answer.

### triggers

```python
# triggers/__init__.py
def build(names: Sequence[str]) -> List[Trigger]     # known: "steam"; unknown names logged and skipped
# triggers/steam.py
class SteamTrigger:  name = "steam"; scan(); end(launch); relaunch(launch)
```

`SteamTrigger` detects `reaper SteamLaunch AppId=<id>` processes owned by the current user (the real uid from
`/proc/<pid>/status`; other users' launchers are ignored),
native and Flatpak; `Launch.key` is `(pid, start_time)`; `game_id` is `steam:<app id>`; `title` comes from
Steam's manifests or non-Steam shortcuts (`game_name`); `end` snapshots the launcher and its descendants with their start times, terminates the tree, and
force-ends stragglers after two seconds, re-checking each process's start time before every signal so a reused
pid is never signalled; `relaunch` runs `steam steam://rungameid/<id>` or the Flatpak equivalent,
using the 64-bit game id for shortcuts. The payload carries the trigger's `Game` dataclass.

### cli.py

```
gpu-game-guard [--config PATH] [run]            run the watcher (default); --config may also follow the subcommand
gpu-game-guard check                            diagnose and print: version, config path and whether it is valid,
                                                dialog backend chosen, runtimes found, triggers, the launches visible
                                                now, the GPU containers detected now (name, image, GPU memory or unknown), GPU totals
gpu-game-guard config path | show | validate | init     (init writes the defaults to the config path, refusing to
                                                overwrite; show prints the effective config as JSON, and only JSON, on stdout; validate also reports
                                                unknown keys)
gpu-game-guard --version
```

Exit status: 0 on success; 1 on a failed check or an invalid config for `check` and `config` commands;
2 on bad usage; 78 when `run` refuses to start on an invalid config (the systemd unit does not restart on
78); 130 on Ctrl-C. `main(argv=None) -> int`.

`cli.LiveConfig` wraps the `ConfigWatcher` for the running guard: on each `current()` it applies
`heavy_vram_mib` to the dialog module and returns the config.

## Packaging

`scripts/build-zipapp.sh` builds a single executable `gpu-game-guard` (a `zipapp` with
`#!/usr/bin/env python3`) from the package. `install.sh` downloads the tarball of a git ref, builds the
zipapp and installs it to `~/.local/bin/gpu-game-guard` along with the systemd user unit; `GPU_GAME_GUARD_SRC`
installs from a local checkout instead. The service runs `gpu-game-guard run`.
