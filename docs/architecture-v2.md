# Architecture v2 (the shipping build)

This extends `docs/architecture.md` (read that and `gpu_game_guard/model.py` first). Everything there still
holds unless this document says otherwise. v2 adds host-level targets, a status channel, a Qt prompt, a tray
application, a settings window with Basic and Advanced modes, and a `.deb`.

## Rules for every module

Python 3.9 to 3.13, standard library only for everything outside `gpu_game_guard/ui/`; `from __future__ import
annotations` everywhere; no `X | Y` outside annotations; comments describe the code (no first or second person, no
names of people); external commands by list through `subprocess`, never a shell; failures log one line through
`util.log` and never stop the watcher; tests are `unittest`, hermetic (no real Docker, systemctl, Steam, display or
home directory: use temp dirs and fakes), run with `python3 -m unittest discover -s tests -t .`; ruff-clean
(`~/.local/bin/ruff check gpu_game_guard tests`). Code under `gpu_game_guard/ui/` imports PyQt5 only through
`gpu_game_guard/ui/qt.py`; its tests set `QT_QPA_PLATFORM=offscreen` and skip cleanly when PyQt5 is missing
(`unittest.skipUnless(qt.available(), ...)`). Never show anything on the real screen from a test or a script.

## Config additions (config.py)

New keys, all optional, with defaults:

```json
{
  "enabled": true,
  "auto_containers": true,
  "remember_choices": true,
  "protect": ["my-editor*"],
  "runtimes": ["docker", "podman", "service", "command", "process"],
  "targets": [
    {"id": "ollama", "kind": "service", "label": "Ollama", "unit": "ollama.service", "scope": "system"},
    {"id": "ollama-unload", "kind": "command", "label": "Ollama models", "stop": ["python3", "-c", "..."], "start": ["true"],
     "check": ["true"]},
    {"id": "obs", "kind": "process", "label": "OBS Studio", "match": "obs", "restart": true}
  ]
}
```

- `enabled` (bool, default true): master switch. False means the guard watches but never intercepts or stops anything.
- `auto_containers` (bool, default true): detect GPU containers automatically. False means no container is managed
  (the container runtimes are not queried). Individual containers are excluded with `ignore_containers`.
- `remember_choices` (bool, default true): whether the Qt dialog offers "remember my choice for this game".
- `protect` (list of fnmatch globs on a process or unit name, default `[]`): additions to the built-in protect list
  (`discovery.BUILTIN_PROTECT`). A protected name is never offered by discovery and a `process` target matching it is
  refused at run time.
- `runtimes` default becomes `["docker", "podman", "service", "command", "process"]`.
- `targets` (list, default `[]`): host targets. Each entry is a dict validated per kind; common keys `id`
  (required, unique, `[a-z0-9][a-z0-9_-]{0,39}`), `kind` (`service`, `command` or `process`), `label` (string, default the
  id), `enabled` (bool, default true).
  - `service`: `unit` (required, must end in `.service`, no spaces or `/`), `scope` (`"system"` default, or `"user"`).
  - `command`: `stop` (required, non-empty list of strings), `start` (required, non-empty list of strings), `check`
    (optional list of strings: exit status 0 means the target is running; when absent the target counts as running
    whenever it is enabled), `timeout_seconds` (number 1 to 600, default 60).
  - `process`: `match` (required string, an fnmatch glob on the process's `comm` name, no `/`), `restart` (bool, default
    true: start the same command line again after the game).
- `validate()` reports problems per target by index and id, and unknown keys inside a target (did-you-mean).
  Python API additions: `@dataclass class TargetSpec` with the fields above (`stop`, `start`, `check`, `match`,
  `unit`, `scope`, `restart`, `timeout_seconds` defaulting to None/False as appropriate), `Config.targets:
  List[TargetSpec]`, `Config.target(id) -> Optional[TargetSpec]`, `Config.is_protected(name) -> bool`
  (built-in plus configured globs, case-insensitive), `Config.with_target(spec)` and `Config.without_target(id)`
  returning updated copies, and `Config.with_game(game_id, name, action)` returning an updated copy.
- A game rule's `action` may also be omitted (it still defaults to "ask").

## State (state.py)

Refs keep the optional keys `scope`, `unit`, `id` (str), `cmdline` (list of str) and `cwd` (str) when valid; other
keys are dropped. The loader stays strict as before.

## Status channel (status.py, new; all paths under `$XDG_STATE_HOME/gpu-game-guard/`)

