#!/usr/bin/env bash
# Starts the app using the bundled runtime in ./runtime (see scripts/setup.sh).
# Settings come from the environment, e.g. HOST=0.0.0.0 PORT=8080 ./run.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -x "$DIR/runtime/python/bin/python3" ]]; then
  echo "No bundled runtime found. Run scripts/setup.sh first." >&2
  exit 1
fi

export PATH="$DIR/runtime/bin:$PATH"
export PYTHONUNBUFFERED=1
exec "$DIR/runtime/python/bin/python3" "$DIR/app.py" "$@"
