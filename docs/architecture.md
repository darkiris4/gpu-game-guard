# Architecture

GPU Game Guard watches for a game launch. When GPU workloads are running it ends the launch (usually
before the game window appears; the poll interval is one second), asks what to do, stops the workloads
on request, starts the game again, and later offers to start the workloads again. When no way to ask
exists it leaves the launch untouched.

A workload is a *target*: a Docker or Podman container (found automatically), or a systemd service, a
pair of commands or a process that the user has configured. Each kind has a provider (a `Runtime`) that
lists what is running, stops it and starts it again. The shared types are in `gpu_game_guard/model.py`.

## Rules for every module

- Python 3.9 to 3.13, standard library only for everything outside `gpu_game_guard/ui/`. Every module
  starts with `from __future__ import annotations`. No `X | Y` outside annotations, no `match`, no
  `tomllib`, no third-party imports.
- Code under `gpu_game_guard/ui/` imports PyQt5 only through `gpu_game_guard/ui/qt.py`. The rest of the
  program never imports it, so the watcher, the command line and the fallback dialogs run without PyQt5.
- Comments describe the code: purpose, behaviour, assumptions, constraints. No first or second person,
  no names of people, no mention of requests or sessions.
- External commands go through `subprocess` with a list, never a shell. A missing command is an `OSError`
  and must degrade gracefully, never crash the watcher.
- Anything that can fail logs one line through `util.log` and carries on. The watcher must not stop.
- Logs go to stderr, which the systemd journal captures. Standard output carries only command results.
- External command failures are logged once per command and subcommand (`gpu.run_command`,
  `gpu.run_process`, `gpu.log_once`), so a missing or failing tool does not fill the log on every poll.
- Tests use `unittest`, are hermetic (no real Docker, systemctl, Steam, display or home directory: temp
  directories and fakes instead) and run with `python3 -m unittest discover -s tests -t .` from the
  repository root. Most modules have a `tests/test_<module>.py`; `tests/test_integration.py` drives the
  guard end to end, `tests/test_steam_trigger.py` covers the Steam trigger, `tests/fakes.py` holds the
  shared fakes, `tests/test_installer.sh` covers the installer and `tests/test_deb.sh` the package.
- The Qt tests set `QT_QPA_PLATFORM=offscreen` and skip cleanly when PyQt5 is missing
  (`unittest.skipUnless(qt.available(), ...)`). Nothing in a test or a script may show a window on the
  real screen.

## Modules

```
gpu_game_guard/
  __init__.py            version
  __main__.py            python -m gpu_game_guard  ->  cli.main()
  model.py               shared types and protocols
  util.py                log, xdg_config_home, xdg_state_home, atomic_write_json
  config.py              Config, GameRule, TargetSpec, ConfigError, load, save, validate, ConfigWatcher
  state.py               State: what was stopped and not yet restarted
  status.py              the status channel: status.json, events.jsonl, pause.json
  guard.py               Guard: the state machine; plan(): its behaviour as a pure function
  gpu.py                 GPU memory figures for any vendor, cgroup helpers, helpers for host targets
  runtimes.py            Docker and Podman as Runtime implementations; build()
  services.py            ServiceRuntime: configured systemd services
  commands.py            CommandRuntime: configured stop and start commands
  processes.py           ProcessRuntime: configured processes
  discovery.py           what could be managed; the built-in protect list
  presets/               ready-made targets as JSON; load_presets(), applicable()
  polkit.py              the polkit rule that lets a user manage chosen units without a password
  dialogs.py             zenity, kdialog, notification and no-dialog Prompter implementations; choose()
  triggers/__init__.py   build(names) -> list[Trigger]
  triggers/steam.py      the Steam trigger
  cli.py                 command line and LiveConfig
  ui/                    the Qt layer (PyQt5, optional)
    qt.py                the only doorway to PyQt5
    theme.py             palette, stylesheet and shared widgets
    prompt.py            QtPrompter and the launch and restart dialogs
    tray.py              the tray icon
    settings.py          the settings window
    settings_model.py    SettingsModel: the state behind the settings window
    basic.py             the Basic page
    advanced.py          the Advanced tabs container
    advanced_*.py        one module per tab, plus shared building blocks (advanced_common.py)
```

## Data on disk

| Path | Written by | Holds |
| --- | --- | --- |
| `$XDG_CONFIG_HOME/gpu-game-guard/config.json` | the user, the settings window, remembered choices, `targets` and `presets add` | the configuration |
| `$XDG_CONFIG_HOME/gpu-game-guard/ui.json` | the settings window | the chosen mode (`basic` or `advanced`) and the window size |
| `$XDG_STATE_HOME/gpu-game-guard/state.json` | the guard | what was stopped and not yet started again |
| `$XDG_STATE_HOME/gpu-game-guard/status.json` | the guard, once per pass | what the guard is doing now |
| `$XDG_STATE_HOME/gpu-game-guard/events.jsonl` | the guard | the last 300 events |
| `$XDG_STATE_HOME/gpu-game-guard/pause.json` | `pause`, the tray, the settings window | a pause and when it ends |
| `/etc/polkit-1/rules.d/60-gpu-game-guard.rules` | `polkit allow` and `polkit revoke`, as root | the units a user may manage |

`XDG_CONFIG_HOME` defaults to `~/.config` and `XDG_STATE_HOME` to `~/.local/state`. The JSON files are
written atomically (a temporary file in the same directory, flushed, then renamed) and keep the existing
file's permissions; a new file gets 0644 as masked by the umask, and a symlinked file is written through.

## model.py