```python
@dataclass
class Status:                       # what status.json holds
    version: str; pid: int; updated: float
    state: str                      # "watching" | "paused" | "disabled" | "asking" | "game-running" | "restart-pending"
    paused_until: Optional[float]   # wall time, or None when not paused; paused with no end is float("inf") stored as null + paused true
    paused: bool
    game: Optional[dict]            # {"id": ..., "title": ...} of the game now running or being asked about
    stopped: List[Ref]              # what the guard stopped and has not yet restarted
    config_ok: bool; config_error: Optional[str]
    dialog: str; can_ask: bool; targets: int   # backend name, whether it can ask, how many configured targets
def write_status(status: Status, directory: Optional[Path] = None) -> None      # atomic
def read_status(directory: Optional[Path] = None, max_age: float = 15.0) -> Optional[Status]   # None when missing, unreadable, or older than max_age
def append_event(kind: str, message: str, directory=None, **fields) -> None   # JSON line {"t": wall, "kind", "message", ...}; keep the last 300 lines
def read_events(limit: int = 100, directory=None) -> List[dict]               # newest last
def pause(seconds: Optional[float] = None, directory=None) -> None   # None = until resumed; writes pause.json {"until": wall|null}
def resume(directory=None) -> None
def paused(directory=None, now: Optional[float] = None) -> Tuple[bool, Optional[float]]   # (is paused, until)
```

The Guard writes the status at most once per pass and after each state change, appends an event for every
interception, answer, stop, restart, failure and config problem, and treats a paused or `enabled: false` guard as
"watch only": launches are adopted and left alone, but pending restart questions are still asked.

## Guard additions (guard.py)

- Targets come from every runtime and are combined as before. The `plan` function (pure, no side effects) lets the
  settings window preview behaviour:

```python
@dataclass
class Plan:
    action: str                 # "ask" | "stop" | "ignore" | "paused" | "disabled" | "nothing-running"
    will_stop: List[Target]; ignored: List[Target]; can_ask: bool; note: str
def plan(config: Config, game_id: str, targets: Sequence[Target], can_ask: bool, paused: bool = False) -> Plan
```

- Remembered choices: after `prompter.launch(...)` the guard calls `prompter.consume_remember()` if the prompter has
  it; for "stop" or "ignore" it saves a game rule through `config.save(config.with_game(...))` (log, never raise).
  Only when `config.remember_choices` is true.
- `Guard.__init__` gains the optional `status_dir` and `dialog_name` parameters used for the status channel.

## Host runtimes (new modules; each implements `Runtime`; constructor takes `get_config: Callable[[], Config]`)

- `services.py` `ServiceRuntime`: for each enabled `service` target: running when `systemctl [--user] is-active
  <unit>` prints `active`. `vram_mib` is the sum of GPU memory of processes in the unit's cgroup (None when unmeasurable).
  `stop`/`start` run `systemctl [--user] --no-ask-password stop|start <unit>`; an authorisation failure is
  detected from the output ("Interactive authentication required", "Access denied") and logged with the fix
  (`gpu-game-guard polkit allow <unit>`), never raised. Target `name` is the label, `extra` carries `unit`, `scope`, `id`.
- `commands.py` `CommandRuntime`: for each enabled `command` target: running when `check` exits 0 (or always when there
  is no `check`). `stop`/`start` run the argv lists with the timeout, no shell. `extra` carries `id`.
- `processes.py` `ProcessRuntime`: for each enabled `process` target: finds the current user's processes whose `comm`
  matches `match`, refusing (log once) a match that `Config.is_protected` rejects or that is the guard itself or one
  of its ancestors. `stop` sends SIGTERM to each matched pid after re-verifying its start time, waits up to 5
  seconds, and never sends SIGKILL (logs processes that stayed). `extra` carries `cmdline` and `cwd` of the oldest
  match so `start` can launch it again detached (`start_new_session`, output discarded) when `restart` is true.
- `runtimes.build(names, get_config=None)`: also builds `service`, `command` and `process` runtimes when
  `get_config` is given (they need the live config); `installed()` is True for them, `available()` True for
  `service` only when `systemctl` exists.
- Refs for another provider are ignored by each provider, as before.

## Discovery and presets (discovery.py, presets.py, polkit.py, `presets/*.json`)

```python
BUILTIN_PROTECT: Tuple[str, ...]   # compositors, display servers, audio, session managers, steam, terminals, shells, the guard
@dataclass
class Candidate:
    key: str; kind: str; name: str; label: str; detail: str
    vram_mib: Optional[int]; managed: bool; risk: str   # "low" (container, service), "medium" (command), "high" (process)
    spec: Optional[dict]      # a `targets` entry that would manage it; None for auto-detected containers
    note: str
def discover(config: Config, runtimes: Sequence[Runtime], proc: Path = Path("/proc")) -> List[Candidate]
def load_presets() -> List[Preset]            # Preset: id, label, description, spec(dict), detect(dict)
def applicable(preset, which=shutil.which, run=...) -> bool   # the unit exists or the command is installed
def render_rule(units: Sequence[str], user: str) -> str       # polkit JS rule text
def allow(units, user=None, rules_dir=Path("/etc/polkit-1/rules.d")) -> Path   # writes the rule; needs root
def revoke(units, ...) ; def status(units) ...
```

Discovery lists, safest first: auto containers (from the runtimes), then host GPU users found through
`gpu.vram_by_pid()` (compute processes from nvidia-smi; fdinfo for other vendors) grouped by systemd unit through
`/proc/<pid>/cgroup` (`...<name>.service` outside `user@*.service/app.slice` internals) and otherwise by process
name, minus protected names, minus already managed targets (`managed=True` rather than omitted). `gpu.py` gains
`vram_by_pid(...) -> Optional[Dict[int, int]]` (None when unmeasurable). Presets ship as JSON in
`gpu_game_guard/presets/` and are read with `pkgutil.get_data` (zipapp safe): at least an Ollama system service, an
Ollama user service, and an Ollama model unload command (stop: a `python3 -c` script that reads `/api/ps` on
127.0.0.1:11434 and unloads each model with `keep_alive` 0, start: `true`, check: exits 0 only when models are loaded).

