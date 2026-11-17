#!/usr/bin/env bash
# Installs GPU Game Guard for the current user: the script goes to ~/.local/bin and a systemd user
# service watches for Steam game launches.
#
#   install.sh                 install or update, then start the service
#   install.sh --uninstall     stop the service and remove the files (add --purge to drop the saved state)
#   --no-systemd               only copy files; never touch systemd (for packaging and tests)
#
# Environment: GPU_GAME_GUARD_REF (git ref to download, default main), GPU_GAME_GUARD_SRC (a local
# checkout to install from instead of downloading), GPU_GAME_GUARD_BIN_DIR (default ~/.local/bin).
set -euo pipefail

REPO="darkiris4/gpu-game-guard"
REF="${GPU_GAME_GUARD_REF:-main}"
SRC="${GPU_GAME_GUARD_SRC:-}"
BIN_DIR="${GPU_GAME_GUARD_BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/gpu-game-guard"
NAME="gpu-game-guard"

uninstall=0 purge=0 systemd=1
for arg in "$@"; do
  case "$arg" in
    --uninstall) uninstall=1 ;;
    --purge) purge=1 ;;
    --no-systemd) systemd=0 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "$uninstall" = 1 ]; then
  if [ "$systemd" = 1 ] && command -v systemctl >/dev/null; then
    systemctl --user disable --now "$NAME.service" 2>/dev/null || true
  fi
  rm -f "$UNIT_DIR/$NAME.service" "$BIN_DIR/$NAME"
  [ "$systemd" = 1 ] && command -v systemctl >/dev/null && systemctl --user daemon-reload 2>/dev/null || true
  if [ "$purge" = 1 ]; then
    rm -rf "$STATE_DIR"
  elif [ -e "$STATE_DIR/state.json" ]; then
    say "kept $STATE_DIR/state.json: it lists containers that were stopped and not restarted"
  fi
  say "GPU Game Guard removed"
  exit 0
fi

command -v python3 >/dev/null || die "python3 is required"
command -v docker >/dev/null || die "docker is required"
command -v kdialog >/dev/null || die "kdialog is required for the dialogs (KDE Plasma; package: kdialog)"
command -v steam >/dev/null || command -v flatpak >/dev/null || say "warning: neither steam nor flatpak found; the relaunch needs one of them"
command -v nvidia-smi >/dev/null || say "warning: nvidia-smi not found; the dialog will not show GPU memory figures"
if [ "$systemd" = 1 ]; then
  command -v systemctl >/dev/null || die "systemd is required (use --no-systemd to only copy the files)"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "$SRC" ]; then
  cp "$SRC/gpu_game_guard.py" "$tmp/$NAME"
else
  command -v curl >/dev/null || die "curl is required to download the script"
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/gpu_game_guard.py" -o "$tmp/$NAME" \
    || die "could not download gpu_game_guard.py from $REPO ($REF)"
fi
head -n 1 "$tmp/$NAME" | grep -q '^#!.*python3' || die "the downloaded file does not look like the script"
python3 -m py_compile "$tmp/$NAME" || die "the downloaded script does not compile"

install -d "$BIN_DIR" "$UNIT_DIR"
install -m 0755 "$tmp/$NAME" "$BIN_DIR/$NAME"
rm -f "$UNIT_DIR/$NAME.service"  # replace a symlink instead of writing through it
cat > "$UNIT_DIR/$NAME.service" <<UNIT
[Unit]
Description=Ask before a Steam game starts while GPU containers are running
After=graphical-session.target
PartOf=graphical-session.target

[Service]
ExecStart=$BIN_DIR/$NAME
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT

if [ "$systemd" = 1 ]; then
  systemctl --user daemon-reload
  systemctl --user enable "$NAME.service" >/dev/null 2>&1
  systemctl --user restart "$NAME.service"
  sleep 1
  systemctl --user is-active --quiet "$NAME.service" || die "the service did not start: journalctl --user -u $NAME"
  say "GPU Game Guard installed and running"
else
  say "GPU Game Guard files installed (systemd untouched)"
fi
say "  script:  $BIN_DIR/$NAME"
say "  service: $UNIT_DIR/$NAME.service"
say "  logs:    journalctl --user -u $NAME -f"
