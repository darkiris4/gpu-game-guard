# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- Host targets next to containers: systemd services (system or user scope), commands (a stop command, a
  start command and an optional running check) and processes (matched by name, ended with SIGTERM, started
  again with the same command line). They are configured under `targets` and offered for stopping before a
  game like containers are. Processes are never killed with SIGKILL.
- A built-in protect list (display servers, compositors, audio, session managers, login managers, Steam,
  terminals, shells, file managers and the guard itself) plus the `protect` key. Protected names are never
  offered and a process target refuses to stop them.
- Discovery of what could be managed (`gpu-game-guard discover`): containers, then services and processes
  that hold GPU memory, safest first, each with the target entry that would manage it.
- Presets for Ollama as a system service, a user service and an unload-only command
  (`gpu-game-guard presets list|add`).
- A polkit rule for system services (`gpu-game-guard polkit allow|revoke|status UNIT...`), limited to the
  named units and user.
- A status channel under the state directory (`status.json`, `events.jsonl`, `pause.json`) and
  `gpu-game-guard status [--json]`.
- Pause and resume: `gpu-game-guard pause [--for 30m|2h|1d]`, `resume`, the tray and the settings window.
  A paused guard lets games start untouched.
- The configuration keys `enabled` (a master switch), `auto_containers`, `remember_choices`, `protect`,
  `targets`, and the `service`, `command` and `process` entries of `runtimes`, with validation of every
  target by index and id.
- "Remember my choice for this game": the Qt launch dialog can save a `stop` or `ignore` rule for the game.
- A Qt launch and restart dialog with a card per workload, shown from a separate process so a failure in Qt
  cannot affect the watcher; chosen automatically when PyQt5 and a display exist, or with `"dialog": "qt"`.
- A tray icon that shows the guard's state and offers Pause for 1 hour, Pause until resumed, Resume, Open
  settings and Quit (`gpu-game-guard tray`, started at login).
- A settings window (`gpu-game-guard settings`) with a Basic page and an Advanced mode of eight tabs
  (Targets, Games, Detection, Timing, Dialogs, Activity, Test run, Raw config). Changes are applied on
  request, and a banner offers to restart the service when a setting that is read only at start-up changed.
- `gpu-game-guard targets list|enable|disable|remove`, and `check` now also lists configured targets and the
  state of the running service.
- A Debian package, built with `scripts/build-deb.sh`, that installs the program, the user unit, the Settings
  and tray entries and the icon, and enables the service for every user.
- The installer also installs the Settings entry, the tray autostart entry and the icon, and removes them on
  `--uninstall`.
- CI jobs for the Qt interface (offscreen) and for the Debian package, including an install and purge in
  Debian 12, Ubuntu 22.04 and Ubuntu 24.04 containers.
- Vendor-neutral GPU detection: NVIDIA through `nvidia-smi`, AMD and Intel through kernel interfaces
  (`/sys/class/drm` and `/proc/<pid>/fdinfo`). The AMD and Intel paths are covered by unit tests with
  fixtures and have not been run on real hardware.
- Podman support alongside Docker, including CDI device names and `libpod` cgroup paths. Covered by
  unit tests with fixtures; not run on real Podman.
- Broader GPU container detection: device requests, the `nvidia` runtime (unless
  `NVIDIA_VISIBLE_DEVICES` is `void` or `none`), `/dev/nvidia*`, `/dev/kfd`, the `/dev/dri` directory
  device and anything under it, CDI devices and CDI annotations. Containers that map `/dev/dri` only for
  video decoding (Plex, Jellyfin, Frigate) are detected; `ignore_containers` is the remedy. Privileged
  containers are not detected.
- Dialog backends: `zenity`, `kdialog`, desktop notifications and none, chosen automatically or with
  the `dialog` setting. Prompters report `can_ask`.
- Configuration file (`~/.config/gpu-game-guard/config.json`) with timings, `on_launch`,
  `after_game`, `ignore_containers`, `runtimes`, `triggers`, `dialog` and per-game rules. Changes are
  picked up while the service runs, detected by modification time, size and inode; the previous settings
  are kept when the file briefly disappears or is invalid. See `docs/configuration.md`.
- Config validation ranges: `poll_seconds` 0.2 to 60, `exit_grace_seconds` 2 to 3600,
  `no_game_timeout_seconds` 30 to 86400, `bypass_seconds` 5 to 3600, `dialog_retry_seconds` 30 to
  86400, `heavy_vram_mib` 0 to 10000000.
- `config validate` reports unknown keys (such as `on_lauch`) with a did-you-mean hint and exits 1;
  loading a config with unknown keys still works and logs a warning to stderr.
