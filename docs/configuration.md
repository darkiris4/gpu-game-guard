# Configuration

GPU Game Guard works without a configuration file. Every key below is optional; a missing key takes its
default. The file is JSON. Most people never edit it by hand: the settings window (`gpu-game-guard settings`)
reads and writes the same file, and its Raw config tab edits it as text.

## Location

```
$XDG_CONFIG_HOME/gpu-game-guard/config.json        (usually ~/.config/gpu-game-guard/config.json)
```

`gpu-game-guard config path` prints the exact path in use, and `--config PATH` selects another file for one
invocation; it works before or after the subcommand. A missing file means all defaults. The settings window
and the tray always use the default path.

| Command | Effect |
| --- | --- |
| `gpu-game-guard config path` | print the path |
| `gpu-game-guard config init` | write the defaults to the path; refuses to overwrite an existing file |
| `gpu-game-guard config show` | print the effective configuration as JSON (only JSON on stdout) |
| `gpu-game-guard config validate` | report problems and unknown keys; exit status 1 when any are found |
| `gpu-game-guard check` | diagnose the whole setup, including whether the config file is valid |

`config validate` reports unknown keys (typos such as `on_lauch`) with a did-you-mean hint and exits 1,
including unknown keys inside a game rule or a target. Loading a file with unknown keys still works: the
guard ignores them, logs a warning to stderr and writes them back unchanged on every save. A wrong type, a
value outside the allowed set, a number outside its range or values nested more than 100 levels deep makes the
file invalid.

Logs go to stderr, which the systemd journal captures. Exit codes: 0 ok; 1 a failed check, an invalid
config or another failed command; 2 usage error; 78 when `run` refuses to start on an invalid config (the
service does not restart on 78); 130 on Ctrl-C.

Files are written atomically and keep the existing file's permissions (a new file gets 0644 as masked by
the umask; a symlinked file is written through). Rewriting the config (the settings window, `targets`,
`presets add`, a remembered choice) saves it in a fixed layout with sorted keys. Each save is a read-modify-write
under a lock on `config.json.lock` (next to the file, mode 0600), so two writers do not lose each other's
changes; without the lock the save goes ahead after a warning. Before a save replaces a valid file with
different content, it is copied to `config.json.bak`, one rolling backup with the same mode. A file that could
not be used is copied to `config.json.broken-<time>` instead, and the newest five are kept.

## Keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | The master switch. `false` means the guard only watches: launches are never interrupted and nothing is stopped. A restart question that is already pending is still asked. |
| `on_launch` | string | `"ask"` | What to do when a game launches while GPU workloads run: `ask`, `stop` or `ignore`. |
| `after_game` | string | `"ask"` | What to do once the game has closed: `ask`, `restart` or `leave`. |
| `auto_containers` | boolean | `true` | Find GPU containers automatically. `false` means no container is managed and Docker and Podman are not queried. |
| `remember_choices` | boolean | `true` | Whether the launch dialog offers "Remember my choice for this game" (Qt dialog only). |
| `ignore_containers` | list of strings | `[]` | Glob patterns on container names. Matching containers are never counted, stopped or restarted. |
| `protect` | list of strings | `[]` | Glob patterns on process names, added to the built-in protect list. See [protect](#protect). |
| `targets` | list of objects | `[]` | Host targets: systemd services, commands and processes. See [targets](#targets). |
| `runtimes` | list of strings | `["docker", "podman", "service", "command", "process"]` | The providers to use, in order of preference. A provider whose command is missing is skipped. A kind of target is only managed when its provider is listed. |
| `triggers` | list of strings | `["steam"]` | Launch detectors to enable. Only `steam` exists. |
| `dialog` | string | `"auto"` | Dialog backend: `auto`, `qt`, `zenity`, `kdialog`, `notify` or `none`. |
| `games` | object | `{}` | Per-game rules, keyed by game id. See [per-game rules](#per-game-rules). |
| `poll_seconds` | number | `1.0` | Interval between checks for new launches. Range 0.2 to 60. |
| `exit_grace_seconds` | number | `8.0` | How long the game must be absent before it counts as closed. Steam's launcher processes briefly disappear between stages of a launch and an exit. Range 2 to 3600. |
| `no_game_timeout_seconds` | number | `180.0` | Workloads were stopped but the relaunched game never appeared: after this long, the after-game action runs anyway. Range 30 to 86400. |
| `bypass_seconds` | number | `120.0` | After the guard relaunches a game, that game is let through for this long so the relaunch is not intercepted again. Range 5 to 3600. |
| `dialog_retry_seconds` | number | `300.0` | When the restart question was not answered, wait this long before asking again. Range 30 to 86400. |
| `heavy_vram_mib` | integer | `4096` | GPU memory (MiB) held by the workloads together at which the dialog's warning changes from tentative to heavy. Range 0 to 10000000. |

### on_launch

- `ask`: end the launch, show the dialog, act on the answer (**Stop containers and launch**,
  **Launch anyway**, **Cancel**; the Qt dialog says **Stop and launch**). A dialog tool that fails counts as
  **Launch anyway**; closing a zenity window counts as **Launch anyway**, closing a kdialog window or the Qt
  window, or pressing Esc in kdialog, counts as **Cancel**. When the dialog backend cannot ask (no dialog
  tool found, or the notification backend without action support), the launch is not ended at all: the
  game starts untouched, nothing is stopped, and the log says so once per game.
- `stop`: end the launch, stop the GPU workloads without asking, start the game again, and show a
  notification naming what was stopped. This works with or without a dialog tool.
- `ignore`: do nothing at all. The launch is not interrupted and no dialog appears.

### after_game

- `ask`: show a dialog offering to start what was stopped, with **Restart**, **Ask me later** and **Leave
  stopped**. **Leave stopped** forgets them. **Ask me later**, closing the window, or a dialog backend that
  fails retries after `dialog_retry_seconds` with the list of stopped workloads kept. When no way to ask
  exists, the workloads the guard stopped are started again with a notification.
- `restart`: start them without asking, and show a notification.
- `leave`: forget them without asking. They stay stopped.

The list of what was stopped lives in `$XDG_STATE_HOME/gpu-game-guard/state.json` until it has been dealt
with, so it survives a crash or a reboot.

When a start fails, the targets stay in that list and are tried again after `dialog_retry_seconds`, then after
twice that, four times and so on, never more than an hour apart (or `dialog_retry_seconds` when that is longer).
After five failures in a row to start the same targets, the guard says so once in the log and then retries
hourly without dialogs or notifications. They stay recorded; `gpu-game-guard state clear` forgets them.

### dialog

`auto` uses the Qt window when `python3-pyqt5` is installed and a display exists, then zenity or kdialog
(kdialog first on KDE), then desktop notifications, then nothing. An explicit value that is not available
here is logged and replaced by that automatic choice. `none` never asks.

### runtimes

The default includes all five providers. A kind of target is managed only while its provider is listed, so a
file that sets `"runtimes": ["docker"]` manages containers but no service, command or process target.
`docker` and `podman` are used only while `auto_containers` is true. Unknown names are logged and skipped.

## Targets

`targets` is a list of objects. Each describes one thing the guard stops before a game and starts again
afterwards. Containers do not need an entry: they are found automatically.

Keys common to every kind:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | required | A short unique name, matching `[a-z0-9][a-z0-9_-]{0,39}`. It is how the guard remembers the target across a stop and a restart, so a target renamed or removed while it is stopped can no longer be started again by the guard. |
| `kind` | string | required | `service`, `command` or `process`. |
| `label` | string | the `id` | The name shown in dialogs and lists. Labels need not be unique; ids must be. |
| `enabled` | boolean | `true` | `false` keeps the entry without managing it. |

An invalid entry is reported by `config validate` with its index and id. At run time an invalid entry is
logged once and skipped; the others keep working.

### service

A systemd service, stopped and started with `systemctl`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `unit` | string | required | The unit name: letters, digits and `:_.@-`, ending in `.service`; no spaces or `/`. |
| `scope` | string | `"system"` | `"system"`, or `"user"` for a unit of the user's own systemd instance (`systemctl --user`). |

The service counts as running while `systemctl is-active` says `active`. Its GPU memory is the memory of the
processes in the unit's control group. A **system** service needs permission to be stopped: see
[polkit](#polkit-for-system-services). A user service needs none.

### command

A pair of commands, for anything that is not a service: a program that can unload its models, a script, a
container started some other way.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `stop` | list of strings | required | The command that frees the GPU: the program and its arguments, as a list. |
| `start` | list of strings | required | The command that brings the workload back. |
| `check` | list of strings | none | A command whose exit status 0 means the workload is running. |
| `timeout_seconds` | number | `60` | Limit for each of the three commands. Range 1 to 600. |

Commands are run directly, never through a shell. To use a pipe, a redirection or a variable, start a shell
yourself: `["sh", "-c", "..."]`. A list entry must not be empty. The GPU memory of a command target is never
measured, so the dialog shows "GPU memory use unknown" for it.

**Give a `check` when you can.** With a `check` that exits 0 only while the workload holds the GPU, the target
is offered only when there is something to stop, and is recorded as stopped once the check fails. Without one
the target is offered whenever it is enabled: the guard counts it as stopped once its `stop` command has
exited with status 0, and as running again once `start` has run or the target has been released (the answer
was **Leave stopped**, `after_game` is `leave`, or `gpu-game-guard state clear`). The guard rebuilds that
record from `state.json` when it restarts. It is the guard's belief, not a measurement.

### process

A running program, matched by name, asked to close with SIGTERM and started again afterwards.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `match` | string | required | A glob (`fnmatch`) matched, case-sensitively, against the name of the user's processes. 3 to 15 characters, at least 3 of them not `*?[]`, no `/`. |
| `restart` | boolean | `true` | Start the same command line again after the game, detached, in the same working directory. |

- `match` is compared with the process name as the kernel reports it (`/proc/<pid>/comm`), which is cut to
  15 characters, hence the limit. A pattern such as `obs` or `firefox*` is typical. A pattern that matches
  more than 25 root processes (matching processes whose parent is not itself a match, so the helpers of a
  multi-process program do not count) is refused when the guard runs as too broad.
- Only the current user's processes are considered. The guard itself and its parent processes are never
  signalled, and neither is anything on the [protect list](#protect).
- Processes are terminated, never killed: the guard sends SIGTERM, waits up to five seconds and logs what
  stayed. It never sends SIGKILL. **Unsaved work in the program can be lost**, and a restarted program
  starts with the command line it had, which may not restore its previous state.
- The command line and working directory are read when the program is stopped, not when it is scanned. A
  relative first entry is made absolute (against the process's working directory, or its `PATH`). The program
  is restarted through `systemd-run --user --scope` when that and the user manager are available, so it
  survives a restart of the guard; otherwise it shares the guard's lifetime, and the log says so once.
- A program whose command line cannot be told is reported as failed to restart, with the reason logged once:
  nothing readable, an executable that cannot be found, or a program that renamed itself to a bare interpreter
  (`python3`, `bash`, `node` and the like with no script argument), which the guard will not start with
  nothing to run. A restarted program that exits with an error within about a second and a half counts as
  failed too.
- With `restart: false` the program stays closed after the game.

### Risk levels

The settings window marks each kind with a risk level.

| Kind | Risk | Why |
| --- | --- | --- |
| container | low | stopped and started again cleanly |
| service | low | stopped and started again cleanly; anything relying on it stops meanwhile |
| command | medium | the commands run as written, so they decide what happens |
| process | high | a program is asked to close, so unsaved work can be lost |

### polkit for system services

Stopping a system service needs administrator rights, and the guard runs as the user. Either choose a user
service, or let the user manage the specific unit without a password with a polkit rule:

```sh
sudo gpu-game-guard polkit allow ollama.service        # for the user who ran sudo; add --user NAME to choose
gpu-game-guard polkit status ollama.service
sudo gpu-game-guard polkit revoke ollama.service
```

`allow` writes `/etc/polkit-1/rules.d/60-gpu-game-guard.rules`. The rule covers only the units listed, only
for the named user, and only starting and stopping (the `org.freedesktop.systemd1.manage-units` action).
`status` needs no privileges. When a system service is refused, the log says to run the `allow` command.
Without polkit installed (`polkitd` on Debian-based systems) there is no rules directory and `allow`
reports that.

## protect

`protect` adds glob patterns (case-insensitive) to the built-in protect list. A protected process or
service is never offered in the settings window or by `gpu-game-guard discover`, and a `process` target
whose match finds a protected process leaves it alone, logging that it did. The settings window refuses to
add a process target that names, or would also match, a protected program.

The built-in list covers display servers and compositors (Xorg, kwin, plasmashell, gnome-shell and others),
audio and session plumbing (pipewire, wireplumber, pulseaudio, systemd, dbus), login managers, Steam and its
helpers (`steam*`, `reaper`, `pressure-vessel*`), terminals and shells, file managers, and the guard itself.
The Detection tab of the settings window shows it in full.

Service targets are not checked against the protect list when they run; it only keeps such units out of
what is offered.

## Per-game rules

`games` maps a game id to a rule:

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | `""` | A label for people reading the file. Informational only; dialogs take the game's name from Steam. |
| `action` | string | `"ask"` | `ask`, `stop` or `ignore`, with the same meanings as `on_launch`. |

A game with a rule uses the rule's `action`. A game without a rule uses `on_launch`. Rules only change what
happens at launch; `after_game` applies to every game.

A rule with no `action` uses `ask`; it does not inherit `on_launch`. A rule written as
`{"name": "Some Game"}` therefore makes that game ask even when `on_launch` is `stop` or `ignore`.

Rules are also created for you: tick **Remember my choice for this game** in the Qt launch dialog and answer
**Stop and launch** (the game gets the rule `stop`) or **Launch anyway** (the rule `ignore`). Cancel is never
remembered. The Qt dialog is the only one with the checkbox, and `remember_choices: false` removes it.

### Finding a game id

A game id is `steam:` followed by Steam's app id.

- From Steam: the number after `/app/` in the game's store page address
  (`https://store.steampowered.com/app/730/` is `steam:730`).
- From the installed files: `steamapps/appmanifest_<id>.acf` in the Steam library folder.
- From the tool: start the game and run `gpu-game-guard check` while its launcher is running. The
  launcher listing shows each launch visible at that moment with its game id and title. This also works for
  non-Steam shortcuts, whose ids are large numbers.
- From the settings window: the Add game dialog of the Games tab lists games the guard has seen, and a bare
  app id such as `730` is read as `steam:730`.

## ignore_containers

Each entry is a shell-style glob (`fnmatch`, case-sensitive) matched against the container name:

| Pattern | Matches |
| --- | --- |
| `backup-*` | `backup-nightly`, `backup-photos` |
| `*-transcoder` | `plex-transcoder` |
| `ollama` | exactly `ollama` |
| `db-?` | `db-1`, `db-a` |

Ignored containers are not counted when deciding whether to intercept a launch, are not shown in the dialog,
and are never stopped. A launch where every GPU container is ignored is not intercepted. Containers already
recorded in the state file as stopped by the guard are still started again later, even if an ignore pattern
matching them was added since.

Containers that map `/dev/dri` only for video decoding (Plex, Jellyfin, Frigate) are detected as GPU
containers; list them here to leave them alone. Detection covers `/dev/nvidia*`, `/dev/kfd`, the `/dev/dri`
directory device and anything under it, a `gpu` device capability, CDI GPU names, and the `nvidia` runtime
unless the container sets `NVIDIA_VISIBLE_DEVICES` to `void` or `none`. Privileged containers are not
detected.

In the Basic page, switching a container off adds its exact name to this list.

## Files the guard writes

Besides `config.json`, these files exist, all small and all safe to delete while the service is stopped. The
four state files are written with mode 0600 and the state directory has mode 0700; existing ones are tightened
to that when the current user owns them.

| File | Location | Holds |
| --- | --- | --- |
| `state.json` | `$XDG_STATE_HOME/gpu-game-guard/` (usually `~/.local/state/gpu-game-guard/`) | what the guard stopped and has not yet started again, so the restart question survives a crash or a reboot. Deleting it, or `gpu-game-guard state clear`, makes the guard forget that anything is stopped. An unreadable file is reported and copied to `state.json.corrupt-<time>` (mode 0600, the newest three kept) before it is replaced. |
| `status.json` | same directory | what the guard is doing; rewritten on every pass and removed when the guard stops cleanly. It counts as current for 15 seconds, or for its `valid_for` (900 seconds) while the guard waits for an answer, for a restart or for a stop or start to finish. It also names the guard's process: when that process is gone, or its pid now belongs to another process, readers say the service is not running. `gpu-game-guard status` and the tray read it. |
| `events.jsonl` | same directory | the last 300 events, one JSON object per line. |
| `pause.json` | same directory | a pause: `{"until": <unix time>}`, or `{"until": null}` until resumed. Created by `gpu-game-guard pause`, the tray and the settings window, removed by `resume`. A pause whose time has passed is ignored. |
| `ui.json` | next to `config.json` | the settings window's mode (`"basic"` or `"advanced"`) and size. |

`status.json` holds `version`, `pid`, `pid_start` (the process's start time in clock ticks since boot), `updated` (unix time), `valid_for` (seconds), `state` (`watching`, `paused`, `disabled`,
`asking`, `game-running` or `restart-pending`), `paused`, `paused_until`, `game` (`id` and `title`),
`stopped` (the refs of what the guard stopped), `config_ok`, `config_error`, `dialog`, `can_ask` and
`targets`. `gpu-game-guard status --json` prints it with `running` added; when there is no recent status it prints only `"running": false` and the pause.

An event has `t` (unix time), `kind` and `message`, and sometimes `game`, `choice` or `action`. The kinds are
`intercept`, `answer`, `stop`, `restart`, `leave`, `remember`, `failure` and `config`. The settings window's
Activity tab shows them.

## How changes are picked up

The running service re-reads the file when its modification time, size or inode changes. Edit and save; no
restart is needed for the values used on each pass: `enabled`, `on_launch`, `after_game`,
`auto_containers`, `remember_choices`, `ignore_containers`, `protect`, `targets`, `games`, the timings and
`heavy_vram_mib`.

Settings that choose what is built at start-up (`runtimes`, `triggers`, `dialog`) are read only when the
service starts; apply a change with `systemctl --user restart gpu-game-guard`. `check` shows a note when
they differ from the defaults, and the settings window shows a banner with a Restart service button.

If the file becomes unreadable or invalid, or briefly disappears during an editor's save, the service keeps
the previous settings and logs the problem once; the tray and `gpu-game-guard status` say that the config has
a problem. `gpu-game-guard run` itself refuses to start on an invalid config (exit 78). Run
`gpu-game-guard config validate` after editing to see what is wrong.

The settings window works on a copy: nothing is written until **Apply** (or Ctrl+S), a single button that
saves every tab, the Raw config tab included (text typed there that is valid counts as an unsaved change, and
text that is not yet valid blocks Apply and is listed). If the file changed on disk while the window was open,
Apply merges the window's edits into the new content; if the file was deleted, it is written again. A file
that cannot be used is left untouched and its text opens in the Raw config tab, or the defaults can replace it,
in which case the old file is kept as `config.json.broken-<time>` when it can be read. Only one settings window
opens at a time. Pause, Resume and Restart service act at once.

## Examples

Each example is a complete file. Anything not listed keeps its default. Check an edit with
`gpu-game-guard config validate`.

### Manage a service

Stop the user service `ollama.service` before a game and start it again afterwards:

```json
{
  "targets": [
    {"id": "ollama", "kind": "service", "label": "Ollama", "unit": "ollama.service", "scope": "user"}
  ]
}
```

For a system service leave `scope` out (it defaults to `"system"`), then run
`sudo gpu-game-guard polkit allow ollama.service` once:

```json
{
  "targets": [
    {"id": "ollama", "kind": "service", "label": "Ollama", "unit": "ollama.service"}
  ]
}
```

`gpu-game-guard presets add ollama-system-service` and `ollama-user-service` add the same targets.

### Unload Ollama models and leave the server running

A command target: `stop` asks the Ollama server to unload a model, `start` has nothing to do because the
server loads models on demand, and `check` succeeds only while a model is loaded, so the guard offers the
target only when it has something to free.

```json
{
  "targets": [
    {
      "id": "ollama-unload",
      "kind": "command",
      "label": "Ollama models",
      "stop": ["curl", "-s", "-d", "{\"model\": \"llama3.2\", \"keep_alive\": 0}", "http://127.0.0.1:11434/api/generate"],
      "start": ["true"],
      "check": ["sh", "-c", "curl -s http://127.0.0.1:11434/api/ps | grep -q '\"model\"'"],
      "timeout_seconds": 30
    }
  ]
}
```

This one names a model. The `ollama-unload` preset (`gpu-game-guard presets add ollama-unload`) is the more
complete version: its commands read the loaded models from the server and unload every one of them, and it
needs only `python3`.

### Pause a program

Ask OBS Studio to close before a game and start it again afterwards:

```json
{
  "targets": [
    {"id": "obs", "kind": "process", "label": "OBS Studio", "match": "obs", "restart": true}
  ]
}
```

Unsaved work in the program can be lost; see [process](#process). To protect other programs from ever being
matched, add `"protect": ["my-editor*"]`.

### Never ask for one game, always stop for another

```json
{
  "games": {
    "steam:730": {"name": "Counter-Strike 2", "action": "ignore"},
    "steam:1091500": {"name": "Cyberpunk 2077", "action": "stop"}
  }
}
```

Add `"after_game": "restart"` to bring everything back without a dialog as well.

### Ignore a backup container

A container that has GPU access but is never worth stopping:

```json
{
  "ignore_containers": ["backup-*"]
}
```

### Force a dialog backend

Use `kdialog` even where the desktop is not detected as KDE:

```json
{
  "dialog": "kdialog"
}
```

Use `none` to never show a dialog. With no dialog the guard cannot ask: for action `ask` the game starts
untouched and nothing is stopped, and with `after_game` `ask` what it stopped is started again. Explicit
`stop` actions (per game or `on_launch`) still stop workloads and start the game with a notification, so
combine `none` with `stop` rules for fully unattended behaviour. Restart the service after changing
`dialog`.

### Only host targets, no containers

```json
{
  "auto_containers": false,
  "runtimes": ["service", "command", "process"],
  "targets": [
    {"id": "ollama", "kind": "service", "label": "Ollama", "unit": "ollama.service", "scope": "user"}
  ]
}
```

### Everything together

```json
{
  "enabled": true,
  "on_launch": "ask",
  "after_game": "ask",
  "auto_containers": true,
  "remember_choices": true,
  "heavy_vram_mib": 4096,
  "ignore_containers": ["backup-*"],
  "protect": ["my-editor*"],
  "runtimes": ["docker", "podman", "service", "command", "process"],
  "triggers": ["steam"],
  "dialog": "auto",
  "targets": [
    {"id": "ollama", "kind": "service", "label": "Ollama", "unit": "ollama.service", "scope": "user"},
    {"id": "obs", "kind": "process", "label": "OBS Studio", "match": "obs", "restart": true}
  ],
  "games": {
    "steam:730": {"name": "Counter-Strike 2", "action": "ignore"},
    "steam:1091500": {"name": "Cyberpunk 2077", "action": "stop"}
  }
}
```
