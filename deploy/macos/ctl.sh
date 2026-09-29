#!/bin/bash
# Convenience control for the FINANCE launchd service.
#   bash deploy/macos/ctl.sh <start|stop|restart|status|logs|health|update>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
LABEL="com.finance.signal"
LOGDIR="$HOME/Library/Logs/finance-signal"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ORBIT_LABEL="com.finance.orbit"
ORBIT_PLIST="$HOME/Library/LaunchAgents/$ORBIT_LABEL.plist"
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
    if launchctl print "$DOMAIN/$ORBIT_LABEL" >/dev/null 2>&1; then
      launchctl kickstart -k "$DOMAIN/$ORBIT_LABEL"; echo "ORBIT restarted"
    fi
    ;;
  discord)
    # Phase 15c: standalone diagnostic. Reports what the running process would
    # see without sending anything. Never prints the webhook value itself.
    cd "$REPO_DIR"
    # Reuse the same robust .env parser run.sh uses, so this shows exactly
    # what run.sh will pass to Python.
    if [ -f "$REPO_DIR/.env" ]; then
      while IFS= read -r _raw || [ -n "$_raw" ]; do
        _line="${_raw#$'\xef\xbb\xbf'}"; _line="${_line%$'\r'}"
        _line="${_line#"${_line%%[![:space:]]*}"}"
        case "$_line" in ''|'#'*) continue ;; esac
        case "$_line" in export\ *) _line="${_line#export }" ;; esac
        case "$_line" in *=*) : ;; *) continue ;; esac
        _key="${_line%%=*}"; _val="${_line#*=}"
        _key="${_key%"${_key##*[![:space:]]}"}"; _key="${_key#"${_key%%[![:space:]]*}"}"
        _val="${_val#"${_val%%[![:space:]]*}"}"
        case "$_val" in \"*\") _val="${_val#\"}"; _val="${_val%\"}" ;; esac
        case "$_key" in ''|*[!A-Za-z0-9_]*|[0-9]*) continue ;; esac
        export "$_key=$_val"
      done < "$REPO_DIR/.env"
    fi
    PYTHONPATH="$REPO_DIR" "$REPO_DIR/.venv/bin/python" - <<'PYEOF'
import os
from signal_engine.config.schema import EngineConfig
from signal_engine.output.dispatcher import OutputDispatcher, _env_bool
cfg = EngineConfig.load()
raw = os.environ.get("BOT_DISCORD_ENABLED", "")
webhook_env = cfg.output.discord.webhook_env
print("BOT_DISCORD_ENABLED   raw=" + repr(raw) + "   parsed=" + str(_env_bool("BOT_DISCORD_ENABLED")))
print("cfg.output.discord.enabled (YAML) =", cfg.output.discord.enabled)
print("$" + webhook_env + " set in this shell =", bool(os.environ.get(webhook_env)))
d = OutputDispatcher.from_config(cfg)
print("adapters attached =", [a.name for a in d.adapters])
if d.adapters:
    a = d.adapters[0]
    print("  config.enabled =", a.config.enabled)
    print("  configured (webhook resolvable) =", a.configured)
PYEOF
    ;;
  autostart)
    case "${2:-}" in
      on)  bash "$REPO_DIR/deploy/macos/ensure-autostart.sh" "$REPO_DIR/.env";
           launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true;
           echo "autostart ON (restarted — will boot ONLINE)";;
      off) [ -f "$REPO_DIR/.env" ] && { sed 's|^[[:space:]]*BOT_AUTO_START=.*|BOT_AUTO_START=false|' "$REPO_DIR/.env" > "$REPO_DIR/.env.tmp" && mv "$REPO_DIR/.env.tmp" "$REPO_DIR/.env"; };
           grep -qE '^BOT_AUTO_START=' "$REPO_DIR/.env" || echo "BOT_AUTO_START=false" >> "$REPO_DIR/.env";
           launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true;
           echo "autostart OFF (restarted — boots OFFLINE, use START in dashboard)";;
      *)   echo "current: $(_get BOT_AUTO_START)"; echo "usage: ctl.sh autostart <on|off>";;
    esac
    ;;
  orbit)
    # ORBIT: the separate paper bot (APERTURA strategy). Its own service,
    # its own log, its own Discord channel ($DISCORD_WEBHOOK_ORBIT in .env).
    case "${2:-}" in
      on)
        mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"
        sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__LOGDIR__|$LOGDIR|g" \
            "$REPO_DIR/deploy/macos/com.finance.orbit.plist.template" > "$ORBIT_PLIST"
        launchctl bootout "$DOMAIN/$ORBIT_LABEL" 2>/dev/null || true
        launchctl bootstrap "$DOMAIN" "$ORBIT_PLIST"
        [ -n "$(_get DISCORD_WEBHOOK_ORBIT)" ] || echo "(aviso: falta DISCORD_WEBHOOK_ORBIT en .env — ORBIT solo escribirá en su log)"
        echo "ORBIT encendido (arranca solo con la Mac y se reinicia si falla)";;
      off)
        launchctl bootout "$DOMAIN/$ORBIT_LABEL" 2>/dev/null || true
        rm -f "$ORBIT_PLIST"
        echo "ORBIT apagado (no vuelve a arrancar hasta 'ctl.sh orbit on'; su récord se conserva)";;
      restart) launchctl kickstart -k "$DOMAIN/$ORBIT_LABEL" && echo "ORBIT reiniciado";;
      status)
        launchctl print "$DOMAIN/$ORBIT_LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code =" \
          || echo "ORBIT no está encendido";;
      logs)
        touch "$LOGDIR/orbit.out.log" "$LOGDIR/orbit.err.log"
        echo "(Ctrl-C para salir)"; tail -n 40 -f "$LOGDIR"/orbit.*.log;;
      *) echo "uso: bash deploy/macos/ctl.sh orbit <on|off|restart|status|logs>";;
    esac
    ;;
  *)
    echo "usage: bash deploy/macos/ctl.sh <start|stop|restart|status|logs|health|update|autostart on|off|discord|orbit on|off|status|logs>" >&2
    exit 2;;
esac
