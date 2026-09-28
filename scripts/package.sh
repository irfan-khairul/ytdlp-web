#!/usr/bin/env bash
# Builds self-contained release tarballs in ./dist, one per architecture.
# Each one only needs a glibc-based Linux to run: extract, then ./run.sh
#
# Usage: scripts/package.sh [x86_64|aarch64 ...]   (default: both)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHES=("$@")
[[ ${#ARCHES[@]} -eq 0 ]] && ARCHES=(x86_64 aarch64)
mkdir -p "$ROOT/dist"

for ARCH in "${ARCHES[@]}"; do
  NAME="ytdlp-web-linux-$ARCH"
  STAGE="$ROOT/dist/$NAME"
  rm -rf "$STAGE"
  mkdir -p "$STAGE/scripts"

  cp -R "$ROOT/app.py" "$ROOT/static" "$ROOT/run.sh" "$ROOT/requirements.txt" "$ROOT/.env.example" \
        "$ROOT/ytdlp-web.service" "$ROOT/README.md" "$STAGE/"
  cp "$ROOT/scripts/setup.sh" "$ROOT/scripts/versions.env" "$STAGE/scripts/"
  "$ROOT/scripts/setup.sh" "$ARCH" "$STAGE/runtime"

  # macOS bsdtar embeds Apple xattrs, which make GNU tar warn on every file.
  TAR_FLAGS=()
  tar --version | grep -q bsdtar && TAR_FLAGS=(--no-xattrs --no-mac-metadata)
  COPYFILE_DISABLE=1 tar "${TAR_FLAGS[@]}" -czf "$ROOT/dist/$NAME.tar.gz" -C "$ROOT/dist" "$NAME"
  rm -rf "$STAGE"
  echo "Built dist/$NAME.tar.gz ($(du -h "$ROOT/dist/$NAME.tar.gz" | cut -f1))"
done
