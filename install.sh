#!/usr/bin/env bash
# Installs GPU Game Guard for the current user: a single executable goes to ~/.local/bin and a systemd
# user service runs it.
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

usage() {
  cat <<'USAGE'
Installs GPU Game Guard for the current user: a single executable goes to ~/.local/bin and a systemd
user service runs it.

  install.sh                 install or update, then start the service
  install.sh --uninstall     stop the service and remove the files (add --purge to drop the saved state)
  --no-systemd               only copy files; never touch systemd (for packaging and tests)

Environment: GPU_GAME_GUARD_REF (git ref to download, default main), GPU_GAME_GUARD_SRC (a local
checkout to install from instead of downloading), GPU_GAME_GUARD_BIN_DIR (default ~/.local/bin).
USAGE
}

uninstall=0 purge=0 systemd=1
for arg in "$@"; do
  case "$arg" in
    --uninstall) uninstall=1 ;;
    --purge) purge=1 ;;
    --no-systemd) systemd=0 ;;
    -h|--help) usage; exit 0 ;;
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
    config_file="${XDG_CONFIG_HOME:-$HOME/.config}/$NAME/config.json"
    [ -e "$config_file" ] && say "kept $config_file: the configuration is never removed"
  elif [ -e "$STATE_DIR/state.json" ]; then
    say "kept $STATE_DIR/state.json: it lists containers that were stopped and not restarted"
  fi
  say "GPU Game Guard removed"
  exit 0
fi

case "$BIN_DIR" in
  *[\"\\]*|*$'\n'*) die "GPU_GAME_GUARD_BIN_DIR must not contain a quote, a backslash or a newline: systemd rejects such a program path" ;;
esac
command -v python3 >/dev/null || die "python3 is required"
command -v docker >/dev/null || command -v podman >/dev/null || die "a container runtime is required (docker or podman)"
command -v zenity >/dev/null || command -v kdialog >/dev/null \
  || say "warning: neither zenity nor kdialog found; without a dialog the guard lets games launch without asking"
command -v steam >/dev/null || command -v flatpak >/dev/null || say "warning: neither steam nor flatpak found; the relaunch needs one of them"
command -v nvidia-smi >/dev/null || say "warning: nvidia-smi not found; GPU memory figures come from the kernel's DRM interface where available"
if [ "$systemd" = 1 ]; then
  command -v systemctl >/dev/null || die "systemd is required (use --no-systemd to only copy the files)"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "$SRC" ]; then
  [ -d "$SRC/gpu_game_guard" ] || die "GPU_GAME_GUARD_SRC is not a checkout: $SRC"
  tree="$SRC"
else
  command -v curl >/dev/null || die "curl is required to download the source"
  command -v tar >/dev/null || die "tar is required to unpack the source"
  mkdir "$tmp/src"
  curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$tmp/src" --strip-components=1 \
    || die "could not download and unpack $REPO ($REF)"
  tree="$tmp/src"
fi
[ -f "$tree/scripts/build-zipapp.sh" ] || die "the source has no scripts/build-zipapp.sh"
[ -f "$tree/$NAME.service" ] || die "the source has no $NAME.service"

bash "$tree/scripts/build-zipapp.sh" "$tmp/$NAME" || die "could not build $NAME"
head -n 1 "$tmp/$NAME" | grep -q '^#!.*python3' || die "the built file does not look like the program"
python3 "$tmp/$NAME" --version >/dev/null || die "the built program does not run"

# Quotes a path for an ExecStart line: double-quoted, with the characters systemd treats specially escaped.
unit_quote() {
  local text="$1"
  text="${text//\\/\\\\}"
  text="${text//\"/\\\"}"
  text="${text//%/%%}"
  text="${text//\$/\$\$}"
  printf '"%s"' "$text"
}

install -d "$BIN_DIR" "$UNIT_DIR"
unit="$UNIT_DIR/$NAME.service"
backup="$tmp/previous"
mkdir "$backup"
had_bin=0 had_unit=0
if [ -e "$BIN_DIR/$NAME" ]; then cp -p "$BIN_DIR/$NAME" "$backup/bin" && had_bin=1; fi
if [ -e "$unit" ] || [ -L "$unit" ]; then cp -P "$unit" "$backup/unit" && had_unit=1; fi

# Each file is written under a temporary name in its own directory and renamed into place, so a reader
# never sees a partial file and a symlink at the final name is replaced rather than written through.
install -m 0755 "$tmp/$NAME" "$BIN_DIR/.$NAME.new.$$"
mv -f "$BIN_DIR/.$NAME.new.$$" "$BIN_DIR/$NAME"
exec_line="ExecStart=$(unit_quote "$BIN_DIR/$NAME") run"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ExecStart=*) printf '%s\n' "$exec_line" ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$tree/$NAME.service" > "$UNIT_DIR/.$NAME.service.new.$$"
mv -f "$UNIT_DIR/.$NAME.service.new.$$" "$unit"

if [ "$systemd" = 1 ]; then
  systemctl --user daemon-reload
  systemctl --user enable "$NAME.service" >/dev/null || die "could not enable $NAME.service: systemctl --user enable failed"
  systemctl --user restart "$NAME.service" || true
  sleep 1
  if ! systemctl --user is-active --quiet "$NAME.service"; then
    if [ "$had_bin" = 1 ]; then
      mv -f "$backup/bin" "$BIN_DIR/.$NAME.old.$$" && mv -f "$BIN_DIR/.$NAME.old.$$" "$BIN_DIR/$NAME"
      if [ "$had_unit" = 1 ]; then
        cp -P "$backup/unit" "$UNIT_DIR/.$NAME.service.old.$$" && mv -f "$UNIT_DIR/.$NAME.service.old.$$" "$unit"
      fi
      systemctl --user daemon-reload || true
      systemctl --user restart "$NAME.service" || true
      die "the new service did not start; the previous version was restored: journalctl --user -u $NAME"
    fi
    die "the service did not start: journalctl --user -u $NAME"
  fi
  say "GPU Game Guard installed and running"
else
  say "GPU Game Guard files installed (systemd untouched)"
fi
say "  program: $BIN_DIR/$NAME"
say "  service: $UNIT_DIR/$NAME.service"
say "  config:  $NAME config init   (optional; defaults apply without a file)"
say "  logs:    journalctl --user -u $NAME -f"
