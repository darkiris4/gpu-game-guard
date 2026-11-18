#!/usr/bin/env bash
# Runs the unit tests inside python:3.9 .. python:3.13 Docker containers and prints one line per image, then
# once more in a debian:12 container with python3-pyqt5 from apt and QT_QPA_PLATFORM=offscreen, so the Qt
# tests run (the python images have no PyQt5 and skip them).
# The repository is mounted read-only and copied to a temporary directory inside the container, so no
# __pycache__ or other file appears in the working tree.
#
#   scripts/test-matrix-docker.sh              all versions
#   scripts/test-matrix-docker.sh 3.9 3.12     only the listed versions (the Qt run is always included)
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

# The Qt run: the apt PyQt5 package belongs to the distribution's own interpreter, so it needs a Debian image.
image="debian:12 + python3-pyqt5"
if ! docker image inspect debian:12 >/dev/null 2>&1 && ! docker pull -q debian:12 >/dev/null 2>&1; then
  echo "SKIP $image (debian:12 could not be pulled)"
  skipped=$((skipped + 1))
elif output="$(docker run --rm -v "$repo:/src:ro" -e PYTHONDONTWRITEBYTECODE=1 -e QT_QPA_PLATFORM=offscreen \
    -e DEBIAN_FRONTEND=noninteractive debian:12 sh -c \
    'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends python3 python3-pyqt5 >/dev/null \
     && d=$(mktemp -d) && cp -r /src/. "$d" && cd "$d" && python3 -m unittest discover -s tests -t . 2>&1' 2>&1)"; then
  echo "PASS $image"
  passed=$((passed + 1))
else
  echo "FAIL $image"
  printf '%s\n' "$output" | tail -n 25 | sed 's/^/    /'
  failed=$((failed + 1))
fi

echo "passed: $passed, failed: $failed, skipped: $skipped"
if [ "$passed" -eq 0 ] && [ "$failed" -eq 0 ]; then
  echo "nothing was tested: every image was skipped" >&2
  exit 1
fi
[ "$failed" -eq 0 ]
