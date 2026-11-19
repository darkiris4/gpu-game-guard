#!/usr/bin/env bash
# Tests install.sh against throwaway directories. The real user service and ~/.local/bin are never
# touched: every run uses --no-systemd with GPU_GAME_GUARD_BIN_DIR, XDG_CONFIG_HOME, XDG_DATA_HOME and
# XDG_STATE_HOME pointing into a temporary directory.
#
#   bash tests/test_installer.sh
#
# GPU_GAME_GUARD_TEST_SRC overrides the checkout the installer builds from (default: this repository).
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${GPU_GAME_GUARD_TEST_SRC:-$repo}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0 skipped=0 passed=0
pass() { passed=$((passed + 1)); echo "ok   $1"; }
fail() { failures=$((failures + 1)); echo "FAIL $1"; shift; [ $# -gt 0 ] && printf '     %s\n' "$@"; }
skip() { skipped=$((skipped + 1)); echo "skip $1"; }

# A PATH holding only the named tools (plus whatever links are added later), so a test controls exactly
# which runtimes and dialog tools the installer can see.
make_path() {
  local dir="$1"; shift
  mkdir -p "$dir"
  for tool in "$@"; do
    local found; found="$(command -v "$tool" || true)"
    [ -n "$found" ] && ln -sf "$found" "$dir/$tool"
  done
}
fake_tool() { printf '#!/bin/sh\nexit 0\n' > "$1/$2"; chmod +x "$1/$2"; }
tools="sleep touch python3 mktemp rm mv install cat head grep sed dirname cp find mkdir chmod tar curl env bash sh"

run_installer() {  # run_installer PATH_DIR HOME_DIR args...
  local path_dir="$1" home="$2"; shift 2
  env -i PATH="$path_dir" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_DATA_HOME="$home/data" XDG_STATE_HOME="$home/state" \
      GPU_GAME_GUARD_SRC="$src" GPU_GAME_GUARD_BIN_DIR="$home/bin" \
      "$(command -v bash)" "$repo/install.sh" --no-systemd "$@"
}

# Missing runtime: python3 present, neither docker nor podman.
case_missing_runtime() {
  local home="$work/missing"; mkdir -p "$home"
  make_path "$work/path-none" $tools
  local out status
  out="$(run_installer "$work/path-none" "$home" 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'docker or podman' <<<"$out" && [ ! -e "$home/bin/gpu-game-guard" ]; then
    pass "fails without docker or podman and installs nothing"
  else
    fail "fails without docker or podman and installs nothing" "status=$status" "$out"
  fi
}

case_missing_python() {
  local home="$work/nopython"; mkdir -p "$home"
  make_path "$work/path-nopy" mktemp rm
  local out status
  out="$(run_installer "$work/path-nopy" "$home" 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'python3 is required' <<<"$out"; then
    pass "fails without python3"
  else
    fail "fails without python3" "status=$status" "$out"
  fi
}

case_bad_option() {
  local out status
  out="$(bash "$repo/install.sh" --bogus 2>&1)"; status=$?
  [ "$status" -eq 2 ] && pass "rejects an unknown option with status 2" || fail "rejects an unknown option with status 2" "status=$status"
}

case_build_script() {
  local out="$work/built/gpu-game-guard"
  if [ ! -f "$src/gpu_game_guard/cli.py" ]; then
    skip "build script output runs --version (gpu_game_guard/cli.py does not exist yet)"
    return
  fi
  if bash "$repo/scripts/build-zipapp.sh" "$out" && [ -x "$out" ] && head -n 1 "$out" | grep -q '^#!/usr/bin/env python3' \
     && "$out" --version >/dev/null; then
    pass "build script produces an executable that runs --version"
  else
    fail "build script produces an executable that runs --version"
  fi
}

case_install_cycle() {
  if [ ! -f "$src/gpu_game_guard/cli.py" ]; then
    skip "install, re-install over a symlinked unit, no-dialog warning, uninstall (gpu_game_guard/cli.py does not exist yet)"
    return
  fi
  local home="$work/cycle"; mkdir -p "$home"
  make_path "$work/path-docker" $tools
  fake_tool "$work/path-docker" docker
  local out status

  out="$(run_installer "$work/path-docker" "$home" 2>&1)"; status=$?
  [ "$status" -eq 0 ] && pass "install succeeds with docker and no dialog tool" || fail "install succeeds with docker and no dialog tool" "status=$status" "$out"
  grep -q 'lets games launch without asking' <<<"$out" && pass "warns when no dialog tool exists" || fail "warns when no dialog tool exists" "$out"
  [ -x "$home/bin/gpu-game-guard" ] && pass "program installed and executable" || fail "program installed and executable"
  "$home/bin/gpu-game-guard" --version >/dev/null 2>&1 && pass "installed program runs --version" || fail "installed program runs --version"
  local unit="$home/config/systemd/user/gpu-game-guard.service"
  grep -qxF "ExecStart=\"$home/bin/gpu-game-guard\" run" "$unit" 2>/dev/null \
    && pass "unit runs '<bin>/gpu-game-guard run'" || fail "unit runs '<bin>/gpu-game-guard run'" "$(cat "$unit" 2>/dev/null)"

  local app="$home/data/applications/gpu-game-guard.desktop" tray="$home/config/autostart/gpu-game-guard-tray.desktop"
  local icon="$home/data/icons/hicolor/scalable/apps/gpu-game-guard.svg"
  [ -f "$app" ] && [ -f "$tray" ] && [ -f "$icon" ] && pass "settings entry, tray entry and icon installed under the XDG directories" \
    || fail "settings entry, tray entry and icon installed under the XDG directories"
  grep -qxF "Exec=\"$home/bin/gpu-game-guard\" settings" "$app" 2>/dev/null \
    && pass "settings entry runs '<bin>/gpu-game-guard settings'" || fail "settings entry runs '<bin>/gpu-game-guard settings'" "$(grep Exec "$app" 2>/dev/null)"
  grep -qxF "Exec=\"$home/bin/gpu-game-guard\" tray" "$tray" 2>/dev/null \
    && pass "tray entry runs '<bin>/gpu-game-guard tray'" || fail "tray entry runs '<bin>/gpu-game-guard tray'" "$(grep Exec "$tray" 2>/dev/null)"
  grep -qx 'NoDisplay=true' "$tray" && grep -qx 'X-GNOME-Autostart-enabled=true' "$tray" && ! grep -q '^OnlyShowIn' "$tray" \
    && pass "tray entry is hidden, autostarted and not limited to a desktop" || fail "tray entry is hidden, autostarted and not limited to a desktop"
  cmp -s "$icon" "$repo/packaging/gpu-game-guard.svg" && pass "icon is the packaged SVG" || fail "icon is the packaged SVG"
  if [ "$(stat -c %a "$app" "$tray" "$icon" | sort -u)" = 644 ]; then pass "entries and icon are mode 0644"; else fail "entries and icon are mode 0644"; fi
  if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$app" "$tray" >/dev/null 2>&1 && pass "installed desktop entries validate" || fail "installed desktop entries validate" "$(desktop-file-validate "$app" "$tray" 2>&1)"
  else
    skip "installed desktop entries validate (desktop-file-validate not installed)"
  fi

  # Re-install replaces edited and symlinked entries without writing through them.
  echo "precious" > "$work/precious-entry"
  echo "stale" > "$app"; rm -f "$tray"; ln -s "$work/precious-entry" "$tray"
  out="$(run_installer "$work/path-docker" "$home" 2>&1)"; status=$?
  if [ "$status" -eq 0 ] && [ ! -L "$tray" ] && [ "$(cat "$work/precious-entry")" = precious ] \
     && grep -q '^Name=GPU Game Guard$' "$app" && grep -q '^Exec=' "$tray"; then
    pass "re-install replaces a stale entry and a symlinked entry without writing through"
  else
    fail "re-install replaces a stale entry and a symlinked entry without writing through" "status=$status" "$out"
  fi
  [ -z "$(find "$home" -name '.*.new.*' -o -name '.*.old.*')" ] && pass "re-install leaves no temporary files" \
    || fail "re-install leaves no temporary files" "$(find "$home" -name '.*.new.*' -o -name '.*.old.*')"

  # Re-install over a unit that is a symlink to a file that must stay untouched.
  echo "precious" > "$work/precious"
  rm -f "$unit"; ln -s "$work/precious" "$unit"
  out="$(run_installer "$work/path-docker" "$home" 2>&1)"; status=$?
  if [ "$status" -eq 0 ] && [ ! -L "$unit" ] && [ "$(cat "$work/precious")" = "precious" ] && grep -q '^ExecStart=' "$unit"; then
    pass "re-install replaces a symlinked unit without writing through it"
  else
    fail "re-install replaces a symlinked unit without writing through it" "status=$status" "$out"
  fi

  # A runtime of podman alone is enough, and a dialog tool silences the warning.
  make_path "$work/path-podman" $tools
  fake_tool "$work/path-podman" podman
  fake_tool "$work/path-podman" zenity
  out="$(run_installer "$work/path-podman" "$home" 2>&1)"; status=$?
  if [ "$status" -eq 0 ] && ! grep -q 'without asking' <<<"$out"; then
    pass "podman alone suffices and zenity suppresses the dialog warning"
  else
    fail "podman alone suffices and zenity suppresses the dialog warning" "status=$status" "$out"
  fi

  # Uninstall keeps the state file unless purged.
  mkdir -p "$home/state/gpu-game-guard"; echo '{}' > "$home/state/gpu-game-guard/state.json"
  out="$(run_installer "$work/path-docker" "$home" --uninstall 2>&1)"; status=$?
  if [ "$status" -eq 0 ] && [ ! -e "$home/bin/gpu-game-guard" ] && [ ! -e "$unit" ] && [ -e "$home/state/gpu-game-guard/state.json" ]; then
    pass "uninstall removes program and unit, keeps state"
  else
    fail "uninstall removes program and unit, keeps state" "status=$status" "$out"
  fi
  if [ ! -e "$app" ] && [ ! -e "$tray" ] && [ ! -L "$tray" ] && [ ! -e "$icon" ]; then
    pass "uninstall removes the entries and the icon"
  else
    fail "uninstall removes the entries and the icon"
  fi
  out="$(run_installer "$work/path-docker" "$home" --uninstall --purge 2>&1)"; status=$?
  [ "$status" -eq 0 ] && [ ! -e "$home/state/gpu-game-guard" ] && pass "uninstall --purge drops the state" || fail "uninstall --purge drops the state" "$out"
  mkdir -p "$home/config/gpu-game-guard"; echo '{}' > "$home/config/gpu-game-guard/config.json"
  out="$(run_installer "$work/path-docker" "$home" --uninstall --purge 2>&1)"
  if [ -e "$home/config/gpu-game-guard/config.json" ] && grep -q "kept $home/config/gpu-game-guard/config.json" <<<"$out"; then
    pass "uninstall --purge keeps and reports config.json"
  else
    fail "uninstall --purge keeps and reports config.json" "$out"
  fi
}

# A bin dir holding a space, a percent sign and a dollar sign must come out as one valid, escaped ExecStart.
case_unit_quoting() {
  if [ ! -f "$src/gpu_game_guard/cli.py" ]; then
    skip "unit ExecStart is quoted and escaped"
    return
  fi
  local home="$work/quoting"; mkdir -p "$home"
  make_path "$work/path-quote" $tools
  fake_tool "$work/path-quote" docker
  local bindir="$home/my bin %h \$X"
  local out status
  out="$(env -i PATH="$work/path-quote" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_DATA_HOME="$home/data" XDG_STATE_HOME="$home/state" \
      GPU_GAME_GUARD_SRC="$src" GPU_GAME_GUARD_BIN_DIR="$bindir" \
      "$(command -v bash)" "$repo/install.sh" --no-systemd 2>&1)"; status=$?
  local unit="$home/config/systemd/user/gpu-game-guard.service"
  # a dollar sign stays as it is: the program path is not subject to variable expansion in systemd
  local expected="ExecStart=\"$home/my bin %%h \$X/gpu-game-guard\" run"
  if [ "$status" -eq 0 ] && grep -qxF "$expected" "$unit" 2>/dev/null; then
    pass "unit ExecStart is quoted and escaped for spaces, %, \$ and quotes"
  else
    fail "unit ExecStart is quoted and escaped for spaces, %, \$ and quotes" "status=$status" "want: $expected" "$(grep ExecStart "$unit" 2>/dev/null)" "$out"
  fi
  # the desktop entry format needs its own quoting: a dollar sign is escaped, twice over, and a percent sign doubled
  local app="$home/data/applications/gpu-game-guard.desktop"
  local expected_exec="Exec=\"$home/my bin %%h \\\\\$X/gpu-game-guard\" settings"
  if grep -qxF "$expected_exec" "$app" 2>/dev/null; then
    pass "desktop entry Exec is quoted and escaped for spaces, % and \$"
  else
    fail "desktop entry Exec is quoted and escaped for spaces, % and \$" "want: $expected_exec" "$(grep Exec "$app" 2>/dev/null)"
  fi
  if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$app" >/dev/null 2>&1 && pass "the quoted desktop entry validates" \
      || fail "the quoted desktop entry validates" "$(desktop-file-validate "$app" 2>&1)"
  fi
  # systemd rejects quotes and backslashes in a program path, so the installer refuses them up front.
  out="$(env -i PATH="$work/path-quote" HOME="$home" XDG_CONFIG_HOME="$home/config" GPU_GAME_GUARD_SRC="$src" \
      GPU_GAME_GUARD_BIN_DIR="$home/bad\"dir" "$(command -v bash)" "$repo/install.sh" --no-systemd 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'must not contain' <<<"$out" && [ ! -e "$home/bad\"dir" ]; then
    pass "a bin dir with a quote is refused before anything is written"
  else
    fail "a bin dir with a quote is refused before anything is written" "status=$status" "$out"
  fi
  grep -q '^RestartPreventExitStatus=78$' "$unit" 2>/dev/null && pass "generated unit prevents restarts on exit status 78" \
    || fail "generated unit prevents restarts on exit status 78"
  if command -v systemd-analyze >/dev/null; then
    # Verified on a copy inside the temporary directory; the real systemd directories are not involved.
    local copy="$work/verify"; mkdir -p "$copy"; cp "$unit" "$copy/gpu-game-guard.service"
    local verdict; verdict="$(systemd-analyze --user verify "$copy/gpu-game-guard.service" 2>&1)"
    if grep -qiE 'special characters|invalid|fatal|bad unit' <<<"$verdict"; then
      fail "systemd-analyze accepts the generated unit" "$verdict"
    else
      pass "systemd-analyze accepts the generated unit"
    fi
  else
    skip "systemd-analyze verify (systemd-analyze not installed)"
  fi
}

case_help_and_noexec() {
  local out status
  out="$(bash -s -- --help < "$repo/install.sh" 2>&1)"; status=$?
  [ "$status" -eq 0 ] && grep -q 'install.sh --uninstall' <<<"$out" \
    && pass "install.sh --help works when read from stdin" || fail "install.sh --help works when read from stdin" "status=$status" "$out"
  out="$(bash "$repo/scripts/build-zipapp.sh" --help 2>&1)"; status=$?
  if [ "$status" -eq 0 ] && grep -q 'OUTPUT_PATH' <<<"$out" && ! grep -q 'set -euo' <<<"$out"; then
    pass "build-zipapp.sh --help prints only the usage"
  else
    fail "build-zipapp.sh --help prints only the usage" "status=$status" "$out"
  fi
  [ -f "$src/gpu_game_guard/cli.py" ] || return 0
  local a="$work/repro/a" b="$work/repro/b"
  if bash "$repo/scripts/build-zipapp.sh" "$a" && sleep 2 && bash "$repo/scripts/build-zipapp.sh" "$b" && cmp -s "$a" "$b"; then
    pass "two builds of the same tree are byte-identical"
  else
    fail "two builds of the same tree are byte-identical"
  fi
}

# With a stand-in systemctl whose service never becomes active, the previous program and unit come back.
case_rollback() {
  if [ ! -f "$src/gpu_game_guard/cli.py" ]; then
    skip "a service that fails to start restores the previous program"
    return
  fi
  local home="$work/rollback"; mkdir -p "$home/bin" "$home/config/systemd/user"
  make_path "$work/path-rb" $tools sleep
  fake_tool "$work/path-rb" docker
  printf '#!/bin/sh\ncase "$*" in *is-active*) exit 3 ;; esac\nexit 0\n' > "$work/path-rb/systemctl"; chmod +x "$work/path-rb/systemctl"
  printf '#!/bin/sh\necho previous\n' > "$home/bin/gpu-game-guard"; chmod +x "$home/bin/gpu-game-guard"
  echo "previous unit" > "$home/config/systemd/user/gpu-game-guard.service"
  mkdir -p "$home/data/applications" "$home/config/autostart"
  echo "previous entry" > "$home/data/applications/gpu-game-guard.desktop"
  local out status
  out="$(env -i PATH="$work/path-rb" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_STATE_HOME="$home/state" \
      GPU_GAME_GUARD_SRC="$src" GPU_GAME_GUARD_BIN_DIR="$home/bin" "$(command -v bash)" "$repo/install.sh" 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'previous version was restored' <<<"$out" \
     && [ "$("$home/bin/gpu-game-guard" --version)" = previous ] \
     && [ "$(cat "$home/config/systemd/user/gpu-game-guard.service")" = "previous unit" ] \
     && [ "$(cat "$home/data/applications/gpu-game-guard.desktop")" = "previous entry" ] \
     && [ ! -e "$home/config/autostart/gpu-game-guard-tray.desktop" ] \
     && [ ! -e "$home/data/icons/hicolor/scalable/apps/gpu-game-guard.svg" ] \
     && [ -z "$(find "$home" -name '.*.new.*' -o -name '.*.old.*')" ]; then
    pass "a service that fails to start restores the previous program, unit and entries"
  else
    fail "a service that fails to start restores the previous program, unit and entries" "status=$status" "$out"
  fi
}

# More failure paths with a stand-in systemctl: enable failing, a first install that fails to start, a config error.
case_transactions() {
  if [ ! -f "$src/gpu_game_guard/cli.py" ]; then
    skip "installer failure paths"
    return
  fi
  local out status home
  # enable fails: the previous installation is put back
  home="$work/tx-enable"; mkdir -p "$home/bin" "$home/config/systemd/user"
  make_path "$work/path-tx1" $tools sleep
  fake_tool "$work/path-tx1" docker
  printf '#!/bin/sh\ncase "$*" in *enable*) exit 1 ;; esac\nexit 0\n' > "$work/path-tx1/systemctl"; chmod +x "$work/path-tx1/systemctl"
  printf '#!/bin/sh\necho previous\n' > "$home/bin/gpu-game-guard"; chmod +x "$home/bin/gpu-game-guard"
  echo "previous unit" > "$home/config/systemd/user/gpu-game-guard.service"
  out="$(env -i PATH="$work/path-tx1" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_STATE_HOME="$home/state" \
      GPU_GAME_GUARD_SRC="$src" GPU_GAME_GUARD_BIN_DIR="$home/bin" "$(command -v bash)" "$repo/install.sh" 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && [ "$("$home/bin/gpu-game-guard" --version)" = previous ] \
     && [ "$(cat "$home/config/systemd/user/gpu-game-guard.service")" = "previous unit" ]; then
    pass "a failing 'systemctl enable' restores the previous program and unit"
  else
    fail "a failing 'systemctl enable' restores the previous program and unit" "status=$status" "$out"
  fi
  # a first install whose service does not start leaves nothing behind
  home="$work/tx-fresh"; mkdir -p "$home"
  make_path "$work/path-tx2" $tools sleep
  fake_tool "$work/path-tx2" docker
  printf '#!/bin/sh\ncase "$*" in *is-active*) exit 3 ;; esac\nexit 0\n' > "$work/path-tx2/systemctl"; chmod +x "$work/path-tx2/systemctl"
  out="$(env -i PATH="$work/path-tx2" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_STATE_HOME="$home/state" \
      GPU_GAME_GUARD_SRC="$src" GPU_GAME_GUARD_BIN_DIR="$home/bin" "$(command -v bash)" "$repo/install.sh" 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'new files were removed' <<<"$out" && [ ! -e "$home/bin/gpu-game-guard" ] \
     && [ ! -e "$home/config/systemd/user/gpu-game-guard.service" ] \
     && [ ! -e "$home/data/applications/gpu-game-guard.desktop" ] && [ ! -e "$home/config/autostart/gpu-game-guard-tray.desktop" ] \
     && [ ! -e "$home/data/icons/hicolor/scalable/apps/gpu-game-guard.svg" ]; then
    pass "a first install whose service does not start removes the new files"
  else
    fail "a first install whose service does not start removes the new files" "status=$status" "$out"
  fi
  # exit status 78 means an invalid config: the installation stays and the message says why
  home="$work/tx-config"; mkdir -p "$home"
  make_path "$work/path-tx3" $tools sleep
  fake_tool "$work/path-tx3" docker
  printf '#!/bin/sh\ncase "$*" in *is-active*) exit 3 ;; *ExecMainStatus*) echo 78 ;; esac\nexit 0\n' > "$work/path-tx3/systemctl"; chmod +x "$work/path-tx3/systemctl"
  out="$(env -i PATH="$work/path-tx3" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_STATE_HOME="$home/state" \
      GPU_GAME_GUARD_SRC="$src" GPU_GAME_GUARD_BIN_DIR="$home/bin" "$(command -v bash)" "$repo/install.sh" 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'config is invalid' <<<"$out" && [ -x "$home/bin/gpu-game-guard" ]; then
    pass "a service that exits 78 keeps the installation and names the invalid config"
  else
    fail "a service that exits 78 keeps the installation and names the invalid config" "status=$status" "$out"
  fi
  # a relative bin dir is refused
  out="$(env -i PATH="$work/path-tx3" HOME="$home" XDG_CONFIG_HOME="$home/config" GPU_GAME_GUARD_SRC="$src" \
      GPU_GAME_GUARD_BIN_DIR="relative/bin" "$(command -v bash)" "$repo/install.sh" --no-systemd 2>&1)"; status=$?
  if [ "$status" -ne 0 ] && grep -q 'absolute path' <<<"$out"; then
    pass "a relative bin dir is refused"
  else
    fail "a relative bin dir is refused" "status=$status" "$out"
  fi
}

# An applications directory that cannot be written makes the entry step fail: nothing stays installed.
case_entry_failure() {
  if [ ! -f "$src/gpu_game_guard/cli.py" ]; then
    skip "a failing entry write rolls the whole installation back"
    return
  fi
  if [ "$(id -u)" = 0 ]; then
    skip "a failing entry write rolls the whole installation back (root ignores directory permissions)"
    return
  fi
  local home="$work/entryfail"; mkdir -p "$home/data/applications"
  chmod 555 "$home/data/applications"
  make_path "$work/path-ef" $tools
  fake_tool "$work/path-ef" docker
  local out status
  out="$(run_installer "$work/path-ef" "$home" 2>&1)"; status=$?
  chmod 755 "$home/data/applications"
  if [ "$status" -ne 0 ] && grep -q 'desktop entries' <<<"$out" && [ ! -e "$home/bin/gpu-game-guard" ] \
     && [ ! -e "$home/config/systemd/user/gpu-game-guard.service" ] && [ ! -e "$home/config/autostart/gpu-game-guard-tray.desktop" ] \
     && [ -z "$(find "$home" -type f)" ]; then
    pass "a failing entry write rolls the whole installation back"
  else
    fail "a failing entry write rolls the whole installation back" "status=$status" "$out" "$(find "$home" -type f)"
  fi
}

case_missing_runtime
case_entry_failure
case_rollback
case_transactions
case_missing_python
case_unit_quoting
case_help_and_noexec
case_bad_option
case_build_script
case_install_cycle

echo "passed=$passed skipped=$skipped failed=$failures"
[ "$failures" -eq 0 ]
