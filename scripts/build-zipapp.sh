#!/usr/bin/env bash
# Builds the single-file executable: a zipapp of the gpu_game_guard package whose entry point is
# gpu_game_guard.cli:entry, started through "#!/usr/bin/env python3". Standard library only.
#
#   scripts/build-zipapp.sh OUTPUT_PATH
set -euo pipefail

usage() {
  cat <<'USAGE'
Builds the single-file executable: a zipapp of the gpu_game_guard package whose entry point is
gpu_game_guard.cli:entry, started through "#!/usr/bin/env python3". Standard library only.

  scripts/build-zipapp.sh OUTPUT_PATH
USAGE
}

case "${1:-}" in
  ""|-h|--help) usage; [ -n "${1:-}" ] && exit 0 || exit 2 ;;
esac
out="$1"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$root/gpu_game_guard" ] || { echo "error: no gpu_game_guard package under $root" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
# Only the package's Python modules and JSON data (the presets) go in: no tests, caches or other files. Modes
# are normalised so the archive does not depend on the checkout's umask.
umask 022
(cd "$root" && find gpu_game_guard -name __pycache__ -prune -o -type f \( -name '*.py' -o -name '*.json' \) -print0) | while IFS= read -r -d '' file; do
  install -D -m 0644 "$root/$file" "$stage/$file"
done
[ -f "$stage/gpu_game_guard/cli.py" ] || { echo "error: the package has no cli.py" >&2; exit 1; }
find "$stage" -type d -exec chmod 0755 {} +

# The entry module is written here, not by zipapp, which would stamp it with the current time. Every
# staged file gets one fixed modification time so the same tree always gives the same bytes.
printf '%s\n' "# -*- coding: utf-8 -*-" "import gpu_game_guard.cli" "gpu_game_guard.cli.entry()" > "$stage/__main__.py"
find "$stage" -exec touch -h -d '2000-01-01 00:00:00 UTC' {} +

mkdir -p "$(dirname "$out")"
TZ=UTC python3 -m zipapp "$stage" --output "$out" --python "/usr/bin/env python3" --compress
chmod 0755 "$out"
