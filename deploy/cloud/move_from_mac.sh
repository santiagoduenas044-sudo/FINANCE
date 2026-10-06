#!/bin/bash
# Runs on the Mac:  bash deploy/macos/ctl.sh nube <IP del servidor>
# Copies the bots (code, data_store/, market_data/, .env) to a cloud server,
# stops the Mac's own bots (so nothing posts twice), and starts them there.
# Running it again later copies only what changed (also an easy update).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IP="${1:-}"
[ -n "$IP" ] || { echo "uso: bash deploy/macos/ctl.sh nube <IP del servidor>"; exit 2; }
DEST="root@$IP"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

echo "==> Probando la conexión con $IP"
if ! "${SSH[@]}" -o BatchMode=yes "$DEST" true 2>/dev/null; then
  echo "No pude entrar a root@$IP con la llave de esta Mac."
  echo "Corre 'bash deploy/macos/ctl.sh nube llave', pega esa llave al crear el servidor"
  echo "(o en su panel -> SSH keys) y vuelve a intentar."
  exit 1
fi

copy() {   # copy [--code-only]
  local extra=()
  [ "${1:-}" = "--code-only" ] && extra=(--exclude '/data_store/' --exclude '/market_data/' --exclude '/.env')
  rsync -az --delete-after -e "ssh -o StrictHostKeyChecking=accept-new" \
    --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '*.pyc' \
    --exclude '/data_store/*.log' --exclude '/.ghost-moved' ${extra[@]+"${extra[@]}"} \
    "$REPO/" "$DEST:/opt/ghost/"
}

if "${SSH[@]}" "$DEST" "test -f /opt/ghost/.ghost-moved"; then
  # already moved: the server's data is the real one now; send only the code
  echo "==> Los bots ya viven en el servidor: copio solo el código nuevo"
  copy --code-only
  "${SSH[@]}" "$DEST" "cd /opt/ghost && docker compose up -d --build && docker compose restart ghost orbit"
  echo "Actualizado."
  exit 0
fi

echo "==> Copiando código y datos (la primera vez tarda unos minutos)"
"${SSH[@]}" "$DEST" "mkdir -p /opt/ghost"
copy

echo "==> Apagando los bots de esta Mac (para que no avisen dos veces)"
DOMAIN_L="gui/$(id -u)"
launchctl bootout "$DOMAIN_L/com.finance.signal" 2>/dev/null || true
launchctl disable "$DOMAIN_L/com.finance.signal" 2>/dev/null || true
launchctl bootout "$DOMAIN_L/com.finance.orbit" 2>/dev/null || true
launchctl disable "$DOMAIN_L/com.finance.orbit" 2>/dev/null || true

echo "==> Copia final (lo último que hicieron los bots)"
copy

echo "==> Encendiendo en el servidor"
"${SSH[@]}" -t "$DEST" "bash /opt/ghost/deploy/cloud/server_setup.sh && touch /opt/ghost/.ghost-moved"

echo
echo "Hecho. Los bots ya corren en el servidor; esta Mac puede apagarse."
echo "Volver a correr este comando más adelante solo manda el código nuevo (los datos se quedan allá)."
echo "Si algún día quieres volver a correrlos aquí: bash deploy/macos/ctl.sh nube volver"
