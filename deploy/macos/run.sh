#!/bin/bash
# Wrapper launchd executes to run the FINANCE dashboard + bot loop (Phase 13).
#
# Why a wrapper:
#   * loads secrets/config from .env (they live there, NEVER in the plist or git),
#   * pins the project virtualenv and PYTHONPATH,
#   * uses `exec` so launchd's SIGTERM goes straight to Python/uvicorn, which
#     shuts the bot down gracefully (stops the loop + runtime on the way out).
set -euo pipefail

# Repo root = two levels up from this script (deploy/macos/run.sh -> repo).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
cd "$REPO_DIR"

# Load .env if present (KEY=VALUE lines). Secrets never touch the plist.
if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$REPO_DIR/.env"
  set +a
fi

export PYTHONPATH="$REPO_DIR"

# Prefer the project venv; fall back to system python3 (must be 3.11+).
PY="$REPO_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "ERROR: no python found (create the venv with deploy/macos/install.sh)" >&2
  exit 1
fi

exec "$PY" scripts/run_dashboard.py
