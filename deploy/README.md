# Deploying the Futures Signal Engine (24/7)

> **Running it on a Mac mini (Apple Silicon) as a local server?** Use the native
> launchd setup in **[`deploy/macos/README.md`](macos/README.md)** — one command
> (`bash deploy/macos/install.sh`), no Docker. The guide below is for a
> Linux VPS / cloud server.

Two ways to run the bot + dashboard continuously on a VPS/cloud server so it
keeps working with your Mac closed and Claude Code shut. Pick **one**.

The bot is **signal-detection only**: no broker, no order execution, no
auto-trading, no AI in the signal path. CME contract specs are unverified, so
dollar-based position sizing stays disabled by design.

---

## Option A — Docker (simplest)

```bash
cp .env.example .env
# Set a token (remote mode refuses to serve publicly without one):
echo "DASHBOARD_TOKEN=$(openssl rand -hex 24)" >> .env
# Optional: Discord alerts
# echo "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/..." >> .env

docker compose up -d --build
docker compose logs -f          # watch it run
```

- Restarts automatically on crash and on server reboot (`restart: unless-stopped`).
- Health is checked via `GET /api/health` (`docker compose ps` shows healthy).
- The signal log persists in `./data_store` (mounted volume).
- Dashboard: `http://<server-ip>:8000/?token=<DASHBOARD_TOKEN>`

Headless (no UI) instead? `docker compose run --rm signal-engine python scripts/run_bot.py`
or change the image `CMD`.

---

## Option B — systemd (no Docker)

```bash
sudo useradd --system --create-home bot
sudo mkdir -p /opt/signal-engine /etc/signal-engine
sudo rsync -a --exclude .git ./ /opt/signal-engine/     # or git clone
cd /opt/signal-engine
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt

sudo cp .env.example /etc/signal-engine/dashboard.env
sudoedit /etc/signal-engine/dashboard.env               # set BOT_MODE=remote, DASHBOARD_TOKEN=...
sudo chown -R bot:bot /opt/signal-engine

sudo cp deploy/systemd/signal-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signal-dashboard
systemctl status signal-dashboard
journalctl -u signal-dashboard -f                       # live logs
```

- `Restart=always` recovers after crashes; `enable` starts it on every boot.
- `systemctl stop signal-dashboard` shuts it down gracefully (clean SIGINT).
- For the headless bot instead, use `deploy/systemd/signal-bot.service`.

---

## Accessing the dashboard from your iPhone

The dashboard is a normal responsive web page — any phone browser works. Do
**not** expose it unauthenticated on the public internet. Best options,
simplest first:

1. **Tailscale / WireGuard VPN (recommended).** Install Tailscale on the VPS
   and your iPhone; open `http://<tailscale-ip>:8000/`. Nothing is exposed
   publicly and you still keep the token.
2. **Cloudflare Tunnel / reverse proxy with HTTPS.** Put Caddy/Nginx +
   Let's Encrypt (or `cloudflared`) in front, terminating TLS, then browse to
   `https://your-domain/?token=<DASHBOARD_TOKEN>`. The token is stored on the
   phone after the first visit (localStorage), so later visits are just
   `https://your-domain/`.
3. **SSH port-forward (quick check):** `ssh -L 8000:127.0.0.1:8000 user@vps`
   then open `http://127.0.0.1:8000/` on a laptop.

**Security notes**
- Remote mode (`BOT_MODE=remote`) *requires* `DASHBOARD_TOKEN`; the server
  refuses to start a public, unauthenticated instance.
- The built-in token check is a **placeholder** — always put real TLS in front
  (VPN or reverse proxy). Never send the token over plain HTTP on an untrusted
  network.
- `GET /api/health` is intentionally unauthenticated (for uptime probes) and
  never returns secrets.
- The dashboard/API never exposes the Discord webhook or the token in any
  response.

---

## Configuration reference

All optional; safe local defaults. See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `BOT_MODE` | `local` | `local` (loopback) or `remote` (public bind + auth required) |
| `BOT_HOST` | `127.0.0.1` / `0.0.0.0` | Bind address (per mode) |
| `BOT_PORT` | `8000` | Bind port |
| `BOT_SYMBOLS` | `ES,NQ` | Instruments to run |
| `BOT_CYCLE_SECONDS` | `5` | Poll/cycle interval |
| `BOT_AUTO_START` | `false` (dashboard) / `true` (bot) | Start trading on boot |
| `DASHBOARD_TOKEN` | — | Bearer token; required in remote mode |
| `BOT_CORS_ORIGINS` | — | Explicit CORS origins (never `*`) |
| `BOT_LOG_FILE` | — | Optional rotating log file path |
| `DISCORD_WEBHOOK_URL` | — | Discord alerts (optional) |

## Health check

```bash
curl -s http://127.0.0.1:8000/api/health
# {"ok":true,"status":"ONLINE","connection":"CONNECTED","uptime_seconds":...,"symbols":["ES","NQ"],"mode":"remote"}
```
