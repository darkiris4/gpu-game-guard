#!/usr/bin/env bash
# Tests scripts/build-deb.sh: the package metadata, the file list with permissions, byte-for-byte
# reproducibility, the maintainer scripts, and (when Docker and the images are available) an install and a
# purge in throwaway containers. Nothing is installed on the host: the package is only inspected with
# dpkg-deb and mounted read-only into containers.
#
#   bash tests/test_deb.sh
#
# Environment: GPU_GAME_GUARD_DEB_IMAGES (space separated images for the container checks, default
# "debian:12 ubuntu:24.04"; images that are not present locally are pulled).
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0 skipped=0 passed=0
pass() { passed=$((passed + 1)); echo "ok   $1"; }
fail() { failures=$((failures + 1)); echo "FAIL $1"; shift; [ $# -gt 0 ] && printf '     %s\n' "$@"; }
skip() { skipped=$((skipped + 1)); echo "skip $1"; }

if ! command -v dpkg-deb >/dev/null; then
  skip "all package checks (dpkg-deb is not installed)"
  echo "passed=$passed skipped=$skipped failed=$failures"
  exit 0
fi

version="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$repo/gpu_game_guard/__init__.py" | head -n 1)"
deb="$work/a/gpu-game-guard_${version}_all.deb"

if out="$(bash "$repo/scripts/build-deb.sh" "$work/a" 2>&1)" && [ -f "$deb" ]; then
  pass "build-deb.sh builds gpu-game-guard_${version}_all.deb"
else
  fail "build-deb.sh builds gpu-game-guard_${version}_all.deb" "$out"
  echo "passed=$passed skipped=$skipped failed=$failures"
  exit 1
fi

# control fields
control="$(dpkg-deb -f "$deb")"
check_field() {  # check_field NAME EXPECTED
  local got; got="$(dpkg-deb -f "$deb" "$1")"
  [ "$got" = "$2" ] && pass "control $1" || fail "control $1" "want: $2" "got:  $got"
}
check_field Package gpu-game-guard
check_field Version "$version"
check_field Architecture all
check_field Section games
check_field Priority optional
check_field Depends "python3 (>= 3.9)"
check_field Recommends "python3-pyqt5, zenity | kdialog | libnotify-bin, docker.io | podman"
check_field Suggests "polkitd | policykit-1"
check_field Homepage "https://github.com/darkiris4/gpu-game-guard"
case "$(dpkg-deb -f "$deb" Maintainer)" in
  *" <109027774+darkiris4@users.noreply.github.com>") pass "control Maintainer uses the noreply address" ;;
  *) fail "control Maintainer uses the noreply address" "$(dpkg-deb -f "$deb" Maintainer)" ;;
esac
grep -q '^Description: .\+' <<<"$control" && [ "$(grep -c '^ ' <<<"$control")" -ge 5 ] \
  && pass "control has a short and a long description" || fail "control has a short and a long description"

# file list: type, mode and path of every entry, compared with the expected set
listing="$(dpkg-deb -c "$deb" | awk '{ owner = $2; path = $6; print $1 " " owner " " path }' | sed 's| \./| /|; s|^\([^ ]*\) \([^ ]*\) /$|\1 \2 /|')"
expected="drwxr-xr-x root/root /
drwxr-xr-x root/root /etc/
drwxr-xr-x root/root /etc/xdg/
drwxr-xr-x root/root /etc/xdg/autostart/
-rw-r--r-- root/root /etc/xdg/autostart/gpu-game-guard-tray.desktop
drwxr-xr-x root/root /usr/
drwxr-xr-x root/root /usr/bin/
-rwxr-xr-x root/root /usr/bin/gpu-game-guard
drwxr-xr-x root/root /usr/lib/
drwxr-xr-x root/root /usr/lib/systemd/
drwxr-xr-x root/root /usr/lib/systemd/user/
-rw-r--r-- root/root /usr/lib/systemd/user/gpu-game-guard.service
drwxr-xr-x root/root /usr/share/
drwxr-xr-x root/root /usr/share/applications/
-rw-r--r-- root/root /usr/share/applications/gpu-game-guard-tray.desktop
-rw-r--r-- root/root /usr/share/applications/gpu-game-guard.desktop
drwxr-xr-x root/root /usr/share/doc/
drwxr-xr-x root/root /usr/share/doc/gpu-game-guard/
-rw-r--r-- root/root /usr/share/doc/gpu-game-guard/README.md
-rw-r--r-- root/root /usr/share/doc/gpu-game-guard/changelog.gz
-rw-r--r-- root/root /usr/share/doc/gpu-game-guard/copyright
drwxr-xr-x root/root /usr/share/icons/
drwxr-xr-x root/root /usr/share/icons/hicolor/
drwxr-xr-x root/root /usr/share/icons/hicolor/scalable/
drwxr-xr-x root/root /usr/share/icons/hicolor/scalable/apps/
-rw-r--r-- root/root /usr/share/icons/hicolor/scalable/apps/gpu-game-guard.svg"
if [ "$(LC_ALL=C sort <<<"$listing")" = "$(LC_ALL=C sort <<<"$expected")" ]; then
  pass "file list, owners and permissions match exactly (no stray paths)"
else
  fail "file list, owners and permissions match exactly (no stray paths)" "$(diff <(LC_ALL=C sort <<<"$expected") <(LC_ALL=C sort <<<"$listing"))"
fi

# control archive: maintainer scripts are executable
ctl="$work/ctl"; mkdir -p "$ctl"
dpkg-deb -e "$deb" "$ctl"
modes_ok=1
for s in postinst prerm postrm; do
  [ "$(stat -c %a "$ctl/$s")" = 755 ] || modes_ok=0
