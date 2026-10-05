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
    if [ "${2:-}" = "comandos" ]; then
      # The command bot (!record, !setups, !status…) is a Discord bot of its
      # own, apart from the webhooks. Its token is typed here, on the Mac, and
      # goes only into .env (never printed).
      cat <<'TXT'
Comandos de Discord (!record, !setups, !status…): es un bot aparte de los avisos.
 1. En https://discord.com/developers/applications -> New Application (nombre: GHOST).
 2. Menú "Bot": prende "Message Content Intent" y guarda (Save Changes).
 3. En la misma página: "Reset Token" -> "Copy".
 4. Menú "OAuth2" -> "URL Generator": marca "bot"; abajo marca "Send Messages",
    "Read Message History" y "Attach Files". Abre la URL que sale y agrega
    el bot a tu servidor.
TXT
      printf "Pega el token aquí (no se ve al escribir) y presiona Enter: "
      IFS= read -rs _tok || true; echo
      _tok="$(printf '%s' "${_tok:-}" | tr -d '[:space:]')"
      if [ -z "$_tok" ]; then echo "No llegó ningún token; no cambié nada."; exit 1; fi
      touch "$REPO_DIR/.env"
      grep -v -E '^[[:space:]]*(export[[:space:]]+)?DISCORD_BOT_TOKEN=' "$REPO_DIR/.env" > "$REPO_DIR/.env.tmp" || true
      printf 'DISCORD_BOT_TOKEN=%s\n' "$_tok" >> "$REPO_DIR/.env.tmp"
      mv "$REPO_DIR/.env.tmp" "$REPO_DIR/.env"; chmod 600 "$REPO_DIR/.env"
      echo "==> paquete de Discord"; "$REPO_DIR/.venv/bin/python" -m pip install --quiet -r "$REPO_DIR/requirements.txt" || true
      launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
      echo "Listo: token guardado y bot reiniciado. En ~20 s escribe !ayuda en Discord."
      echo "En la página: Sistema -> Comandos de Discord dice si quedó conectado."
      exit 0
    fi
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
print("DISCORD_BOT_TOKEN (comandos !record) set =", bool(os.environ.get("DISCORD_BOT_TOKEN", "").strip()))
try:
    import discord  # noqa: F401
    print("paquete discord.py instalado = True")
