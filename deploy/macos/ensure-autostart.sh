#!/bin/bash
# Ensure a .env enables 24/7 autostart, i.e. BOT_AUTO_START=true, so the
# launchd-managed service boots ONLINE instead of waiting for a manual START.
#
# Idempotent and NON-DESTRUCTIVE to every other key:
#   * key missing        -> appended
#   * key present, false -> updated in place to true
#   * key already true   -> left untouched
#
#   bash deploy/macos/ensure-autostart.sh /path/to/.env
#
# Kept as a tiny standalone script (not inlined in install.sh) so it is unit
# tested directly and reused by `ctl.sh`.
set -euo pipefail

ENV_FILE="${1:?usage: ensure-autostart.sh <env_file>}"
KEY="BOT_AUTO_START"

if [ ! -f "$ENV_FILE" ]; then
  printf '%s=true\n' "$KEY" > "$ENV_FILE"
  echo "created $ENV_FILE with $KEY=true"
  exit 0
fi

if grep -qE "^[[:space:]]*${KEY}=" "$ENV_FILE"; then
  cur="$(grep -E "^[[:space:]]*${KEY}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
  if [ "$cur" = "true" ]; then
    echo "$KEY already true — no change"
  else
    # portable in-place edit (BSD/macOS + GNU sed)
    sed "s|^[[:space:]]*${KEY}=.*|${KEY}=true|" "$ENV_FILE" > "$ENV_FILE.tmp" \
      && mv "$ENV_FILE.tmp" "$ENV_FILE"
    echo "updated $KEY: $cur -> true"
  fi
else
  printf '%s=true\n' "$KEY" >> "$ENV_FILE"
  echo "appended $KEY=true"
fi
