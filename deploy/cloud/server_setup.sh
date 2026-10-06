#!/bin/bash
# Runs ON the cloud server (Ubuntu/Debian), inside /opt/ghost, as root.
# Installs Docker, keeps the GitHub read token (private repo) out of the
# code, completes .env and starts GHOST + ORBIT + HTTPS. Safe to run again.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
APP="$(pwd)"
SECRETS=/opt/ghost-secrets

say() { printf '\n==> %s\n' "$*"; }

say "Docker y git"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq curl ca-certificates git >/dev/null
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git >/dev/null; }

say "Permiso de GitHub para actualizar (repositorio privado)"
mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
touch "$SECRETS/git-credentials" && chmod 600 "$SECRETS/git-credentials"
git config --global --add safe.directory "$APP" 2>/dev/null || true
git config --global credential.helper "store --file=$SECRETS/git-credentials"
if [ -s "$SECRETS/git-credentials" ]; then
  echo "Ya está guardado."
elif [ -t 0 ]; then
  echo "Para que 'Buscar actualización e instalar' funcione aquí, pega un token de GitHub de SOLO"
  echo "LECTURA (github.com -> Settings -> Developer settings -> Fine-grained tokens -> este"
  echo "repositorio -> Contents: Read-only). Se guarda solo en este servidor. Enter para saltar."
  printf "Token: "; IFS= read -rs tok || true; echo
  tok="$(printf '%s' "${tok:-}" | tr -d '[:space:]')"
  if [ -n "$tok" ]; then
    printf 'https://x-access-token:%s@github.com\n' "$tok" > "$SECRETS/git-credentials"
    echo "Guardado."
  else
    echo "Sin token: para actualizar, vuelve a correr 'bash deploy/macos/ctl.sh nube IP' desde la Mac."
  fi
fi

say "Configuración (.env)"
touch .env && chmod 600 .env
setenv() {   # KEY VALUE: replace or add, keeping every other line
  grep -v -E "^[[:space:]]*(export[[:space:]]+)?$1=" .env > .env.tmp || true
  printf '%s=%s\n' "$1" "$2" >> .env.tmp && mv .env.tmp .env && chmod 600 .env
}
getenv() { awk -F= -v k="$1" '$1==k{print substr($0,length(k)+2)}' .env | tail -1; }
[ -n "$(getenv DASHBOARD_TOKEN)" ] || setenv DASHBOARD_TOKEN "$(openssl rand -hex 24)"
if [ -z "$(getenv DOMAIN)" ]; then
  ip="$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')"
  setenv DOMAIN "$(echo "$ip" | tr '.' '-').sslip.io"
fi
setenv BOT_MODE remote
setenv BOT_AUTO_START true

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

say "Encendiendo GHOST, ORBIT y HTTPS"
docker compose up -d --build
sleep 8
docker compose ps

say "Listo"
echo "Tu página: https://$(getenv DOMAIN)"
echo "(el certificado HTTPS tarda hasta un minuto la primera vez)"