```python
Ref = Dict[str, Any]   # {"kind": "container", "runtime": "docker", "name": "ollama"} plus optional keys

@dataclass(frozen=True)
class Target:          # something that can be stopped before a game and started again after it
    kind: str          # "container", "service", "command" or "process"
    runtime: str       # the provider: "docker", "podman", "service", "command" or "process"
    name: str          # what the provider calls it (for configured targets, the label)
    image: str = ""
    vram_mib: Optional[int] = None   # a number when measured (0 = none held), None when it could not be measured
    ref_id: str = ""   # the runtime's full id, used to match processes to it; never stored
    extra: Optional[dict] = None     # provider data copied into the ref
    ref -> Ref         # kind, runtime and name, plus extra

@dataclass(frozen=True)
class Launch:          # one game launch seen by a trigger
    trigger: str; key: tuple; game_id: str; title: str; payload: Any = None
```

The protocols are `Trigger` (`scan`, `end`, `relaunch`), `Runtime` (`installed`, `available`,
`running_gpu_targets`, `stop`, `start`) and `Prompter` (`available`, `launch`, `restart`, `notify`, the
`name` and `can_ask` attributes, and the optional `consume_remember`).

A ref carries the required keys `kind`, `runtime` and `name`, and the optional keys `scope`, `unit`, `id`
(strings), `cmdline` (a list of strings) and `cwd` (a string) that a provider needs to start the target
again. Each provider acts only on refs whose `runtime` is its own.

## config.py

```python
GAME_ACTIONS = ("ask", "stop", "ignore")
ON_LAUNCH = ("ask", "stop", "ignore")          # what to do when a launch meets GPU workloads
AFTER_GAME = ("ask", "restart", "leave")       # what to do once the game has closed
DIALOGS = ("auto", "qt", "zenity", "kdialog", "notify", "none")
TARGET_KINDS = ("service", "command", "process")
DEFAULT_RUNTIMES = ("docker", "podman", "service", "command", "process")

@dataclass
class GameRule:
    name: str = ""            # display name, informational
    action: str = "ask"       # one of GAME_ACTIONS

@dataclass
class TargetSpec:             # one entry of `targets`; only the fields of its kind are used and written back
    id: str; kind: str; label: str = ""; enabled: bool = True
    unit: str = ""; scope: str = "system"                      # service
    stop: Optional[List[str]] = None; start: Optional[List[str]] = None
    check: Optional[List[str]] = None; timeout_seconds: float = 60.0   # command
    match: str = ""; restart: bool = True                      # process

@dataclass
class Config:
    enabled: bool = True              # master switch: false means watch only
    auto_containers: bool = True      # false: the container runtimes are not queried
    remember_choices: bool = True     # whether the dialog offers "remember my choice for this game"
    protect: List[str] = []           # fnmatch globs on a process or unit name, added to the built-in list
    targets: List[TargetSpec] = []
    on_launch: str = "ask"
    after_game: str = "ask"
    poll_seconds: float = 1.0
    exit_grace_seconds: float = 8.0
    no_game_timeout_seconds: float = 180.0
    bypass_seconds: float = 120.0
    dialog_retry_seconds: float = 300.0
    heavy_vram_mib: int = 4096
    ignore_containers: List[str] = []         # fnmatch globs on the container name; never touched
    runtimes: List[str] = list(DEFAULT_RUNTIMES)   # providers in order of preference; unavailable ones are skipped
    triggers: List[str] = ["steam"]
    dialog: str = "auto"                      # one of DIALOGS
    games: Dict[str, GameRule] = {}           # game_id ("steam:730") -> rule

    def action_for(self, game_id) -> str      # the game's rule action if a rule exists, else on_launch
    def is_ignored(self, name) -> bool        # matches ignore_containers (case-sensitive)
    def is_protected(self, name) -> bool      # matches a configured protect glob (case-insensitive)
    def target(self, id) -> Optional[TargetSpec]
    def with_target(self, spec) -> Config     # copy with the target of that id replaced, or appended
    def without_target(self, id) -> Config
    def with_game(self, game_id, name, action) -> Config   # copy with the rule set; an empty name keeps the old one
    def to_json(self) -> dict                 # the exact on-disk shape, every key present
    @classmethod from_json(cls, data) -> Config   # unknown keys are ignored with a warning on stderr; ConfigError on bad values

class ConfigError(ValueError):                # ConfigError(reason, path): reason is the problem, path the file
def config_path() -> Path                     # $XDG_CONFIG_HOME/gpu-game-guard/config.json
def validate(data: dict) -> List[str]         # human-readable problems, unknown keys included (with a
                                              # did-you-mean hint); an empty list means valid
def load(path=None) -> Config                 # missing file -> defaults; invalid -> ConfigError
def save(config, path=None) -> None           # atomic, pretty-printed, sorted keys
class ConfigWatcher:
    path: Path
    error: str                                # the problem with the file now on disk; empty when valid or absent
    def current(self) -> Config               # re-reads when the file's stamp (mtime, size, inode) changes
```

`ConfigWatcher.current()` keeps the previous settings, and logs once per distinct error, when the file
cannot be read or fails validation, and when the file briefly disappears after having existed.

Validation ranges (inclusive):

| Key | Range |
| --- | --- |
| `poll_seconds` | 0.2 to 60 |
| `exit_grace_seconds` | 2 to 3600 |
| `no_game_timeout_seconds` | 30 to 86400 |
| `bypass_seconds` | 5 to 3600 |
| `dialog_retry_seconds` | 30 to 86400 |
| `heavy_vram_mib` | 0 to 10000000 |
| a command target's `timeout_seconds` | 1 to 600 |