## Qt layer (`gpu_game_guard/ui/`)

- `qt.py`: `available() -> bool`, and re-exports `QtCore, QtGui, QtWidgets` (PyQt5). Nothing else imports PyQt5.
- `theme.py`: `apply_theme(app)`, `is_dark(app) -> bool`, `colors(dark) -> Colors`, `stylesheet(colors) -> str`,
  widgets `ToggleSwitch`, `Card`, `Badge`, `SectionHeader`, `Pill`, `app_icon(state="ok") -> QIcon` (painted in code,
  states `ok`, `paused`, `attention`, `off`), plus `render(widget, path)` for off-screen PNG screenshots. The look:
  rounded cards, generous spacing, one accent colour, light and dark following the system palette.
- `prompt.py`: `QtPrompter` (a Prompter; `can_ask` True only when `available()` and a display variable exists; runs the
  dialog in a subprocess `<self> prompt launch|restart` that reads a JSON payload on stdin and prints a JSON answer;
  crash, timeout or bad output fail safe exactly like the other backends: launch "anyway", restart None) and
  `run_prompt(argv, stdin, stdout) -> int` for the subprocess. Launch dialog: game title, a card per target with
  image or unit, memory (known/unknown/none measured), the graded impact sentence, an optional "Remember my choice
  for this game" checkbox (only when `remember_choices`), buttons Stop and launch / Launch anyway / Cancel; closing
  the window means Cancel. `consume_remember()` returns "stop" or "ignore" when ticked with Stop or Launch anyway.
  Restart dialog: lists targets, buttons Restart / Ask me later / Leave stopped; closing means Ask me later.
  `dialogs.choose("auto")` prefers `qt` when available, then the existing order; preference `qt` is accepted.
- `tray.py`: `run() -> int`: a `QSystemTrayIcon` application that reads the status every 2 seconds; menu: a status line, Pause
  for 1 hour, Pause until resumed, Resume, Open settings, Open logs help, Quit; icon state follows the status; if no
  system tray exists it logs that and exits 0. Started at login by the autostart entry.
- `settings.py`: `run() -> int`, a main window with a Basic page and an Advanced mode (a switch in the header,
  remembered in `ui.json` next to the config). Basic: a status card (running, paused, pending restart); "When I start a
  game" (Ask me, Stop automatically, Do nothing); "What gets paused" (discovered containers and services with friendly
  names, memory, a toggle each, hidden risky items); "After the game" (Ask me, Restart automatically, Leave stopped);
  Pause/Resume. Advanced: tabs Targets (all kinds, add from presets, add custom command/service/process with
  warnings), Games (per-game rules table), Detection and protect list, Timing, Dialogs and runtimes, Activity (events
  and live status), Test run (uses `guard.plan`), Raw config (validated editor, import and export). Writes the config
  through `config.save` after validation; shows a banner with a Restart service button when a setting that needs a
  restart changed.

## CLI additions (cli.py)

`pause [--for 30m|2h|1d]`, `resume`, `status [--json]` (reads status.json, says when the service is not running),
`targets list|enable ID|disable ID|remove ID`, `presets list`, `presets add ID`, `discover`, `polkit allow|revoke|status
UNIT...`, `settings`, `tray`, and the internal `prompt launch|restart`. `settings`, `tray` and `prompt` import `ui`
lazily; without PyQt5 they print "Install python3-pyqt5 (sudo apt install python3-pyqt5)" and exit 1. `check` also lists
configured targets (with whether each is running) and the status channel's view of the service.

## Packaging

- `scripts/build-deb.sh OUTPUT_DIR` builds `gpu-game-guard_<version>_all.deb` with `dpkg-deb --root-owner-group`:
  `/usr/bin/gpu-game-guard` (the zipapp), `/usr/lib/systemd/user/gpu-game-guard.service`,
  `/usr/share/applications/gpu-game-guard.desktop` (Settings) and `gpu-game-guard-tray.desktop`,
  `/etc/xdg/autostart/gpu-game-guard-tray.desktop`, `/usr/share/icons/hicolor/scalable/apps/gpu-game-guard.svg`,
  `/usr/share/doc/gpu-game-guard/` (copyright, README, changelog). Control: `Depends: python3 (>= 3.9)`,
  `Recommends: python3-pyqt5, zenity | kdialog | libnotify-bin, docker.io | podman`, `Suggests: polkitd | policykit-1`.
  `postinst` runs `systemctl --global enable gpu-game-guard.service` when systemctl exists and never fails;
  `prerm`/`postrm` disable it. Reproducible (fixed mtimes).
- `install.sh` (user install) additionally installs the desktop entries, the autostart entry and the icon under the
  user's XDG directories, and removes them on `--uninstall`.
