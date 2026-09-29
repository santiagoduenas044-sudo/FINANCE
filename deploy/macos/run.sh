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
# Phase 15c: robust parser — strips BOM, skips comments/blank lines, tolerates
# `export KEY=value`, spaces around `=`, and surrounding quotes. A malformed
# line is skipped with a warning to STDERR (does NOT kill the wrapper), so a
# tiny edit mistake in .env can never mysteriously disable the bot.
if [ -f "$REPO_DIR/.env" ]; then
  # Read line by line so `set -e` doesn't kill us on a bad row.
  while IFS= read -r _raw || [ -n "$_raw" ]; do
    # Strip UTF-8 BOM from the very first line (added by some macOS editors).
    _line="${_raw#$'\xef\xbb\xbf'}"
    # Strip \r (CRLF from cross-platform editors).
    _line="${_line%$'\r'}"
    # Trim leading whitespace.
    _line="${_line#"${_line%%[![:space:]]*}"}"
    # Skip blank + full-line comments.
    case "$_line" in ''|'#'*) continue ;; esac
    # Drop optional `export ` prefix.
    case "$_line" in export\ *) _line="${_line#export }" ;; esac
    # Require KEY=VALUE (an `=` must be present).
    case "$_line" in *=*) : ;; *)
      echo "run.sh: skipping malformed .env line: ${_line}" >&2; continue ;;
    esac
    _key="${_line%%=*}"
    _val="${_line#*=}"
    # Trim spaces around KEY and leading spaces from VALUE.
    _key="${_key%"${_key##*[![:space:]]}"}"
    _key="${_key#"${_key%%[![:space:]]*}"}"
    _val="${_val#"${_val%%[![:space:]]*}"}"
    # Strip surrounding single or double quotes if present.
    case "$_val" in
      \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;;
      \'*\') _val="${_val#\'}"; _val="${_val%\'}" ;;
    esac
    # Validate KEY is a plausible shell identifier (letters/digits/underscore).
    case "$_key" in
      ''|*[!A-Za-z0-9_]*|[0-9]*)
        echo "run.sh: skipping invalid key: ${_key}" >&2; continue ;;
    esac
    export "$_key=$_val"
  done < "$REPO_DIR/.env"
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

# `run.sh orbit` starts ORBIT (the separate APERTURA paper bot) instead.
if [ "${1:-}" = "orbit" ]; then
  exec "$PY" -m signal_engine.subbots.orbit
fi
exec "$PY" scripts/run_dashboard.py
