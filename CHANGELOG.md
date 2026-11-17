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
- Broader GPU container detection: device requests, the `nvidia` runtime, `/dev/nvidia*`, `/dev/kfd`
  and `/dev/dri` device mappings, CDI devices and CDI annotations.
- Dialog backends: `zenity`, `kdialog`, desktop notifications and none, chosen automatically or with
  the `dialog` setting. With no usable dialog the game starts.
- Configuration file (`~/.config/gpu-game-guard/config.json`) with timings, `on_launch`,
  `after_game`, `ignore_containers`, `runtimes`, `triggers`, `dialog` and per-game rules. Changes are
  picked up while the service runs. See `docs/configuration.md`.
- Per-game rules and policies: ask, stop or ignore at launch; ask, restart or leave after the game.
- `check` command that diagnoses the setup, and `config` commands (`path`, `show`, `validate`,
  `init`).
- Package layout with a trigger interface, a runtime interface and a dialog interface, described in
  `docs/architecture.md`.
- Single-file executable built as a zipapp by `scripts/build-zipapp.sh`; the installer installs it.
- Continuous integration on Python 3.9 to 3.13, on the system Python of Debian 12, Ubuntu 22.04 and
  Ubuntu 24.04, for the shell scripts, and for the zipapp build.
- `scripts/test-matrix-docker.sh` to run the tests locally on Python 3.9 to 3.13 in Docker.

### Changed

- The single script `gpu_game_guard.py` is replaced by the `gpu_game_guard` package.
- The settings that were constants at the top of the script are now configuration keys.
- `kdialog` is no longer required; any one of the dialog backends will do.
- The state file records the runtime of each stopped container. The previous format is still read.

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