- Per-game rules and policies: ask, stop or ignore at launch; ask, restart or leave after the game.
- `check` command that diagnoses the setup, and `config` commands (`path`, `show`, `validate`,
  `init`). `--config PATH` works before or after the subcommand.
- Exit codes: 0 ok; 1 a failed check or invalid config; 2 usage; 78 when `run` refuses to start on an
  invalid config (the service does not restart on 78); 130 on Ctrl-C.
- Package layout with a trigger interface, a runtime interface and a dialog interface, described in
  `docs/architecture.md`.
- Single-file executable built as a zipapp by `scripts/build-zipapp.sh`; the installer installs it.
- Continuous integration on Python 3.9 to 3.13, on the system Python of Debian 12, Ubuntu 22.04 and
  Ubuntu 24.04, for the shell scripts, and for the zipapp build.
- `scripts/test-matrix-docker.sh` to run the tests locally on Python 3.9 to 3.13 in Docker; it exits
  non-zero when every image was skipped.
- `gpu-game-guard state clear`: drops the list of targets the guard stopped and has not started again,
  without starting them, and tells the providers (see `release`). It warns when the service is running, which
  keeps its own copy until restarted.
- Optional `adopt` and `release` hooks on a `Runtime`. `adopt` receives the refs read from the state file when
  the guard starts; `release` receives refs the guard drops without starting them (`after_game` `leave`, a
  restart question answered **Leave stopped**, the no-game timeout with `leave`, `state clear`). The command
  provider uses them to keep its record of check-less targets across a restart.
- A failed restart is retried after `dialog_retry_seconds`, then twice, four times that and so on, never
  longer than an hour (or `dialog_retry_seconds` when that is longer). After five failures in a row to start the
  same targets the guard says so once, stops asking and notifying, keeps them in the state file and retries
  hourly.
- `status.json` records the guard's process start time (`pid_start`) beside its pid, and its `valid_for`
  stays at 900 seconds while a stop or start is running, not only while asking or waiting to restart.
- The guard removes `status.json` when it stops cleanly (SIGTERM, SIGHUP or Ctrl-C).
- An unreadable `state.json` is reported (log, event, notification) and a copy is kept as
  `state.json.corrupt-<time>`, the newest three.
- Process restarts: the command line and working directory are read when the program is stopped, a relative
  first entry is resolved to an absolute path, and the program is started through
  `systemd-run --user --scope` when that is available, so it outlives a restart of the guard.

### Changed

- Config saves keep keys this version does not know, at the top level and inside game rules and targets, and
  go through a locked read-modify-write (`<config>.lock`), so a change made on disk in the meantime is merged
  instead of overwritten. The settings window merges its edits with changes made on disk. Each save keeps one
  rolling `<config>.bak`. A file nested more than 100 levels deep is rejected as invalid.
- Target labels no longer have to be unique; ids still do.
- A process target's `match` must be 3 to 15 characters with at least 3 that are not glob characters, and is
  refused when it matches more than 25 root processes (helpers whose parent is also a match do not count).
- A command target without a `check` counts as stopped after its `stop` command succeeds, until `start` runs
  or the target is released, and the record survives a restart of the guard.
- A process that cannot be restarted (no usable command line, or a bare interpreter such as `python3` with no
  script) is reported as failed, with the reason logged once, rather than started wrongly. Restarted programs
  are watched together, once, for an immediate failure.
- Readers treat a `status.json` whose process is gone, or whose pid now belongs to another process, as "not
  running", however recent the file looks.
- `state.json`, `status.json`, `events.jsonl` and `pause.json` are written with mode 0600 and the state
  directory is 0700; existing ones are tightened when the current user owns them. The `.bak` and
  `.broken-<time>` copies of the config have the mode of the file they copy.