A wrong type, a value outside its range or outside the allowed set makes the config invalid. `validate`
(and so `config validate`) also reports unknown keys, in the top level, in game rules and in targets,
with a did-you-mean hint; `load` accepts them and logs a warning. A game rule without `action` uses
`"ask"`; it does not inherit `on_launch`.

Targets are validated per kind and reported by index and id:

- Common keys: `id` (required, unique, matching `[a-z0-9][a-z0-9_-]{0,39}`), `kind` (required, one of
  `service`, `command`, `process`), `label` (string, the id when empty), `enabled` (boolean, default true).
- `service`: `unit` (required, ends in `.service`, no whitespace and no `/`), `scope` (`"system"` default
  or `"user"`).
- `command`: `stop` and `start` (required, non-empty lists of non-empty strings), `check` (optional
  non-empty list of strings), `timeout_seconds` (1 to 600, default 60).
- `process`: `match` (required, non-empty string, no `/`), `restart` (boolean, default true).

## state.py

```python
class State:
    def __init__(self, path=None): ...   # default $XDG_STATE_HOME/gpu-game-guard/state.json
    stopped: List[Ref]; stopped_at: float; game_seen: bool
    def add(self, refs, wall_time): ...  # union by (kind, runtime, name); sets stopped_at; game_seen=False
    def save(self): ...                  # atomic; removes the file when nothing is stopped
    def clear(self): ...
```

The loader is strict: an entry without string `kind`, `runtime` and `name` is dropped, and optional ref
keys of the wrong type are dropped. It accepts the first format (`"stopped": ["name", ...]`) and turns each
name into `{"kind": "container", "runtime": "docker", "name": name}`.

## status.py

The status channel lets other processes (the command line, the tray, the settings window) see what the
guard is doing, read its recent events and pause it. All files are under `$XDG_STATE_HOME/gpu-game-guard/`.

```python
@dataclass
class Status:                      # what status.json holds
    version: str; pid: int; updated: float
    state: str                     # "watching", "paused", "disabled", "asking", "game-running" or "restart-pending"
    paused: bool
    paused_until: Optional[float]  # wall time; None when not paused; float("inf") when paused with no end
    game: Optional[dict]           # {"id", "title"} of the game now running or being asked about
    stopped: List[Ref]             # stopped by the guard and not yet started again
    config_ok: bool; config_error: Optional[str]
    dialog: str; can_ask: bool     # the dialog backend and whether it can ask
    targets: int                   # configured targets

def write_status(status, directory=None)                 # atomic; a pause with no end is stored as null
def read_status(directory=None, max_age=15.0)            # None when missing, unreadable or older than max_age
def append_event(kind, message, directory=None, **fields)   # one JSON line {"t", "kind", "message", ...}; the last 300 kept
def read_events(limit=100, directory=None)               # newest last; lines that are not JSON objects are skipped
def pause(seconds=None, directory=None)                  # None = until resumed; writes pause.json {"until": wall | null}
def resume(directory=None)                               # removes pause.json
def paused(directory=None, now=None)                     # (is paused, until); an ended or unreadable pause is "not paused"
```

A status older than 15 seconds means the service is not running. The event kinds the guard writes are
`intercept`, `answer`, `stop`, `restart`, `leave`, `remember`, `failure` and `config`.

## guard.py

```python
class Guard:
    def __init__(self, watcher, triggers, runtimes, prompter, state, gpu_totals=lambda: None,
                 clock=time.monotonic, wall=time.time, status_dir=None, dialog_name=""): ...
    def step(self) -> None        # one pass; never raises for expected failures; publishes the status
    def run(self) -> None         # step() every config.poll_seconds, catching and logging every exception

@dataclass
class Plan:
    action: str                   # "ask", "stop", "ignore", "paused", "disabled" or "nothing-running"
    will_stop: List[Target]; ignored: List[Target]; can_ask: bool; note: str
def plan(config, game_id, targets, can_ask, paused=False) -> Plan   # pure: nothing is stopped or written
```

`watcher` is anything with `.current() -> Config`; it may also have `.path` (the file to write remembered
choices to) and `.error` (the problem with the file). `heavy_vram_mib` and `remember_choices` are not
applied by the guard: `cli.LiveConfig` wraps the watcher and copies them to the `dialogs` module on every
`current()` call.

Behaviour:

- Launches already running when the guard is constructed are adopted and never intercepted. So is a game
  that starts while a trigger's scan is failing, once the scan works again.
- A new launch is intercepted when GPU targets exist (every runtime's `running_gpu_targets()` combined,
  skipping the Docker and Podman runtimes when `auto_containers` is false, minus
  `config.is_ignored(name)`), the effective action is not `ignore`, the guard is neither paused nor
  disabled, and the launch is not in the bypass window.
- A paused or disabled guard is "watch only": launches are adopted and left alone. Pending restart
  questions are still asked.
- Effective action: `config.action_for(launch.game_id)`.
  - `ignore`: do nothing at all (no launch ending, no dialog).
  - `ask` with `prompter.can_ask`: `trigger.end(launch)`, then `prompter.launch(...)` with `gpu_totals()`;
    `stop` stops the targets, records them in `State`, relaunches; `anyway` relaunches with a bypass;
    `cancel` does nothing more. An unknown answer or a prompter that raises counts as `anyway`.
  - `ask` without `prompter.can_ask`: the launch is not ended at all. The game starts untouched, nothing
    is stopped, and the log says so once per game.
  - `stop`: `trigger.end(launch)`, stop the targets, record them, relaunch; `prompter.notify` says what was
    stopped. Works whether or not the prompter can ask.
