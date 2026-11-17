#!/usr/bin/env bash
# Runs the unit tests inside python:3.9 .. python:3.13 Docker containers and prints one line per image.
# The repository is mounted read-only and copied to a temporary directory inside the container, so no
# __pycache__ or other file appears in the working tree.
#
#   scripts/test-matrix-docker.sh              all versions
#   scripts/test-matrix-docker.sh 3.9 3.12     only the listed versions
#
# Exit status: 0 when at least one image was tested and every tested image passed; 1 when any failed or
# when every image was skipped (nothing was tested).
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
versions=("$@")
[ "${#versions[@]}" -gt 0 ] || versions=(3.9 3.10 3.11 3.12 3.13)

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

failed=0 passed=0 skipped=0
for version in "${versions[@]}"; do
  image="python:$version"
  if ! docker image inspect "$image" >/dev/null 2>&1 && ! docker pull -q "$image" >/dev/null 2>&1; then
    echo "SKIP $image (could not be pulled)"
    skipped=$((skipped + 1))
    continue
  fi
  if output="$(docker run --rm -v "$repo:/src:ro" -e PYTHONDONTWRITEBYTECODE=1 "$image" sh -c \
      'd=$(mktemp -d) && cp -r /src/. "$d" && cd "$d" && python -m unittest discover -s tests -t . 2>&1' 2>&1)"; then
    echo "PASS $image"
    passed=$((passed + 1))
  else
    echo "FAIL $image"
    printf '%s\n' "$output" | tail -n 25 | sed 's/^/    /'
    failed=$((failed + 1))
  fi
done

echo "passed: $passed, failed: $failed, skipped: $skipped"
if [ "$passed" -eq 0 ] && [ "$failed" -eq 0 ]; then
  echo "nothing was tested: every image was skipped" >&2
  exit 1
fi
[ "$failed" -eq 0 ]