- The launch and restart dialogs name every kind of target ("this container", "these containers or
  services") and show names, images and titles as plain text, with control and bidirectional override
  characters removed.
- The Qt dialog process picks the platform plugin itself: an inherited headless platform is dropped, and a
  Wayland session with none chosen gets `wayland;xcb`, which falls back to X11.
- The theme's small images (combo box arrow, check mark) go to a private directory under the runtime
  directory, or under `<tmp>/gpu-game-guard-<uid>` when that is unusable; without them the controls keep their
  borders and backgrounds.
- The Raw config tab counts as unsaved changes while it holds text that is not yet valid, and there is a single
  Apply for everything. A settings file that cannot be used is left untouched until you replace it; the
  replaced file is kept as a `.broken-<time>` copy when it can be read.
- The automatic dialog choice prefers the Qt dialog when it can be shown, then kdialog or zenity, then
  notifications. `dialog` also accepts `qt`.
- `runtimes` defaults to `["docker", "podman", "service", "command", "process"]`.
- `config validate` also reports unknown keys inside game rules and targets, and checks the targets.
- The state file keeps the optional `scope`, `unit`, `id`, `cmdline` and `cwd` of each stopped target, so
  services and processes can be started again.
- The single script `gpu_game_guard.py` is replaced by the `gpu_game_guard` package.
- The settings that were constants at the top of the script are now configuration keys.
- `kdialog` is no longer required; any one of the dialog backends will do.
- The state file records the runtime of each stopped container. The previous format is still read.
- When no way to ask exists (no dialog tool found, or the notification backend without action
  support), the guard no longer ends the launch: the game starts untouched, nothing is stopped for
  action `ask`, and the log says so once per game. An explicit `stop` still stops the containers and
  starts the game with a notification. With `after_game` `ask` and no way to ask, the containers the
  guard stopped are started again with a notification.
- Dialog buttons: the launch dialog offers **Stop containers and launch**, **Launch anyway** and
  **Cancel**; the restart dialog offers **Restart**, **Ask me later** and **Leave stopped**. Closing a
  window or a failing dialog backend is never a deliberate answer: a failing tool means **Launch anyway**
  at launch and **Ask me later** after the game. Closing a zenity launch dialog means **Launch anyway** and
  closing a kdialog one means **Cancel**; closing the restart dialog means **Ask me later**.
- GPU memory per container is measured (a number; 0 means none held) or unknown. The dialog says "GPU
  memory use unknown" for the latter and never calls a container idle. Units are GiB and MiB. NVIDIA
  container memory is counted through `nvidia-smi` compute processes only. The `drm-total-vram` fdinfo
  key is matched, and KiB, MiB and GiB units are accepted.
- Logs go to stderr; `config show` prints only JSON on stdout.
- A game rule with no `action` uses `ask`; it does not inherit `on_launch`.
- Ignored containers are never stopped, but containers already recorded as stopped by the guard are
  still started again even if an ignore pattern was added since.
- Atomic config and state writes keep the file's existing permissions (0644 by default).
- The launch is ended usually before the game window appears; with a one-second poll, a game that
  shows its window within the first second can be past that stage.
- `heavy_vram_mib` is applied by `cli.LiveConfig` rather than the guard. External command failures
  are logged once per command and subcommand.

### Fixed

- Documentation claimed the game was relaunched or restarted when no dialog was available; it is left
  untouched.
- The CI zipapp job now passes an output path to `scripts/build-zipapp.sh`, and shellcheck runs at
  warning level.
- `scripts/test-matrix-docker.sh` no longer reports success when nothing was tested.
- A launcher's pid and start time are verified before each signal, so a reused pid is never signalled,
  and launchers owned by other users are ignored.
- A game that starts while a trigger's scan is failing is adopted when scanning resumes, not ended.
- A failing state save no longer strands a launch after the containers were stopped; a failed stop is
  reported to the user and the containers that did not stop are not offered for restart.
- GPU memory read from `fdinfo` that cannot cover every process is reported as unknown for containers it
  did not see, never as zero.
- The installer rolls back to the previous version (or removes the new files) when the files cannot be
  written, `systemctl` fails or the service does not start, requires an absolute install directory,
  quotes the unit's `ExecStart`, and leaves an invalid-config start failure (exit status 78) in place.
- Config and state writes follow a symlinked file instead of replacing it.
- A command target without a `check` that was stopped before the guard restarted is no longer taken for
  running when the guard starts again.
- Discovery counts and offers processes the way the process runtime does (root processes only), and reads GPU
  memory of a process listed without a figure as unknown, not zero.
- A settings file deleted while the window was open is written again with the window's settings; a failed
  discovery scan says so on the Basic page instead of "Nothing is using the GPU"; closing the window no longer
  waits for a running scan.
- `config validate` and the Raw editor report a deeply nested value instead of failing.
- `systemctl` failure lines name the actual error and keep the polkit hint whole.
- The Space key can no longer answer the launch dialog in its first 700 ms.

## 0.1.0

First public version.

### Added

- Detection of Steam game launches (native and Flatpak, including non-Steam shortcuts).
- Detection of running Docker containers with GPU access, with per-container GPU memory from
  `nvidia-smi`.
- A `kdialog` prompt to stop those containers and launch the game, launch anyway, or cancel, and a
  second prompt after the game to restart them.
- State kept on disk so the restart question survives a crash or reboot.
- A one-line installer and a systemd user service.
