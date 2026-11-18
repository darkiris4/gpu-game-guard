#!/usr/bin/env bash
# Installs GPU Game Guard for the current user: a single executable goes to ~/.local/bin, a systemd user
# service runs it, and a settings entry, a tray autostart entry and an icon go under the XDG directories.
#
#   install.sh                 install or update, then start the service
#   install.sh --uninstall     stop the service and remove the files (add --purge to drop the saved state)
#   --no-systemd               only copy files; never touch systemd (for packaging and tests)
#
# Environment: GPU_GAME_GUARD_REF (git ref to download, default main), GPU_GAME_GUARD_SRC (a local
# checkout to install from instead of downloading), GPU_GAME_GUARD_BIN_DIR (default ~/.local/bin). The
# entries and the icon follow XDG_DATA_HOME (applications, icons) and XDG_CONFIG_HOME (autostart, systemd).
set -euo pipefail

REPO="darkiris4/gpu-game-guard"
REF="${GPU_GAME_GUARD_REF:-main}"
SRC="${GPU_GAME_GUARD_SRC:-}"
BIN_DIR="${GPU_GAME_GUARD_BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/gpu-game-guard"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="$DATA_HOME/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
NAME="gpu-game-guard"

usage() {
  cat <<'USAGE'
Installs GPU Game Guard for the current user: a single executable goes to ~/.local/bin, a systemd user
service runs it, and a settings entry, a tray autostart entry and an icon go under the XDG directories.

  install.sh                 install or update, then start the service
  install.sh --uninstall     stop the service and remove the files (add --purge to drop the saved state)
  --no-systemd               only copy files; never touch systemd (for packaging and tests)

Environment: GPU_GAME_GUARD_REF (git ref to download, default main), GPU_GAME_GUARD_SRC (a local
checkout to install from instead of downloading), GPU_GAME_GUARD_BIN_DIR (default ~/.local/bin). The
entries and the icon follow XDG_DATA_HOME (applications, icons) and XDG_CONFIG_HOME (autostart, systemd).
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

# Refreshes the desktop and icon caches when the tools exist. They only speed up discovery, so nothing here
# can fail the installation, and systemd-free runs (packaging, tests) leave them alone.
refresh_caches() {
  [ "$systemd" = 1 ] || return 0
  if command -v update-desktop-database >/dev/null; then update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true; fi
  if command -v gtk-update-icon-cache >/dev/null && [ -f "$DATA_HOME/icons/hicolor/index.theme" ]; then
    gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true
  fi
  return 0
}

if [ "$uninstall" = 1 ]; then
  if [ "$systemd" = 1 ] && command -v systemctl >/dev/null; then
    systemctl --user disable --now "$NAME.service" 2>/dev/null || true
  fi
  rm -f "$UNIT_DIR/$NAME.service" "$BIN_DIR/$NAME" "$APP_DIR/$NAME.desktop" "$AUTOSTART_DIR/$NAME-tray.desktop" \
    "$ICON_DIR/$NAME.svg"
  refresh_caches
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
  /*) ;;
  *) die "GPU_GAME_GUARD_BIN_DIR must be an absolute path: systemd needs one in ExecStart" ;;
esac
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
for f in "$NAME.desktop" "$NAME-tray.desktop" "$NAME.svg"; do
  [ -f "$tree/packaging/$f" ] || die "the source has no packaging/$f"
done

bash "$tree/scripts/build-zipapp.sh" "$tmp/$NAME" || die "could not build $NAME"
head -n 1 "$tmp/$NAME" | grep -q '^#!.*python3' || die "the built file does not look like the program"
python3 "$tmp/$NAME" --version >/dev/null || die "the built program does not run"

# Quotes a path for an ExecStart line: double-quoted, with the characters systemd treats specially escaped. The
# program path is not subject to variable expansion, so a dollar sign is left as it is.
unit_quote() {
  local text="$1"
  text="${text//\\/\\\\}"
  text="${text//\"/\\\"}"
  text="${text//%/%%}"
  printf '"%s"' "$text"
}

# Quotes the program path for the Exec key of a desktop entry: double-quoted, with the characters the entry
# format treats specially escaped (each backslash is doubled again for the string level), and a percent sign
# doubled so it is not read as a field code.
desktop_quote() {
  local text="$1"
  text="${text//\\/\\\\\\\\}"
  text="${text//\"/\\\\\"}"
  text="${text//\`/\\\\\`}"
  text="${text//\$/\\\\\$}"
  text="${text//%/%%}"
  printf '"%s"' "$text"
}

# Copies a desktop entry with the program in its Exec line replaced by the installed path; the arguments stay.
write_entry() {  # write_entry SOURCE
  local line rest
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      Exec=*)
        rest="${line#Exec=}"
        case "$rest" in *" "*) rest=" ${rest#* }" ;; *) rest="" ;; esac
        printf 'Exec=%s%s\n' "$(desktop_quote "$BIN_DIR/$NAME")" "$rest" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$1"
}

unit="$UNIT_DIR/$NAME.service"
app_entry="$APP_DIR/$NAME.desktop"
tray_entry="$AUTOSTART_DIR/$NAME-tray.desktop"
icon="$ICON_DIR/$NAME.svg"
# Every installed file, in installation order; its previous content is kept under $backup/<index>.
installed=("$BIN_DIR/$NAME" "$unit" "$app_entry" "$tray_entry" "$icon")
mkdir -p "$BIN_DIR" "$UNIT_DIR" "$APP_DIR" "$AUTOSTART_DIR" "$ICON_DIR"
backup="$tmp/previous"
mkdir "$backup"
had=()
for i in "${!installed[@]}"; do
  had[i]=0
  if [ -e "${installed[$i]}" ] || [ -L "${installed[$i]}" ]; then cp -pP "${installed[$i]}" "$backup/$i" && had[i]=1; fi
done
had_bin="${had[0]}" had_unit="${had[1]}"

# Puts the previous files back, or removes the new ones when nothing was installed before.
restore() {
  local i dest dir base
  for i in "${!installed[@]}"; do
    dest="${installed[$i]}"
    dir="${dest%/*}" base="${dest##*/}"
    if [ "${had[$i]}" = 1 ]; then
      cp -pP "$backup/$i" "$dir/.$base.old.$$" && mv -f "$dir/.$base.old.$$" "$dest"
    else
      rm -f "$dest"
    fi
  done
  if [ "$systemd" = 1 ]; then
    [ "$had_unit" = 1 ] || systemctl --user disable "$NAME.service" >/dev/null 2>&1 || true
    systemctl --user daemon-reload || true
    if [ "$had_bin" = 1 ]; then systemctl --user restart "$NAME.service" || true; fi
  fi
}