- Remembered choices: after `prompter.launch(...)`, and only when `config.remember_choices` is true, the
  guard calls `prompter.consume_remember()` if the prompter has it. For `stop` or `ignore` it saves a game
  rule through `save(config.with_game(...), watcher.path)`. A failure is logged, never raised. An action
  that is `stop` without a question does not remember anything.
- Relaunch sets a bypass for that `game_id` for `bypass_seconds` so the relaunch is not intercepted again.
- A target counts as stopped only if it is no longer listed by `running_gpu_targets()` after the stop.
  Those that stopped are recorded in `State`; the others are logged, and when none stopped the user gets a
  notification that they could not be stopped. Only recorded targets are offered for restart.
- After the game: when there are recorded targets and the game has been absent for `exit_grace_seconds` (or
  `no_game_timeout_seconds` after the stop if the game never reappeared), act on `after_game`:
  - `ask` with `prompter.can_ask`: `prompter.restart(refs)`; `True` starts them, `False` leaves them,
    `None` retries after `dialog_retry_seconds` with the state kept.
  - `ask` without `prompter.can_ask`: the recorded targets are started again and a notification says so.
  - `restart`: start them and notify.
  - `leave`: forget them without asking.
- The state file persists across restarts. Targets are started through the runtime named in each ref.
  Refs already recorded as stopped are started again even if an ignore pattern matching them was added
  since.
- A killed launch's lingering processes must not trigger a second prompt.
- `can_ask` is False for `NullPrompter` and for the notification backend without action support; every
  other backend has it True. The guard never relies on a prompter's answer to decide whether to end a
  launch.
- Every pass ends by writing `status.json`. The guard appends an event for every interception, answer,
  stop, restart, leave, remembered choice, failure and config problem. A failure to write the status
  files is logged once and never stops the guard.
- `plan` mirrors these rules without side effects, in this order: `disabled`, `paused`, the game's
  action `ignore`, `nothing-running` (no target left after `ignore_containers`), `ask` with `can_ask` false
  (`will_stop` empty), otherwise `ask` or `stop` with the targets to stop. The settings window's Test run
  tab uses it.

## gpu.py

```python
def totals() -> Optional[Tuple[int, int]]             # (used_mib, total_mib) for the whole system; None if unknown
def vram_by_container() -> Optional[Dict[str, int]]    # full container id -> MiB held by its processes; None = unmeasurable
def vram_by_pid() -> Optional[Dict[int, int]]          # pid -> MiB; None = unmeasurable
def memory_for_pids(by_pid, pids) -> Optional[int]     # sum for the pids; 0 when measured and none held; None when unknown
def container_id_from_cgroup(text) -> Optional[str]
def service_from_cgroup(text) -> Optional[Tuple[str, str]]   # (scope, unit) of the service a process is in
def cgroup_in_unit(text, unit) -> bool
def run_process(args, timeout) -> Optional[CommandResult]    # status and output whatever the status is
def run_command(args, timeout, keep_output=False) -> Optional[str]   # stdout, or None after logging the failure once
def usable_specs(get_config, kind), find_spec(get_config, kind, ref), own_refs(refs, kind, provider), display_name(spec)
```

Backends, tried in order, results from the first that yields data:

- NVIDIA: `nvidia-smi` (`--query-gpu=memory.used,memory.total`, `--query-compute-apps=pid,used_memory`),
  summing across GPUs for `totals()`. Per-process memory is counted through compute processes only;
  graphics-only workloads are not counted. A successful query is authoritative: a process it does not
  list holds none.
- AMD and Intel: kernel interfaces only. `totals()` from `/sys/class/drm/card*/device/mem_info_vram_used` and
  `mem_info_vram_total` (bytes). Per process, `/proc/<pid>/fdinfo/*` lines `drm-memory-vram:`,
  `drm-resident-vram0:`, `drm-total-vram` and similar (values with a KiB, MiB or GiB unit), counting each
  `drm-client-id` once. This source cannot read other users' processes, so it is not authoritative: a
  workload it lists nothing for is reported as unknown, never as zero.
- cgroup parsing accepts Docker (`docker-<64 hex>.scope`, `/docker/<64 hex>`) and Podman
  (`libpod-<64 hex>.scope`, `libpod-<64 hex>/`) paths. `service_from_cgroup` reports the innermost
  `.service` component, with scope `user` when it lies inside `user@<uid>.service`, and nothing for a
  process in a `.scope` (a login session or a container).
- Every backend returns empty results when its tool or files are missing. Nothing raises.

VRAM semantics: a target's figure is measured (a number; 0 means measured and none is held) or unknown
(`None`; it could not be measured). Unknown is never treated as zero.

`usable_specs` returns the enabled, valid targets of one kind in the live config and logs each problem
once; an invalid spec never hides the others. `find_spec` looks a ref up by its `id`.

## runtimes.py

```python
def is_gpu_container(info: dict) -> bool           # a single `inspect` record
def build(names, get_config=None) -> List[Runtime] # "docker", "podman", "service", "command", "process"
class DockerLikeRuntime:                           # one class parametrised by the command name
    def __init__(self, command, vram=gpu.vram_by_container): ...
```

`build` returns the runtimes in the order of `names`. The host providers need the live config, so
without `get_config` they are skipped with one log line. An unknown name is logged and skipped.

A container counts as a GPU container when any of these holds:

- `HostConfig.DeviceRequests` has an entry whose `Capabilities` (a list of lists) contains `gpu`, or whose
  `Driver` is `cdi` and a `DeviceIDs` value contains `gpu`.
- `HostConfig.Runtime` is `nvidia`, unless the container's `NVIDIA_VISIBLE_DEVICES` environment value is
  `void` or `none`.
