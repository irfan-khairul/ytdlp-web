#!/usr/bin/env bash
# Downloads everything the app needs into ./runtime so it runs without any
# system packages:
#   runtime/python/  standalone CPython with requirements.txt installed
#   runtime/bin/     ffmpeg, ffprobe, deno
#
# Usage: scripts/setup.sh [x86_64|aarch64] [runtime-dir]
# Defaults to this machine's architecture and ./runtime. Can be run from macOS
# to prepare a Linux runtime (see scripts/package.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/versions.env"

ARCH="${1:-$(uname -m)}"
case "$ARCH" in
  x86_64|amd64)  ARCH=x86_64;  FFMPEG_ARCH=linux64;    PIP_ARCH=x86_64 ;;
  aarch64|arm64) ARCH=aarch64; FFMPEG_ARCH=linuxarm64; PIP_ARCH=aarch64 ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
if [[ -z "${2:-}" && "$(uname -s)" != "Linux" ]]; then
  echo "The bundled runtime is Linux-only. On $(uname -s), use scripts/package.sh to build" >&2
  echo "Linux release tarballs, or see README for running from a venv." >&2
  exit 1
fi
RUNTIME="${2:-$ROOT/runtime}"
PY_MINOR="${PYTHON_VERSION%.*}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# fetch <url> <dest> <expected-sha256>
fetch() {
  echo "  ↓ $(basename "$2")"
  curl -fsSL --retry 3 -o "$2" "$1"
  local actual; actual="$(sha256 "$2")"
  if [[ -z "$3" || "$3" != "$actual" ]]; then
    echo "Checksum mismatch for $(basename "$2") (expected '$3', got '$actual')" >&2
    exit 1
  fi
}

echo "Preparing Linux $ARCH runtime in $RUNTIME"
rm -rf "$RUNTIME"
mkdir -p "$RUNTIME/bin"

# --- Python -----------------------------------------------------------------
PY_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_BUILD_TAG"
PY_NAME="cpython-$PYTHON_VERSION+$PYTHON_BUILD_TAG-$ARCH-unknown-linux-gnu-install_only_stripped.tar.gz"
curl -fsSL --retry 3 -o "$TMP/SHA256SUMS" "$PY_BASE/SHA256SUMS"
fetch "$PY_BASE/${PY_NAME//+/%2B}" "$TMP/python.tar.gz" \
  "$(grep " $PY_NAME\$" "$TMP/SHA256SUMS" | cut -d' ' -f1)"
tar -xzf "$TMP/python.tar.gz" -C "$RUNTIME"   # extracts to ./python

# Install wheels for the target platform. This uses pip's cross-platform mode,
# so it works the same whether we're on the target machine or not.
SITE="$RUNTIME/python/lib/python$PY_MINOR/site-packages"
if "$RUNTIME/python/bin/python3" -V >/dev/null 2>&1; then
  PIP=("$RUNTIME/python/bin/python3" -m pip)
else
  PIP=(python3 -m pip)
fi
echo "  ↓ Python packages"
"${PIP[@]}" install --quiet --disable-pip-version-check --no-compile --upgrade \
  --target "$SITE" \
  --python-version "$PY_MINOR" --implementation cp --abi "cp${PY_MINOR/./}" --abi abi3 --abi none \
  --platform "manylinux_2_28_$PIP_ARCH" --platform "manylinux_2_17_$PIP_ARCH" \
  --platform "manylinux2014_$PIP_ARCH" --platform any \
  --only-binary=:all: \
  -r "$ROOT/requirements.txt"

# --- ffmpeg -----------------------------------------------------------------
FF_BASE="https://github.com/yt-dlp/FFmpeg-Builds/releases/download/$FFMPEG_BUILD_TAG"
FF_NAME="ffmpeg-master-latest-$FFMPEG_ARCH-gpl"
curl -fsSL --retry 3 -o "$TMP/ff.sha256" "$FF_BASE/checksums.sha256"
fetch "$FF_BASE/$FF_NAME.tar.xz" "$TMP/ffmpeg.tar.xz" \
  "$(grep " $FF_NAME.tar.xz\$" "$TMP/ff.sha256" | cut -d' ' -f1)"
tar -xJf "$TMP/ffmpeg.tar.xz" -C "$TMP" "$FF_NAME/bin/ffmpeg" "$FF_NAME/bin/ffprobe"
install -m 755 "$TMP/$FF_NAME/bin/ffmpeg" "$TMP/$FF_NAME/bin/ffprobe" "$RUNTIME/bin/"

# --- Deno -------------------------------------------------------------------
DENO_BASE="https://github.com/denoland/deno/releases/download/$DENO_VERSION"
DENO_NAME="deno-$ARCH-unknown-linux-gnu.zip"
fetch "$DENO_BASE/$DENO_NAME" "$TMP/deno.zip" \
  "$(curl -fsSL --retry 3 "$DENO_BASE/$DENO_NAME.sha256sum" | grep -oE '[0-9a-f]{64}' | head -n1)"
unzip -q -o "$TMP/deno.zip" -d "$RUNTIME/bin"
chmod 755 "$RUNTIME/bin/deno"

echo "Done. Start the app with ./run.sh"
