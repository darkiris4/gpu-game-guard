# GPU Game Guard

[![CI](https://github.com/darkiris4/gpu-game-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/darkiris4/gpu-game-guard/actions/workflows/ci.yml)

Stops GPU-hungry Docker containers from quietly wrecking your game. When you start a Steam game
while containers that use your GPU are running (a local LLM, speech-to-text, a transcoder), GPU
Game Guard pauses the launch, shows what is holding the GPU, and offers to stop those containers
and start the game for you. When the game closes it offers to bring the containers back.

![The GPU Game Guard dialog](docs/dialog.png)

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/darkiris4/gpu-game-guard/main/install.sh | bash
```

That installs one script to `~/.local/bin` and starts a systemd user service. Nothing runs as
root. To try it:

1. Start a container that uses the GPU, for example `docker start ollama`.
2. Launch any game from Steam.
3. Choose **Stop containers and launch**, play, then quit. A second dialog offers to restart the
   containers it stopped.

Prefer to read before running? Download [`install.sh`](install.sh), look at it, then run it.

Update by running the same command again. Remove with:

```sh
curl -fsSL https://raw.githubusercontent.com/darkiris4/gpu-game-guard/main/install.sh | bash -s -- --uninstall
```

## What it does

- Watches for Steam's per-game launcher process (`reaper SteamLaunch AppId=<id>`), for native and
  Flatpak Steam, including non-Steam shortcuts. It polls once a second.
- If no container with GPU access is running, it does nothing and shows nothing.
- Otherwise it ends the launch before the game window exists and shows a dialog with three choices:
  - **Stop containers and launch** stops them (in parallel), remembers which, and starts the game
    again through `steam://rungameid/`.
  - **Launch anyway** starts the game again and leaves the containers running.
  - **Cancel** does nothing further.
- When the game has been closed for about 8 seconds, it asks whether to start the containers it
  stopped. Answering **Leave stopped** forgets them.
- The list of stopped containers is kept in `~/.local/state/gpu-game-guard/state.json` until you
  answer, so the question survives a crash or a reboot. A container stopped with `docker stop`
  stays stopped across reboots even with `restart: unless-stopped`.
- Launchers that are already running when the service starts are left alone, so restarting the
  service in the middle of a game cannot interrupt it.

The dialog names the game (from Steam's manifests or the shortcut's name), lists each container
with its image and the GPU memory its processes hold, and shows overall GPU memory use. Its
warning is graded: an idle container is reported as such, and the stutter warning appears only
when the containers together hold 4 GB or more.

## Which containers count

A running container counts as a GPU container when its configuration requests the `gpu` device
capability (`--gpus ...`, or `deploy.resources.reservations.devices` in Compose) or uses the
`nvidia` runtime. Nothing is listed by name, so new containers are picked up automatically.

It judges configuration, not activity: an idle Ollama with no model loaded still counts, and a
container that reaches the GPU another way (for example by mounting `/dev/nvidia*` while
privileged) is not detected. GPU memory per container comes from `nvidia-smi` and the process's
cgroup; without `nvidia-smi` the dialog simply omits the figures.

## Requirements

- Linux with systemd and a graphical session, with `python3`, `docker` and `kdialog` (KDE Plasma).
- Steam, native or Flatpak.
- An NVIDIA GPU for the memory figures. The container check itself needs no GPU tooling.

Developed and tested on Pop!_OS 24.04 with KDE Plasma on X11 and native Steam. Wayland, other
desktops (the dialogs need `kdialog`) and AMD or Intel GPUs are not tested.

## Limits

- It only sees Steam launches. A game started from inside another launcher (for example a game
  started from Battle.net) is not a separate Steam launch; the guard asks when the launcher itself
  starts.
- It checks when a launch begins. A GPU container you start during a game is not stopped.
- Ending a launch in its first second can interrupt the setup of a brand-new Proton prefix, which
  only matters the first time a game runs. Steam's "Verify integrity of game files" repairs it.
- If `kdialog` cannot be run, the guard lets the game start rather than leaving it dead.

## Troubleshooting

```sh
systemctl --user status gpu-game-guard
journalctl --user -u gpu-game-guard -f
```

The log shows each intercepted launch, the containers involved and the answer given. Nothing in
the log after you start a game means no GPU container was running at that moment.

## Settings

The few tunables are constants at the top of `gpu_game_guard.py`: `POLL_SECONDS`,
`EXIT_GRACE_SECONDS`, `NO_GAME_TIMEOUT_SECONDS`, `BYPASS_SECONDS` and `HEAVY_VRAM_MIB`. Edit
`~/.local/bin/gpu-game-guard` and run `systemctl --user restart gpu-game-guard`; reinstalling
overwrites the edit.

## Development

```sh
git clone https://github.com/darkiris4/gpu-game-guard
cd gpu-game-guard
python3 -m unittest -v
GPU_GAME_GUARD_SRC=$PWD ./install.sh        # install from the checkout
```

The standard library is the only dependency. The tests cover process detection, container and
cgroup parsing, game-name lookup, message text and every dialog flow with fakes for Docker, the
dialogs and Steam.

## License

[MIT](LICENSE)