- `HostConfig.Devices` maps a host path that is `/dev/nvidia*`, `/dev/kfd`, `/dev/dri` itself or anything
  under it, or a CDI name containing `gpu` (Podman: `nvidia.com/gpu=all`, `amd.com/gpu=all`).
- `Config.Annotations` or `HostConfig.Annotations` has a key starting `cdi.k8s.io/` whose value contains `gpu`.

Containers that map `/dev/dri` only for video decoding (Plex, Jellyfin, Frigate) are detected; the remedy
is `ignore_containers`. Privileged containers are not detected.

`DockerLikeRuntime.running_gpu_targets()` runs `<command> ps -q` then `<command> inspect <ids>` once and
builds `Target`s (sorted by name, `ref_id` the full id, `vram_mib` from the vram function keyed by full id).
`installed()` reports whether the command is on `PATH`; `available()` whether it also answers. `stop` runs
`<command> stop -- <name>` for each ref of this runtime in parallel and waits; `start` runs
`<command> start -- <name>` for each. The Podman command line is the same as Docker's for these calls.

### Host providers

Each takes `get_config: Callable[[], Config]`, so edits to `targets` apply on the next pass. A provider
lists only enabled, valid targets of its kind. `installed()` is True for all three; `available()` is True
for `service` only when `systemctl` exists.

- `services.py` `ServiceRuntime`: a `service` target is running when `systemctl [--user] is-active <unit>`
  prints `active`. `vram_mib` is the sum of the GPU memory of the processes in the unit's cgroup (None when
  unmeasurable). `stop` and `start` run `systemctl [--user] --no-ask-password stop|start <unit>`; `stop`
  runs the units in parallel. When a system service is refused ("Interactive authentication required",
  "Access denied", "Failed to stop") the log says to run `sudo gpu-game-guard polkit allow <unit>`; the
  failure is never raised. `Target.name` is the label; `extra` carries `unit`, `scope` and `id`.
- `commands.py` `CommandRuntime`: a `command` target is running when its `check` exits 0, or always when it
  has no `check`. `stop` and `start` run the argument lists with the target's timeout, no shell. A target
  with no `check` is always listed as running, so after `stop` the guard cannot tell that it stopped, does
  not record it and never runs its `start`. `extra` carries `id`.
- `processes.py` `ProcessRuntime`: a `process` target is running when the current user has a process whose
  `comm` name matches `match` (an fnmatch glob, case-sensitive). A pattern made only of wildcards is
  refused as too broad. A match that is protected (`discovery.is_protected`) or that is the guard itself or
  one of its ancestors is left alone, with one log line. `stop` sends SIGTERM to each matched pid after
  re-verifying its start time, waits up to 5 seconds, and never sends SIGKILL; processes that stay are
  logged. `extra` carries the `cmdline` and `cwd` of the oldest match so `start` can launch it again
  detached (new session, no input or output) when `restart` is true. `start` does nothing when a matching
  process is already running or no command line was recorded.

## discovery.py, presets and polkit.py

```python
BUILTIN_PROTECT: Tuple[str, ...]   # compositors, display servers, audio, session managers, login managers, Steam,
                                   # terminals, shells, file managers and the guard itself
def is_protected(config, name, cmdline=None) -> bool   # built-in list, configured globs, and a Python running the guard
@dataclass
class Candidate:
    key: str; kind: str; name: str; label: str; detail: str
    vram_mib: Optional[int]; managed: bool
    risk: str                      # "low" (container, service), "high" (process)
    spec: Optional[dict]           # a `targets` entry that would manage it; None for auto-detected containers
    note: str
def discover(config, runtimes, proc=Path("/proc"), run=None) -> List[Candidate]
```

Discovery lists, safest first: the containers the runtimes report, then host GPU users found through
`gpu.vram_by_pid()` grouped by systemd service (`service_from_cgroup`) and otherwise by process name, minus
protected names and minus anything that runs in a container. What a configured target already manages is
marked `managed` rather than omitted. A container is `managed` when `auto_containers` is true and no
`ignore_containers` pattern matches it. The suggested `spec` gets a unique id derived from the name. A
process whose name contains glob characters is not offered.

Risk levels: `low` for containers and services (stopped and started again cleanly), `medium` for commands
(the commands decide what happens; shown by the settings window, never produced by discovery), `high` for
processes (a program is asked to close, so unsaved work can be lost).

Presets are JSON files in `gpu_game_guard/presets/`, read with `pkgutil.get_data` so they work from a
zipapp; the file names are listed in `presets/__init__.py` because a zipapp cannot be enumerated.

```python
@dataclass(frozen=True)
class Preset: id: str; label: str; description: str; spec: dict; detect: dict
def load_presets() -> List[Preset]                 # a missing or malformed file is logged and skipped
def applicable(preset, which=shutil.which, run=None) -> bool   # the unit exists (`systemctl cat`) or the command is on PATH
```

Shipped presets: `ollama-system-service` (unit `ollama.service`, id `ollama`), `ollama-user-service` (the
user unit, id `ollama-user`) and `ollama-unload` (a command target: `stop` is a `python3 -c` script that
reads `/api/ps` on `127.0.0.1:11434` and unloads each model with `keep_alive` 0, `start` is `true`, `check`
exits 0 only while models are loaded).

`polkit.py` manages one rule file, `/etc/polkit-1/rules.d/60-gpu-game-guard.rules`, that answers `YES` for
the action `org.freedesktop.systemd1.manage-units` when the subject is a listed user and the unit is
listed for that user. The grants are kept as JSON inside the file, so `allow` merges and `revoke`
subtracts; a file not written by the guard is refused, and the file is deleted when no grant remains.

