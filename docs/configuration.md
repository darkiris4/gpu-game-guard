# Configuration

GPU Game Guard works without a configuration file. Every key below is optional; a missing key takes
its default. The file is read as JSON.

## Location

```
$XDG_CONFIG_HOME/gpu-game-guard/config.json        (usually ~/.config/gpu-game-guard/config.json)
```

`gpu-game-guard config path` prints the exact path in use, and `--config PATH` selects another file
for one invocation. A missing file means all defaults.

| Command | Effect |
| --- | --- |
| `gpu-game-guard config path` | print the path |
| `gpu-game-guard config init` | write the defaults to the path; refuses to overwrite an existing file |
| `gpu-game-guard config show` | print the effective configuration as JSON |
| `gpu-game-guard config validate` | report problems; exit status 1 when the file is invalid |
| `gpu-game-guard check` | diagnose the whole setup, including whether the config file is valid |

Unknown keys are ignored with a warning in the log. A wrong type or a value outside the allowed set
makes the file invalid.

## Keys

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `on_launch` | string | `"ask"` | What to do when a game launches while GPU containers run: `ask`, `stop` or `ignore`. |
| `after_game` | string | `"ask"` | What to do once the game has closed: `ask`, `restart` or `leave`. |
| `poll_seconds` | number | `1.0` | Interval between checks for new launches. |
| `exit_grace_seconds` | number | `8.0` | How long the game must be absent before it counts as closed. Steam's launcher processes briefly disappear between stages of a launch and an exit. |
| `no_game_timeout_seconds` | number | `180.0` | Containers were stopped but the relaunched game never appeared: after this long, the after-game action runs anyway. |
| `bypass_seconds` | number | `120.0` | After the guard relaunches a game, that game is let through for this long so the relaunch is not intercepted again. |
| `dialog_retry_seconds` | number | `300.0` | When the restart question could not be shown, wait this long before asking again. |
| `heavy_vram_mib` | integer | `4096` | GPU memory (MiB) held by the containers together at which the dialog's warning changes from tentative to heavy. |
| `ignore_containers` | list of strings | `[]` | Glob patterns on container names. Matching containers are never counted, stopped or restarted. |
| `runtimes` | list of strings | `["docker", "podman"]` | Container runtimes to use, in order of preference. A runtime whose command is missing is skipped. |
| `triggers` | list of strings | `["steam"]` | Launch detectors to enable. Only `steam` exists. |
| `dialog` | string | `"auto"` | Dialog backend: `auto`, `zenity`, `kdialog`, `notify` or `none`. |
| `games` | object | `{}` | Per-game rules, keyed by game id. |

### on_launch

- `ask`: end the launch, show the dialog, act on the answer (**Stop containers and launch**,
  **Launch anyway**, **Cancel**).
- `stop`: end the launch, stop the GPU containers without asking, start the game again, and show a
  notification naming what was stopped.
- `ignore`: do nothing at all. The launch is not interrupted and no dialog appears.

### after_game

- `ask`: show a dialog offering to start the containers that were stopped. **Leave stopped** forgets
  them. If no dialog can be shown, the question is retried after `dialog_retry_seconds` and the
  list of stopped containers is kept.
- `restart`: start them without asking, and show a notification.
- `leave`: forget them without asking. They stay stopped.

The list of stopped containers lives in `$XDG_STATE_HOME/gpu-game-guard/state.json`
(usually `~/.local/state/gpu-game-guard/state.json`) until it has been dealt with, so it survives a
crash or a reboot.

## Per-game rules

`games` maps a game id to a rule:

```json
"games": {
  "steam:730": {"name": "Counter-Strike 2", "action": "ignore"}
}
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | `""` | A label for people reading the file. Informational only; dialogs take the game's name from Steam. |
| `action` | string | `"ask"` | `ask`, `stop` or `ignore`, with the same meanings as `on_launch`. |

A game with a rule uses the rule's `action`. A game without one uses `on_launch`. Rules only
change what happens at launch; `after_game` applies to every game.

Because a rule's default action is `ask`, a rule written as `{"name": "Some Game"}` makes that game
ask even when `on_launch` is `stop` or `ignore`.

### Finding a game id

A game id is `steam:` followed by Steam's app id.

- From Steam: the number after `/app/` in the game's store page address
  (`https://store.steampowered.com/app/730/` is `steam:730`).
- From the installed files: `steamapps/appmanifest_<id>.acf` in the Steam library folder.
- From the tool: start the game and run `gpu-game-guard check` while its launcher is running. The
  launcher listing shows each launch visible at that moment with its game id and title. This also
  works for non-Steam shortcuts, whose ids are large numbers.

## ignore_containers

Each entry is a shell-style glob (`fnmatch`) matched against the container name:

| Pattern | Matches |
| --- | --- |
| `backup-*` | `backup-nightly`, `backup-photos` |
| `*-transcoder` | `plex-transcoder` |
| `ollama` | exactly `ollama` |
| `db-?` | `db-1`, `db-a` |

Ignored containers are not counted when deciding whether to intercept a launch, are not shown in the
dialog, and are never stopped or restarted. A launch where every GPU container is ignored is not
intercepted.

## How changes are picked up

The running service re-reads the file when its modification time changes. Edit and save; no restart
is needed for the values the watcher uses on each pass (timings, `on_launch`, `after_game`,
`games`, `ignore_containers`, `heavy_vram_mib`). Settings that choose the backends built at start-up
(`runtimes`, `triggers`, `dialog`) are safest applied with
`systemctl --user restart gpu-game-guard`.

If the file becomes unreadable or invalid, the service keeps the last good configuration and logs
the problem once. Run `gpu-game-guard config validate` after editing to see what is wrong.

## Examples

### Never ask for one game

A competitive game where you never want an interruption:

```json
{
  "games": {
    "steam:730": {"name": "Counter-Strike 2", "action": "ignore"}
  }
}
```

### Always stop for another

A demanding game where the answer is always the same, with everything else still asking:

```json
{
  "games": {
    "steam:1091500": {"name": "Cyberpunk 2077", "action": "stop"}
  }
}
```

Add `"after_game": "restart"` to bring the containers back without a dialog as well.

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

Use `none` to never show a dialog. With no dialog, games start untouched and nothing is asked;
combine it with per-game `stop` rules for fully unattended behaviour.

### Everything together

```json
{
  "on_launch": "ask",
  "after_game": "ask",
  "heavy_vram_mib": 4096,
  "ignore_containers": ["backup-*"],
  "runtimes": ["docker", "podman"],
  "triggers": ["steam"],
  "dialog": "auto",
  "games": {
    "steam:730": {"name": "Counter-Strike 2", "action": "ignore"},
    "steam:1091500": {"name": "Cyberpunk 2077", "action": "stop"}
  }
}
```
