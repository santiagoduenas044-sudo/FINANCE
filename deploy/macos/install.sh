#!/bin/bash
# One-shot installer for running FINANCE 24/7 on macOS (Apple Silicon) via
# launchd. Idempotent: safe to re-run after a `git pull`.
#
#   bash deploy/macos/install.sh
#
# It: checks Python 3.11+, creates a venv + installs deps, creates a .env with a
# generated dashboard token (only if missing), installs a LaunchAgent that
# auto-starts at login and auto-restarts on failure, and runs the preflight
# check. No secrets are written into the plist.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
cd "$REPO_DIR"
LABEL="com.finance.signal"
LOGDIR="$HOME/Library/Logs/finance-signal"
LA_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LA_DIR/$LABEL.plist"
UID_NUM="$(id -u)"

echo "==> FINANCE macOS installer"
echo "    repo:   $REPO_DIR"
echo "    logs:   $LOGDIR"

# 1) Python 3.11+ --------------------------------------------------------------
PYBIN="$(command -v python3.11 || command -v python3 || true)"
if [ -z "$PYBIN" ]; then
  echo "ERROR: python3 not found. Install it (e.g. 'brew install python@3.11')." >&2
  exit 1
fi
PYVER="$("$PYBIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if ! "$PYBIN" -c 'import sys;raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)'; then
  echo "ERROR: need Python 3.11+, found $PYVER ($PYBIN)." >&2
  exit 1
fi
echo "==> Python $PYVER OK ($PYBIN)"

# 2) venv + deps ---------------------------------------------------------------
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
  echo "==> Creating virtualenv (.venv)"
  "$PYBIN" -m venv "$REPO_DIR/.venv"
fi
echo "==> Installing dependencies"
"$REPO_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/python" -m pip install --quiet -r "$REPO_DIR/requirements.txt"

# 3) .env (created once; never overwritten so your secrets/token survive) ------
if [ ! -f "$REPO_DIR/.env" ]; then
  echo "==> Creating .env with a generated dashboard token"
  TOKEN="$(openssl rand -hex 24)"
  cat > "$REPO_DIR/.env" <<EOF
# FINANCE runtime config (macOS 24/7). Secrets live here only — never in git.
# Loopback bind + token (defense in depth). Reach it remotely via Tailscale.
BOT_MODE=local
BOT_HOST=127.0.0.1
BOT_PORT=8000
BOT_SYMBOLS=ES,NQ
BOT_CYCLE_SECONDS=5
BOT_AUTO_START=true
DASHBOARD_TOKEN=$TOKEN
BOT_LOG_FILE=$LOGDIR/bot.log
# Optional Discord alerts (paste your webhook, then set enabled in config):
DISCORD_WEBHOOK_URL=
EOF
  chmod 600 "$REPO_DIR/.env"
else
  echo "==> Keeping existing .env"
fi

# 3b) Guarantee the 24/7 service boots ONLINE (fixes a pre-existing .env that
#     was copied from .env.example with BOT_AUTO_START=false). Non-destructive
#     to every other key. STOP/PAUSE from the dashboard still work at runtime.
echo "==> Ensuring autostart is enabled"
bash "$REPO_DIR/deploy/macos/ensure-autostart.sh" "$REPO_DIR/.env"

# 4) logs dir + executables ----------------------------------------------------
mkdir -p "$LOGDIR" "$LA_DIR"
chmod +x "$REPO_DIR"/deploy/macos/*.sh 2>/dev/null || true

# 5) render + install the LaunchAgent -----------------------------------------
echo "==> Installing LaunchAgent -> $PLIST_DST"
sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__LOGDIR__|$LOGDIR|g" \
    "$REPO_DIR/deploy/macos/com.finance.signal.plist.template" > "$PLIST_DST"

# reload cleanly (bootout ignores "not loaded")
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
echo "==> LaunchAgent loaded (auto-start on login, auto-restart on failure)"

# 6) preflight (real Yahoo/Discord/config check) ------------------------------
echo "==> Running preflight..."
set +e
( set -a; . "$REPO_DIR/.env"; set +a; PYTHONPATH="$REPO_DIR" "$REPO_DIR/.venv/bin/python" scripts/preflight.py )
set -e

cat <<EOF

==> Done. The bot is running under launchd.

  Status:   bash deploy/macos/ctl.sh status
  Logs:     bash deploy/macos/ctl.sh logs
  Health:   bash deploy/macos/ctl.sh health
  Dashboard (on this Mac): open the URL printed by 'ctl.sh health'

IMPORTANT for a true 24/7 server:
  * Enable automatic login (System Settings > Users & Groups > Automatic login)
    so the LaunchAgent starts after a reboot.
  * Prevent sleep:  sudo pmset -c sleep 0 disksleep 0
  * Restart after power loss: System Settings > Energy > Start up automatically.
See deploy/macos/README.md for iPhone (Tailscale) access.
EOF
