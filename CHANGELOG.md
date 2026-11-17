# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

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
- Config validation ranges: `poll_seconds` 0.2 to 3600, `exit_grace_seconds` 2 to 3600,
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

### Changed

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
  window or a failing dialog backend is never a deliberate answer: closing the launch dialog means
  **Launch anyway**, closing the restart dialog means **Ask me later**.
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