done
[ "$modes_ok" = 1 ] && pass "maintainer scripts are mode 0755" || fail "maintainer scripts are mode 0755"
for s in postinst prerm postrm; do
  if sh -n "$ctl/$s" && bash -n "$ctl/$s" && head -n 1 "$ctl/$s" | grep -qx '#!/bin/sh'; then
    pass "$s parses with sh and bash"
  else
    fail "$s parses with sh and bash"
  fi
done

# extracted content
x="$work/x"; mkdir -p "$x"
dpkg-deb -x "$deb" "$x"
unit="$x/usr/lib/systemd/user/gpu-game-guard.service"
grep -qx 'ExecStart=/usr/bin/gpu-game-guard run' "$unit" && grep -qx 'RestartPreventExitStatus=78' "$unit" \
  && pass "unit runs /usr/bin/gpu-game-guard run and prevents restarts on 78" || fail "unit runs /usr/bin/gpu-game-guard run and prevents restarts on 78" "$(cat "$unit")"
head -n 1 "$x/usr/bin/gpu-game-guard" | grep -qx '#!/usr/bin/env python3' && pass "program is the zipapp" || fail "program is the zipapp"
"$x/usr/bin/gpu-game-guard" --version >/dev/null 2>&1 && pass "extracted program runs --version" || fail "extracted program runs --version"
cmp -s "$x/etc/xdg/autostart/gpu-game-guard-tray.desktop" "$x/usr/share/applications/gpu-game-guard-tray.desktop" \
  && pass "autostart entry is the tray entry" || fail "autostart entry is the tray entry"
grep -qx 'Exec=gpu-game-guard settings' "$x/usr/share/applications/gpu-game-guard.desktop" \
  && grep -qx 'Exec=gpu-game-guard tray' "$x/etc/xdg/autostart/gpu-game-guard-tray.desktop" \
  && pass "desktop entries run the commands by name" || fail "desktop entries run the commands by name"
if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$x/usr/share/applications/gpu-game-guard.desktop" "$x/usr/share/applications/gpu-game-guard-tray.desktop" >/dev/null 2>&1 \
    && pass "desktop entries validate" || fail "desktop entries validate"
else
  skip "desktop entries validate (desktop-file-validate not installed)"
fi
if command -v xmllint >/dev/null; then
  xmllint --noout "$x/usr/share/icons/hicolor/scalable/apps/gpu-game-guard.svg" 2>/dev/null && pass "icon is well-formed XML" || fail "icon is well-formed XML"
else
  python3 -c 'import sys, xml.dom.minidom as m; m.parse(sys.argv[1])' "$x/usr/share/icons/hicolor/scalable/apps/gpu-game-guard.svg" \
    && pass "icon is well-formed XML" || fail "icon is well-formed XML"
fi
zcat "$x/usr/share/doc/gpu-game-guard/changelog.gz" | head -n 1 | grep -q "^gpu-game-guard ($version) " \
  && pass "changelog starts with this version" || fail "changelog starts with this version"
python3 - "$x/usr/bin/gpu-game-guard" <<'PY' && pass "zipapp holds the ui and presets and nothing else" || fail "zipapp holds the ui and presets and nothing else"
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
bad = [n for n in names if "__pycache__" in n or n.endswith(".pyc") or n.startswith("tests")]
allowed = [n for n in names if n.endswith((".py", ".json", "/"))]
sys.exit(1 if bad or len(allowed) != len(names) else 0)
PY

# reproducibility
sleep 2
if bash "$repo/scripts/build-deb.sh" "$work/b" >/dev/null 2>&1 && cmp -s "$deb" "$work/b/$(basename "$deb")"; then
  pass "two builds are byte-identical"
else
  fail "two builds are byte-identical"
fi

# containers
deb_in_container() {  # deb_in_container IMAGE
  local image="$1" out
  out="$(docker run --rm -v "$deb:/gpu-game-guard.deb:ro" "$image" sh -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null
    apt-get install -y --no-install-recommends ./gpu-game-guard.deb >/dev/null
    gpu-game-guard --version
    gpu-game-guard config validate
    test -f /usr/share/applications/gpu-game-guard.desktop
    test -f /etc/xdg/autostart/gpu-game-guard-tray.desktop
    apt-get purge -y gpu-game-guard >/dev/null 2>&1
    ! test -e /usr/bin/gpu-game-guard
    ! test -e /usr/lib/systemd/user/gpu-game-guard.service
    ! test -e /usr/share/applications/gpu-game-guard.desktop
    ! test -e /etc/xdg/autostart/gpu-game-guard-tray.desktop
    ! test -e /usr/share/icons/hicolor/scalable/apps/gpu-game-guard.svg
    ! test -e /usr/share/doc/gpu-game-guard
    echo container-ok
  ' 2>&1)"
  if grep -q '^container-ok$' <<<"$out"; then
    pass "installs, runs --version and config validate, and purges cleanly in $image"
  else
    fail "installs, runs --version and config validate, and purges cleanly in $image" "$(tail -n 15 <<<"$out")"
  fi
}

if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  skip "container installs (docker is not available)"
else
  for image in ${GPU_GAME_GUARD_DEB_IMAGES:-debian:12 ubuntu:24.04}; do
    if docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image" >/dev/null 2>&1; then
      deb_in_container "$image"
    else
      skip "container install in $image (image could not be pulled)"
    fi
  done
fi

echo "passed=$passed skipped=$skipped failed=$failures"
[ "$failures" -eq 0 ]
