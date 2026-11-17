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
cp -Rp "$root/gpu_game_guard" "$stage/gpu_game_guard"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$stage" -name '*.pyc' -delete

# The entry module is written here, not by zipapp, which would stamp it with the current time. Every
# staged file gets one fixed modification time so the same tree always gives the same bytes.
printf '%s\n' "# -*- coding: utf-8 -*-" "import gpu_game_guard.cli" "gpu_game_guard.cli.entry()" > "$stage/__main__.py"
find "$stage" -exec touch -h -d '2000-01-01 00:00:00 UTC' {} +

mkdir -p "$(dirname "$out")"
python3 -m zipapp "$stage" --output "$out" --python "/usr/bin/env python3" --compress
chmod 0755 "$out"
