#!/usr/bin/env bash
# Builds the single-file executable: a zipapp of the gpu_game_guard package whose entry point is
# gpu_game_guard.cli:entry, started through "#!/usr/bin/env python3". Standard library only.
#
#   scripts/build-zipapp.sh OUTPUT_PATH
set -euo pipefail

case "${1:-}" in
  ""|-h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; [ -n "${1:-}" ] && exit 0 || exit 2 ;;
esac
out="$1"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$root/gpu_game_guard" ] || { echo "error: no gpu_game_guard package under $root" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp -R "$root/gpu_game_guard" "$stage/gpu_game_guard"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$stage" -name '*.pyc' -delete

mkdir -p "$(dirname "$out")"
python3 -m zipapp "$stage" --output "$out" --python "/usr/bin/env python3" --main "gpu_game_guard.cli:entry" --compress
chmod 0755 "$out"