except ImportError:
    print("paquete discord.py instalado = False  (bash deploy/macos/ctl.sh update)")
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
  ghost)
    # "ghost porque [DIAS]" = what the live GHOST did with each idea and why
    # "ghost probar [DESDE [HASTA]]" = replay real days through today's rules
    # (Yahoo's last days, or the Databento history), without touching the record
    shift 1
    _cmd="${1:-}"; [ $# -gt 0 ] && shift
    _args=()
    case "$_cmd" in
      porque) _args=(--log "${1:-3}") ;;
      probar)
        for _a in "$@"; do
          if [[ " ${_args[*]:-} " == *" --desde "* ]]; then _args+=(--hasta "$_a")
          else _args+=(--desde "$_a"); fi
        done ;;
      *) echo "uso: bash deploy/macos/ctl.sh ghost <porque [DIAS]|probar [DESDE [HASTA]]>"; exit 2 ;;
    esac
    ( set -a; [ -f "$REPO_DIR/.env" ] && . "$REPO_DIR/.env"; set +a; cd "$REPO_DIR"
      PYTHONPATH="$REPO_DIR" "$REPO_DIR/.venv/bin/python" -m signal_engine.runtime.probar \
        ${_args[@]+"${_args[@]}"} );;
  orbit)
    # ORBIT: the separate paper bot (APERTURA strategy). Its own service,
    # its own log, its own Discord channel ($DISCORD_WEBHOOK_ORBIT in .env).
    case "${2:-}" in
      on)
        mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"
        sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__LOGDIR__|$LOGDIR|g" \
            "$REPO_DIR/deploy/macos/com.finance.orbit.plist.template" > "$ORBIT_PLIST"
        launchctl bootout "$DOMAIN/$ORBIT_LABEL" 2>/dev/null || true
        # bootout returns before launchd has let go; bootstrapping at once
        # fails with "5: Input/output error" when ORBIT was already running
        for _ in 1 2 3 4 5 6 7 8 9 10; do
          launchctl print "$DOMAIN/$ORBIT_LABEL" >/dev/null 2>&1 || break; sleep 1
        done
        launchctl bootstrap "$DOMAIN" "$ORBIT_PLIST"
        [ -n "$(_get DISCORD_WEBHOOK_ORBIT)" ] || echo "(aviso: falta DISCORD_WEBHOOK_ORBIT en .env — ORBIT solo escribirá en su log)"
        echo "ORBIT encendido (arranca solo con la Mac y se reinicia si falla)";;
      off)
        launchctl bootout "$DOMAIN/$ORBIT_LABEL" 2>/dev/null || true
        rm -f "$ORBIT_PLIST"
        echo "ORBIT apagado (no vuelve a arrancar hasta 'ctl.sh orbit on'; su récord se conserva)";;
      restart) launchctl kickstart -k "$DOMAIN/$ORBIT_LABEL" && echo "ORBIT reiniciado";;
      probar)
        # replay past bars through ORBIT (temporary folder, the real record is
        # untouched). "probar" = Yahoo's last days · "probar discord" also posts
        # them · "probar 2026-01-01 [2026-06-30]" = Databento history
        shift 2
        _args=()
        for _a in "$@"; do
          case "$_a" in
            discord) _args+=(--discord) ;;
            detalle) _args+=(--detalle) ;;
            [0-9][0-9][0-9][0-9]-*)
              if [[ " ${_args[*]:-} " == *" --desde "* ]]; then _args+=(--hasta "$_a")
              else _args+=(--desde "$_a"); fi ;;
          esac
        done
        ( set -a; [ -f "$REPO_DIR/.env" ] && . "$REPO_DIR/.env"; set +a; cd "$REPO_DIR"
          PYTHONPATH="$REPO_DIR" "$REPO_DIR/.venv/bin/python" -m signal_engine.subbots.orbit \
            --probar ${_args[@]+"${_args[@]}"} );;
      status)
        launchctl print "$DOMAIN/$ORBIT_LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code =" \
          || echo "ORBIT no está encendido";;
      logs)
        touch "$LOGDIR/orbit.out.log" "$LOGDIR/orbit.err.log"
        echo "(Ctrl-C para salir)"; tail -n 40 -f "$LOGDIR"/orbit.*.log;;
      *) echo "uso: bash deploy/macos/ctl.sh orbit <on|off|restart|status|logs|probar [discord] [DESDE [HASTA]] [detalle]>";;
    esac
    ;;
  web)
    # La página web: usuarios con contraseña y publicarla en internet con
    # Tailscale Funnel (https://<esta-mac>.<tailnet>.ts.net, gratis, sin abrir puertos).
    cd "$REPO_DIR"
    PY="$REPO_DIR/.venv/bin/python"
    TS="$(command -v tailscale 2>/dev/null || true)"
    [ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
    port="$(_get BOT_PORT)"; [ -z "$port" ] && port="8000"
    _users() { PYTHONPATH="$REPO_DIR" "$PY" -c "from pathlib import Path; from signal_engine.api.accounts import Accounts; print(len(Accounts(Path('$REPO_DIR')).users()))"; }
    _need_ts() { [ -n "$TS" ] || { echo "No encuentro Tailscale. Instala la app de Tailscale en esta Mac."; exit 1; }; }
    case "${2:-}" in
      usuario)
        [ -n "${3:-}" ] || { echo "uso: bash deploy/macos/ctl.sh web usuario NOMBRE [owner|viewer]"; exit 2; }
        if [ -n "${4:-}" ]; then PYTHONPATH="$REPO_DIR" "$PY" -m signal_engine.api.accounts add "$3" "$4"
        else PYTHONPATH="$REPO_DIR" "$PY" -m signal_engine.api.accounts add "$3"; fi;;
      usuarios) PYTHONPATH="$REPO_DIR" "$PY" -m signal_engine.api.accounts list;;
      borrar)
        [ -n "${3:-}" ] || { echo "uso: bash deploy/macos/ctl.sh web borrar NOMBRE"; exit 2; }
        PYTHONPATH="$REPO_DIR" "$PY" -m signal_engine.api.accounts remove "$3";;
      on)
        _need_ts
        if [ "$(_users)" = "0" ]; then
          echo "Primero crea tu usuario (la página quedaría abierta a cualquiera):"
          echo "  bash deploy/macos/ctl.sh web usuario TU_NOMBRE owner"; exit 1
        fi
        echo "Publicando el dashboard (puerto $port) con Tailscale Funnel…"
        "$TS" funnel --bg "$port"
        echo; "$TS" funnel status
        echo; echo "Abre la dirección https://… de arriba desde cualquier celular o compu y entra con tu usuario.";;
      off)
        _need_ts
        "$TS" funnel --https=443 off && echo "La página ya no está publicada en internet (Tailscale sigue funcionando).";;
      status)
        _need_ts
        echo "Usuarios: $(_users)"; "$TS" funnel status;;
      *) echo "uso: bash deploy/macos/ctl.sh web <usuario NOMBRE [owner|viewer]|usuarios|borrar NOMBRE|on|off|status>";;
    esac
    ;;
  *)
    echo "usage: bash deploy/macos/ctl.sh <start|stop|restart|status|logs|health|update|autostart on|off|discord [comandos]|ghost porque|probar|orbit on|off|status|logs|web on|off|usuario>" >&2
    exit 2;;
esac