```python
def allow(units, user=None, rules_dir=RULES_DIR) -> Path     # needs root
def revoke(units, user=None, rules_dir=RULES_DIR) -> Path    # needs root
def status(units, user=None, rules_dir=RULES_DIR) -> Dict[str, bool]
class PolkitError(Exception)
```

The user defaults to `SUDO_USER`, then `USER` or `LOGNAME`, and never to root. Unit names must match
`[A-Za-z0-9:_.@-]+\.service`.

## dialogs.py

```python
def choose(preference="auto", environ=os.environ, which=shutil.which) -> Prompter
class ZenityPrompter, KdialogPrompter, NotifyPrompter, NullPrompter   # each has name, can_ask, available()
def launch_message(launch, targets, totals, markup) -> str     # markup: "html" | "pango" | "plain"
def restart_message(names, markup) -> str
def impact_sentence(targets, markup) -> str
def format_mib(mib) -> str
```

- `auto` order: the Qt dialog when PyQt5 is installed and a display variable exists; then KDE
  (`XDG_CURRENT_DESKTOP` contains KDE or `KDE_FULL_SESSION` is set) tries kdialog then zenity, anything
  else zenity then kdialog; then the notification backend (when `notify-send` exists); then `NullPrompter`.
  An explicit preference that is unavailable is logged and falls back to this order; `none` always gives
  `NullPrompter`.
- Buttons. Launch dialog: **Stop containers and launch** (`stop`), **Launch anyway** (`anyway`),
  **Cancel** (`cancel`); the Qt dialog words the first one **Stop and launch**. Restart dialog:
  **Restart** (True), **Ask me later** (None), **Leave stopped** (False). A backend that fails to start or
  crashes is never a deliberate answer: `launch` is `anyway` and `restart` is None. zenity cannot tell a
  closed window from a failure (both exit 1 with empty output), so closing its launch dialog is `anyway`
  and closing its restart dialog is None. kdialog can (a crash such as a missing display aborts with a
  status other than 0, 1 or 2), so its Esc and window-close status 2 is `cancel` at launch and None (ask
  later) after the game.
- `kdialog`: rich text (HTML subset), three buttons through `--yesnocancel` with custom labels. Launch:
  yes `stop`, no `anyway`, cancel `cancel`, any other status `anyway`. Restart: yes True, no False, cancel
  None, any other status None.
- `zenity`: Pango markup (`<b>`, `<small>`; newlines and bullets instead of lists), `--question` with
  `--ok-label`, `--cancel-label` and `--extra-button`; the extra button's label is printed on stdout with
  exit status 1, OK is exit 0, a dismissed window is exit 1 with empty output.
- `notify`: `can_ask` only when `notify-send --help` lists `--action`; otherwise it can only inform and
  `can_ask` is False. Never raises. An unanswered actionable notification gives up after 10 minutes: the
  launch question then counts as `anyway` and the restart question as ask later.
- `NullPrompter`: `can_ask` is False; `notify` logs.
- Window title `GPU Game Guard`. The message names the game, each target with image and GPU memory, the
  overall GPU use when known, and the graded impact sentence (modest, or heavy at `heavy_vram_mib`, passed
  through the module-level default `HEAVY_VRAM_MIB = 4096` that `cli.LiveConfig` overwrites from config). A
  target whose memory is unknown is shown as "GPU memory use unknown" and nothing is ever called idle.
  Units are GiB and MiB.
- Every user-supplied string (names, images, game titles) is escaped for the chosen markup.

## triggers

```python
# triggers/__init__.py
def build(names) -> List[Trigger]     # known: "steam"; unknown names are logged and skipped
# triggers/steam.py
class SteamTrigger:  name = "steam"; scan(); end(launch); relaunch(launch)
```

