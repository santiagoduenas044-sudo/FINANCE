#!/bin/bash
# Convenience control for the FINANCE launchd service.
#   bash deploy/macos/ctl.sh <start|stop|restart|status|logs|health|update>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
LABEL="com.finance.signal"
LOGDIR="$HOME/Library/Logs/finance-signal"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

_env() { [ -f "$REPO_DIR/.env" ] && ( set -a; . "$REPO_DIR/.env"; set +a; env ); }
_get() { _env | awk -F= -v k="$1" '$1==k{print substr($0,length(k)+2)}' | tail -1; }

url() {
  local host port tok; host="$(_get BOT_HOST)"; port="$(_get BOT_PORT)"; tok="$(_get DASHBOARD_TOKEN)"
  [ -z "$host" ] && host="127.0.0.1"; [ -z "$port" ] && port="8000"
  local u="http://$host:$port/"; [ -n "$tok" ] && u="$u?token=$tok"; echo "$u"
}

case "${1:-}" in
  start)   launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl kickstart "$DOMAIN/$LABEL"; echo "started";;
  stop)    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true; echo "stopped";;
  restart) launchctl kickstart -k "$DOMAIN/$LABEL"; echo "restarted";;
  status)
    echo "== launchd =="
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code =" || echo "not loaded"
    echo "== dashboard URL =="; url
    ;;
  logs)    echo "(Ctrl-C to stop) tailing $LOGDIR/*.log"; touch "$LOGDIR/dashboard.out.log" "$LOGDIR/dashboard.err.log"; tail -n 50 -f "$LOGDIR"/*.log;;
  health)
    local_host="$(_get BOT_HOST)"; local_port="$(_get BOT_PORT)"; tok="$(_get DASHBOARD_TOKEN)"
    [ -z "$local_host" ] && local_host="127.0.0.1"; [ -z "$local_port" ] && local_port="8000"
    echo "GET http://$local_host:$local_port/api/health"
    curl -fsS "http://$local_host:$local_port/api/health" && echo || echo "(no response — is it running? check 'ctl.sh logs')"
    echo "Open dashboard: $(url)"
    ;;
  update)
    cd "$REPO_DIR"
    echo "==> git pull"; git pull --ff-only
    echo "==> deps"; "$REPO_DIR/.venv/bin/python" -m pip install --quiet -r requirements.txt
    echo "==> restart"; launchctl kickstart -k "$DOMAIN/$LABEL"; echo "updated + restarted"
    ;;
  *)
    echo "usage: bash deploy/macos/ctl.sh <start|stop|restart|status|logs|health|update>" >&2
    exit 2;;
esac
