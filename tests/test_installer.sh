#!/usr/bin/env bash
# Tests install.sh against throwaway directories. The real user service and ~/.local/bin are never
# touched: every run uses --no-systemd with GPU_GAME_GUARD_BIN_DIR, XDG_CONFIG_HOME and XDG_STATE_HOME
# pointing into a temporary directory.
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
tools="python3 mktemp rm install cat head grep sed dirname cp find mkdir chmod tar curl env bash sh"

run_installer() {  # run_installer PATH_DIR HOME_DIR args...
  local path_dir="$1" home="$2"; shift 2
  env -i PATH="$path_dir" HOME="$home" XDG_CONFIG_HOME="$home/config" XDG_STATE_HOME="$home/state" \
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
  grep -qxF "ExecStart=$home/bin/gpu-game-guard run" "$unit" 2>/dev/null \
    && pass "unit runs '<bin>/gpu-game-guard run'" || fail "unit runs '<bin>/gpu-game-guard run'" "$(cat "$unit" 2>/dev/null)"

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
  out="$(run_installer "$work/path-docker" "$home" --uninstall --purge 2>&1)"; status=$?
  [ "$status" -eq 0 ] && [ ! -e "$home/state/gpu-game-guard" ] && pass "uninstall --purge drops the state" || fail "uninstall --purge drops the state" "$out"
}

case_missing_runtime
case_missing_python
case_bad_option
case_build_script
case_install_cycle

echo "passed=$passed skipped=$skipped failed=$failures"
[ "$failures" -eq 0 ]