`SteamTrigger` detects `reaper SteamLaunch AppId=<id>` processes owned by the current user (the real uid
from `/proc/<pid>/status`; other users' launchers are ignored), native and Flatpak. `Launch.key` is
`(pid, start_time)`; `game_id` is `steam:<app id>`; `title` comes from Steam's manifests or non-Steam
shortcuts. `end` snapshots the launcher and its descendants with their start times, terminates the tree,
and force-ends stragglers after two seconds, re-checking each process's start time before every signal so a
reused pid is never signalled. `relaunch` runs `steam steam://rungameid/<id>` or the Flatpak equivalent,
using the 64-bit game id for shortcuts. The payload carries the trigger's `Game` dataclass.

## The Qt layer

`gpu_game_guard/ui/` is optional. Without PyQt5 the watcher and the command line work, and the dialog
backends fall back to zenity, kdialog and notifications.

### qt.py and theme.py

`qt.py`: `available() -> bool` (PyQt5's widget module can be imported; computed once), `QtCore`, `QtGui`
and `QtWidgets` resolved on first access, and `app()`, the `QApplication` singleton. Nothing else imports
PyQt5.

`theme.py`: `apply_theme(app)`, `is_dark`, `current_colors`, the stylesheet, the shared widgets
(`ToggleSwitch`, `Card`, `Badge`, `Pill`, `Callout`, `SectionHeader`, `ElidedLabel`, `button`,
`styled_label`), `app_icon(state)` (painted in code; states `ok`, `paused`, `attention`, `off`) and
`render(widget, path)` for off-screen PNG screenshots. The look is rounded cards, one accent colour, and
light or dark following the system palette.

### prompt.py

`QtPrompter` is a `Prompter` named `qt`. `can_ask` is True when `qt.available()` and `DISPLAY` or
`WAYLAND_DISPLAY` is set. It shows each dialog in a short-lived child process, `<self> prompt launch|restart`
(the zipapp or `python -m gpu_game_guard`), so a crash in Qt or the display server cannot take the watcher
down. The payload goes in as JSON on standard input and the answer comes back as JSON on standard output;
a crash, a timeout (10 minutes) or unusable output fails safe exactly like the other backends: `anyway` for
a launch, None for a restart.

```
payload: {"kind": "launch" | "restart", "title": str,
          "targets": [{"kind", "name", "image", "vram_mib": int | null, "label"}],
          "totals": [used_mib, total_mib] | null, "remember": bool, "heavy_vram_mib": int}
answer:  {"choice": "stop" | "anyway" | "cancel", "remember": "stop" | "ignore" | null}   (launch)
         {"choice": "restart" | "later" | "leave"}                                         (restart)
```

`label` is the unit of a service; it is shown where an image would be. The launch dialog shows the game
title, a card per target (a kind badge, the name, image or unit, and memory: known, unknown or none
measured), the graded impact sentence, the overall GPU use, an optional "Remember my choice for this game"
checkbox (only when `remember_choices`), and the buttons Cancel, Launch anyway and Stop and launch.
Closing the window means Cancel. The remembered action is `stop` only together with the stop button and
`ignore` only together with Launch anyway; `consume_remember()` returns it once. The restart dialog lists
the stopped targets with Leave stopped, Ask me later and Restart; closing it means Ask me later.

### tray.py

`run() -> int` starts a `QSystemTrayIcon` application that reads the status every 2 seconds, with the pause
file applied so a pause shows at once. The icon state follows the status: `ok`, `paused`, `attention` (a
question is open, a restart is pending or the config file has a problem) and `off` (the service is not
running or the guard is switched off). The menu has a status line, Pause for 1 hour, Pause until resumed,
Resume (visible only while paused), Open settings (starts `gpu-game-guard settings` as a detached process;
a click on the icon does the same) and Quit. When no system tray exists it logs that and returns 0. The
autostart entry starts it at login. `TrayController` takes its inputs as callables so it runs without a
real tray.

### settings_model.py: the settings model

`SettingsModel` (a `QObject` with a `changed` signal) is the state behind the settings window. It works
without a window; its file, status directory, discovery function and command runner are injectable, so
tests never touch the real system.

- A working copy of the `Config` (`config`) beside the saved one (`saved`); `dirty` is true when they differ.
- Changes go through `update(**fields)`, `set_config(cfg)`, `set_target(spec)`, `remove_target(id)`,
  `set_game(id, name, action)` and `remove_game(id)`; each emits `changed`. Nothing reaches the file
  until `apply()`.
- `problems` holds `config.validate` of the working copy. `apply()` validates, writes through
  `config.save` and returns the list of problems (empty when saved); `revert()` goes back to the saved copy.
- When the file could not be read, `load_error` is set, the working copy starts at the defaults, and
  `apply()` refuses until `accept_defaults()` says the file may be replaced.
- `candidates(refresh=False)` runs `discovery.discover` once and caches the result.
- Live views of the service: `status()`, `events(limit)`, `recent_games()` (games with an `intercept`
  event, newest first), `pause(seconds)`, `resume()`, `paused()`.
- `restart_needed()` is true when `runtimes`, `triggers` or `dialog` were saved with a value the running
  service has not seen; `restart_service()` runs `systemctl --user restart gpu-game-guard.service`.
- `plan_for(game_id, targets, can_ask)` and `effective_action_for_game(game_id)` use `guard.plan` and the
  working copy.
- `service_state(model, status)` gives the state shown by the header pill and the Basic status card:
  watching, paused, not running, restart pending or switched off.

### settings.py

`run() -> int` is the entry point of `gpu-game-guard settings`. `SettingsWindow` has a header (logo, state
pill and a Basic or Advanced switch), the page for the chosen mode, notices (the settings file could not be
read; the service is not running, with the command to start it) and a footer with the problems of a failed
save, the restart banner with a Restart service button, Revert and Apply (also Ctrl+S). The chosen mode
and the window size are kept in `ui.json` next to the config file. Closing the window with unsaved changes
asks whether to save. Advanced mode is built on first use; if its module cannot be loaded the window shows a
notice and Basic keeps working. The window always uses the default config path, not `--config`.

### basic.py

The Basic page writes straight into the model. A status card (watching, paused, switched off, not running)
with Pause 1 hour, Pause until resumed, Resume and Switch on; "When I start a game" (Ask me, Stop them
automatically, Do nothing: `on_launch`); "What gets paused", a list of what discovery found that is safe to
offer, each with its GPU memory and a switch; "After the game" (Ask me, Start them again automatically,
Leave them stopped: `after_game`); and a link to Advanced. Only containers and services are listed.
Switching a container off adds its name to `ignore_containers` (and on removes it again, turning
`auto_containers` back on if needed); switching a service on adds a `service` target and off removes it.
Configured services that are not running are listed as idle. Commands and processes are managed only in
Advanced.

### advanced.py and the tabs

`AdvancedView` is a `QTabWidget` with one module per tab. Each tab reads from and writes to the model only
and reloads when the model changes.

| Tab | What it covers |
| --- | --- |
| Targets | auto-detected containers (read only) and configured targets with an on switch, kind badge (risk colour), details and memory; Add from preset, Add service, Add command, Add process, Edit, Remove |
| Games | the per-game rules (`games`): add, change the action, remove; recent games fill in the id and name |
| Detection | `auto_containers`, `ignore_containers`, `protect` (and the built-in list, shown read-only), `heavy_vram_mib`, `runtimes` (tick and order) |
| Timing | `poll_seconds`, `exit_grace_seconds`, `no_game_timeout_seconds`, `bypass_seconds`, `dialog_retry_seconds`, `enabled`, `remember_choices` |
| Dialogs | `dialog` with which backends are installed here, and `triggers` |
| Activity | the live status and the recent events from `events.jsonl` |
| Test run | a preview of what the guard would do for a game, from `guard.plan`; nothing is stopped or started |
| Raw config | the whole configuration as validated JSON, with Validate, Apply (into the working copy), Reset, Import and Export |

The add and edit dialogs validate as the user types. The service dialog shows the `polkit allow` command
for a system service and whether a rule already allows the unit; the process dialog warns that unsaved work
can be lost, that the program is asked to close politely and never forced, and refuses a name on the
protect list or a pattern that would also match one; the command dialog takes the stop, start and check
commands as shell-style lines and splits them into argument lists.

## cli.py

```
gpu-game-guard [--config PATH] [run]      run the watcher (default); --config may also follow the subcommand
gpu-game-guard check                      diagnose and print: version, config path and whether it is valid, dialog
                                          backend, runtimes, triggers, launches visible now, GPU containers now,
                                          GPU totals, configured targets and whether each is running, the service
gpu-game-guard config path | show | validate | init
gpu-game-guard pause [--for 30m|2h|1d] | resume
gpu-game-guard status [--json]
gpu-game-guard targets list | enable ID | disable ID | remove ID
gpu-game-guard presets list | add ID
gpu-game-guard discover
gpu-game-guard polkit allow|revoke|status UNIT... [--user NAME]
gpu-game-guard settings | tray            open a Qt window or run the tray (need PyQt5)
gpu-game-guard prompt launch|restart      internal: the dialog child process
gpu-game-guard --version
```

`config init` writes the defaults to the config path and refuses to overwrite; `config show` prints the
effective config as JSON, and only JSON, on stdout; `config validate` also reports unknown keys. `status`
reads `status.json` and exits 1 with a message when the service is not running. `targets` and `presets add`
rewrite the config file through `config.save`. `settings`, `tray` and `prompt` import `ui` lazily; without
PyQt5 they print "Install python3-pyqt5 (sudo apt install python3-pyqt5)" and exit 1. A duration is a
number with `s`, `m`, `h` or `d`.

Exit status: 0 on success; 1 on a failed check, an invalid config, a service that is not running (`status`)
or another failed command; 2 on bad usage; 78 when `run` refuses to start on an invalid config (the systemd
unit does not restart on 78); 130 on Ctrl-C. `main(argv=None) -> int`.

`run` reads the config once to build the triggers, the runtimes (skipping those whose command is not
installed) and the dialog backend; those are fixed until the service restarts. Everything else is read
from the `ConfigWatcher` on each pass. `cli.LiveConfig` wraps the watcher: on each `current()` it applies
`heavy_vram_mib` and `remember_choices` to the `dialogs` module and returns the config.

## Packaging

- `scripts/build-zipapp.sh OUTPUT_PATH` builds a single executable `gpu-game-guard` (a `zipapp` with
  `#!/usr/bin/env python3`) from the package's `.py` and `.json` files, with fixed modification times.
- `install.sh` (user install) downloads the tarball of a git ref, builds the zipapp and installs it to
  `~/.local/bin/gpu-game-guard`, the systemd user unit under `~/.config/systemd/user`, the Settings entry
  (`~/.local/share/applications`), the tray autostart entry (`~/.config/autostart`) and the icon
  (`~/.local/share/icons/hicolor/scalable/apps`), enables the unit and starts it. `GPU_GAME_GUARD_SRC`
  installs from a local checkout instead. It rolls back to the previous files when anything fails, and
  `--uninstall` removes everything it installed (`--purge` also removes the state directory). The service
  runs `gpu-game-guard run`.
- `scripts/build-deb.sh OUTPUT_DIR` builds `gpu-game-guard_<version>_all.deb` with
  `dpkg-deb --root-owner-group`, reproducibly (fixed modification times): `/usr/bin/gpu-game-guard` (the
  zipapp), `/usr/lib/systemd/user/gpu-game-guard.service`, `/usr/share/applications/gpu-game-guard.desktop`
  (Settings) and `gpu-game-guard-tray.desktop`, `/etc/xdg/autostart/gpu-game-guard-tray.desktop`, the icon
  and `/usr/share/doc/gpu-game-guard/` (copyright, README, changelog). Control: `Depends: python3 (>= 3.9)`,
  `Recommends: python3-pyqt5, zenity | kdialog | libnotify-bin, docker.io | podman`,
  `Suggests: polkitd | policykit-1`. `postinst` runs `systemctl --global enable gpu-game-guard.service`
  when systemctl exists and never fails, so the service starts at each user's next login; `prerm` and
  `postrm` disable it.
- The unit (`gpu-game-guard.service`) is `After=`, `PartOf=` and `WantedBy=graphical-session.target`, with
  `Restart=on-failure`, `RestartPreventExitStatus=78` and `RestartSec=5`.
- `.github/workflows/ci.yml` runs the tests on Python 3.9 to 3.13 and on the system Python of Debian 12,
  Ubuntu 22.04 and Ubuntu 24.04, the Qt tests offscreen with the distribution's PyQt5, a syntax check and
  shellcheck of the scripts with the installer tests, the zipapp build, and the `.deb` build with its
  tests and an install and purge in Debian 12, Ubuntu 22.04 and Ubuntu 24.04 containers.