# Each file is written under a temporary name in its own directory and renamed into place, so a reader
# never sees a partial file and a symlink at the final name is replaced rather than written through.
cleanup() {
  local dest
  for dest in "${installed[@]}"; do
    rm -f "${dest%/*}/.${dest##*/}.new.$$" "${dest%/*}/.${dest##*/}.old.$$"
  done
  rm -rf "$tmp"
}
trap cleanup EXIT
exec_line="ExecStart=$(unit_quote "$BIN_DIR/$NAME") run"
if ! { install -m 0755 "$tmp/$NAME" "$BIN_DIR/.$NAME.new.$$" && mv -f "$BIN_DIR/.$NAME.new.$$" "$BIN_DIR/$NAME"; }; then
  restore
  die "could not install $BIN_DIR/$NAME; the previous installation was left as it was"
fi
if ! { while IFS= read -r line || [ -n "$line" ]; do
         case "$line" in
           ExecStart=*) printf '%s\n' "$exec_line" ;;
           *) printf '%s\n' "$line" ;;
         esac
       done < "$tree/$NAME.service" > "$UNIT_DIR/.$NAME.service.new.$$" \
       && mv -f "$UNIT_DIR/.$NAME.service.new.$$" "$unit"; }; then
  restore
  die "could not write $unit; the previous installation was restored"
fi
if ! { write_entry "$tree/packaging/$NAME.desktop" > "$APP_DIR/.$NAME.desktop.new.$$" \
       && chmod 0644 "$APP_DIR/.$NAME.desktop.new.$$" && mv -f "$APP_DIR/.$NAME.desktop.new.$$" "$app_entry" \
       && write_entry "$tree/packaging/$NAME-tray.desktop" > "$AUTOSTART_DIR/.$NAME-tray.desktop.new.$$" \
       && chmod 0644 "$AUTOSTART_DIR/.$NAME-tray.desktop.new.$$" \
       && mv -f "$AUTOSTART_DIR/.$NAME-tray.desktop.new.$$" "$tray_entry"; }; then
  restore
  die "could not write the desktop entries; the previous installation was restored"
fi
if ! { install -m 0644 "$tree/packaging/$NAME.svg" "$ICON_DIR/.$NAME.svg.new.$$" \
       && mv -f "$ICON_DIR/.$NAME.svg.new.$$" "$icon"; }; then
  restore
  die "could not install $icon; the previous installation was restored"
fi

if [ "$systemd" = 1 ]; then
  systemctl --user daemon-reload || { restore; die "systemctl --user daemon-reload failed; the previous installation was restored"; }
  systemctl --user enable "$NAME.service" >/dev/null || { restore; die "could not enable $NAME.service; the previous installation was restored"; }
  systemctl --user restart "$NAME.service" || true
  running=0
  for _ in 1 2 3 4 5; do
    sleep 1
    if systemctl --user is-active --quiet "$NAME.service"; then running=1; break; fi
  done
  if [ "$running" != 1 ]; then
    status="$(systemctl --user show -p ExecMainStatus --value "$NAME.service" 2>/dev/null || true)"
    if [ "$status" = 78 ]; then
      # the program refuses to start on an invalid config; the installation itself is fine
      die "installed, but the service did not start because the config is invalid: run '$NAME config validate'"
    fi
    restore
    if [ "$had_bin" = 1 ]; then
      die "the new service did not start; the previous version was restored: journalctl --user -u $NAME"
    fi
    die "the service did not start; the new files were removed: journalctl --user -u $NAME"
  fi
  refresh_caches
  say "GPU Game Guard installed and running"
else
  say "GPU Game Guard files installed (systemd untouched)"
fi
say "  program: $BIN_DIR/$NAME"
say "  service: $UNIT_DIR/$NAME.service"
say "  entries: $app_entry, $tray_entry (the tray starts at the next login)"
say "  config:  $NAME config init   (optional; defaults apply without a file)"
say "  logs:    journalctl --user -u $NAME -f"
