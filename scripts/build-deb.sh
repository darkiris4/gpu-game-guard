#!/usr/bin/env bash
# Builds the Debian package gpu-game-guard_<version>_all.deb: the zipapp as /usr/bin/gpu-game-guard, the
# systemd user unit, the desktop and autostart entries, the icon and the documentation. The same tree and
# the same SOURCE_DATE_EPOCH always give the same bytes.
#
#   scripts/build-deb.sh OUTPUT_DIR
#
# Environment: SOURCE_DATE_EPOCH (seconds; default the time of the last git commit, else 2000-01-01).
set -euo pipefail

usage() {
  cat <<'USAGE'
Builds the Debian package gpu-game-guard_<version>_all.deb: the zipapp as /usr/bin/gpu-game-guard, the
systemd user unit, the desktop and autostart entries, the icon and the documentation.

  scripts/build-deb.sh OUTPUT_DIR

Environment: SOURCE_DATE_EPOCH (seconds; default the time of the last git commit, else 2000-01-01).
USAGE
}

case "${1:-}" in
  ""|-h|--help) usage; [ -n "${1:-}" ] && exit 0 || exit 2 ;;
esac
outdir="$1"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkg="gpu-game-guard"
maintainer_email="109027774+darkiris4@users.noreply.github.com"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v dpkg-deb >/dev/null || die "dpkg-deb is required"
command -v gzip >/dev/null || die "gzip is required"
[ -f "$root/gpu_game_guard/__init__.py" ] || die "no gpu_game_guard package under $root"
for f in debian/control.in debian/postinst debian/prerm debian/postrm copyright gpu-game-guard.desktop \
         gpu-game-guard-tray.desktop gpu-game-guard.svg; do
  [ -f "$root/packaging/$f" ] || die "missing packaging/$f"
done
[ -f "$root/$pkg.service" ] || die "missing $pkg.service"

version="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$root/gpu_game_guard/__init__.py" | head -n 1)"
[ -n "$version" ] || die "could not read the version from gpu_game_guard/__init__.py"

epoch="${SOURCE_DATE_EPOCH:-}"
if [ -z "$epoch" ] && command -v git >/dev/null; then
  epoch="$(git -C "$root" log -1 --format=%ct 2>/dev/null || true)"
fi
case "$epoch" in
  ""|*[!0-9]*) epoch=946684800 ;;
esac
export SOURCE_DATE_EPOCH="$epoch"

maintainer_name=""
if command -v git >/dev/null; then
  maintainer_name="$(git -C "$root" log -1 --format=%an 2>/dev/null || true)"
  [ -n "$maintainer_name" ] || maintainer_name="$(git -C "$root" config user.name 2>/dev/null || true)"
fi
[ -n "$maintainer_name" ] || maintainer_name="darkiris4"

umask 022
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
tree="$stage/tree"
mkdir -p "$tree/DEBIAN" "$tree/usr/bin" "$tree/usr/lib/systemd/user" "$tree/usr/share/applications" \
  "$tree/etc/xdg/autostart" "$tree/usr/share/icons/hicolor/scalable/apps" "$tree/usr/share/doc/$pkg"

bash "$root/scripts/build-zipapp.sh" "$stage/$pkg" || die "could not build the zipapp"
install -m 0755 "$stage/$pkg" "$tree/usr/bin/$pkg"

# The unit starts the installed program; everything else is the repository's unit as it is.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ExecStart=*) printf '%s\n' "ExecStart=/usr/bin/$pkg run" ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$root/$pkg.service" > "$stage/$pkg.service"
grep -qx 'RestartPreventExitStatus=78' "$stage/$pkg.service" || die "the unit lacks RestartPreventExitStatus=78"
install -m 0644 "$stage/$pkg.service" "$tree/usr/lib/systemd/user/$pkg.service"

install -m 0644 "$root/packaging/$pkg.desktop" "$tree/usr/share/applications/$pkg.desktop"
install -m 0644 "$root/packaging/$pkg-tray.desktop" "$tree/usr/share/applications/$pkg-tray.desktop"
install -m 0644 "$root/packaging/$pkg-tray.desktop" "$tree/etc/xdg/autostart/$pkg-tray.desktop"
install -m 0644 "$root/packaging/$pkg.svg" "$tree/usr/share/icons/hicolor/scalable/apps/$pkg.svg"

doc="$tree/usr/share/doc/$pkg"
install -m 0644 "$root/packaging/copyright" "$doc/copyright"
if [ -f "$root/README.md" ]; then
  install -m 0644 "$root/README.md" "$doc/README.md"
else
  printf '%s\n' "# GPU Game Guard" > "$doc/README.md"
  chmod 0644 "$doc/README.md"
fi

# Debian changelog: one entry for this version, listing each bullet of the newest section of
# CHANGELOG.md (or a single line when there is none).
date_line="$(LC_ALL=C date -u -d "@$epoch" '+%a, %d %b %Y %H:%M:%S +0000')"
{
  printf '%s (%s) unstable; urgency=medium\n\n' "$pkg" "$version"
  bullets=""
  if [ -f "$root/CHANGELOG.md" ]; then
    bullets="$(awk '
      /^## /{ if (line != "") print line; line = ""; n++; next }
      n != 1 { next }
      /^- /{ if (line != "") print line; line = $0; sub(/^- /, "  * ", line); next }
      /^  [^ ]/ && line != "" { sub(/^ +/, " "); line = line $0; next }
      /^### /{ if (line != "") print line; line = "" }
      END { if (line != "") print line }' "$root/CHANGELOG.md" | head -n 25)"
  fi
  if [ -n "$bullets" ]; then printf '%s\n' "$bullets"; else printf '  * Release %s.\n' "$version"; fi
  printf '\n -- %s <%s>  %s\n' "$maintainer_name" "$maintainer_email" "$date_line"
} | gzip -n -9 > "$doc/changelog.gz"
chmod 0644 "$doc/changelog.gz"

size="$(du -sk --exclude=DEBIAN "$tree" | cut -f1)"
sed -e "s|@VERSION@|$version|" -e "s|@MAINTAINER@|$maintainer_name <$maintainer_email>|" -e "s|@SIZE@|$size|" \
  "$root/packaging/debian/control.in" > "$tree/DEBIAN/control"
chmod 0644 "$tree/DEBIAN/control"
for script in postinst prerm postrm; do
  install -m 0755 "$root/packaging/debian/$script" "$tree/DEBIAN/$script"
done

find "$tree" -exec touch -h -d "@$epoch" {} +
find "$tree" -type d -exec chmod 0755 {} +

mkdir -p "$outdir"
deb="$outdir/${pkg}_${version}_all.deb"
rm -f "$deb"
dpkg-deb --root-owner-group -Zxz --build "$tree" "$deb" >/dev/null || die "dpkg-deb failed"
printf '%s\n' "$deb"
