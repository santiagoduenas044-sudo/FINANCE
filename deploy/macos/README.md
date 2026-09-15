# Running FINANCE 24/7 on a Mac mini (Apple Silicon)

Native macOS setup with **launchd** — no Docker needed (and no ARM image
worries). The bot + dashboard start on login, restart automatically if they
crash, shut down gracefully, log to files, and can only run as a single
instance. Reach the dashboard from your iPhone privately via **Tailscale**.

The bot is **signal-detection only**: no broker, no order execution, no
auto-trading, no LLM in the signal path. CME specs stay unverified, so dollar
sizing is disabled by design. Market data is Yahoo Finance **delayed** ES/NQ.

---

## 0. Requirements

- Mac mini M2 (Apple Silicon) — Apple Silicon is fully supported; nothing here
  depends on x86/Docker images.
- **Python 3.11+**. Check: `python3 --version`. If missing: `brew install python@3.11`.
- Command Line Tools (for `git`): `xcode-select --install`.

---

## 1. Install (one command)

```bash
git clone https://github.com/santiagoduenas044-sudo/FINANCE.git
cd FINANCE
git checkout claude/phase-12-interactive-dashboard-5j2jsq
bash deploy/macos/install.sh
```

The installer:
- checks Python 3.11+,
- creates `.venv` and installs dependencies,
- creates `.env` with a **generated dashboard token** (only if it doesn't
  exist — your secrets are never overwritten),
- **ensures `BOT_AUTO_START=true`** so the service boots straight into
  RUNNING/ONLINE (it fixes a pre-existing `.env` copied from `.env.example`
  without touching any other key),
- installs a LaunchAgent (`~/Library/LaunchAgents/com.finance.signal.plist`)
  that auto-starts on login and auto-restarts on failure,
- runs the **preflight** check (real Yahoo/Discord/config verification).

After it finishes the bot is already running **ONLINE** — no browser, no manual
START. After any reboot / power-loss recovery, launchd starts the process and it
comes up ONLINE automatically, connects to Yahoo delayed ES/NQ, and begins
processing closed candles.

### Autostart on/off

Autostart is ON by default for the 24/7 service. To change it:

```bash
bash deploy/macos/ctl.sh autostart on     # boot ONLINE (default)
bash deploy/macos/ctl.sh autostart off    # boot OFFLINE; press START in the dashboard
bash deploy/macos/ctl.sh autostart        # show current setting
```

`STOP` and `PAUSE` from the dashboard keep working at runtime. Note: they are
in-memory controls — if the process is later restarted (crash or reboot) the
service comes back ONLINE, which is the whole point of a 24/7 deployment.

### Make it a true 24/7 server (do this once)

```bash
# Never sleep while plugged in (a sleeping Mac stops the bot):
sudo pmset -c sleep 0 disksleep 0
```
Then in **System Settings**:
- **Users & Groups → Automatic login → ON** (so the LaunchAgent loads after a reboot).
- **Energy → Start up automatically after a power failure → ON**.

---

## 2. Daily commands

All via the helper `deploy/macos/ctl.sh`:

```bash
bash deploy/macos/ctl.sh status     # launchd state + pid + dashboard URL
bash deploy/macos/ctl.sh health     # hit /api/health and print the dashboard link
bash deploy/macos/ctl.sh logs       # live tail of the logs (Ctrl-C to stop)
bash deploy/macos/ctl.sh stop       # graceful stop
bash deploy/macos/ctl.sh start      # start again
bash deploy/macos/ctl.sh restart    # restart (e.g. after editing .env)
bash deploy/macos/ctl.sh update     # git pull + deps + restart
```

Raw launchd equivalents (if you prefer):

```bash
launchctl print gui/$(id -u)/com.finance.signal          # status
launchctl kickstart -k gui/$(id -u)/com.finance.signal   # restart
launchctl bootout  gui/$(id -u)/com.finance.signal       # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.finance.signal.plist  # start
```

---

## 3. Logs

- Process stdout/err: `~/Library/Logs/finance-signal/dashboard.out.log` and
  `dashboard.err.log`.
- App log (rotating, timestamped): the path in `BOT_LOG_FILE` (default
  `~/Library/Logs/finance-signal/bot.log`).

```bash
tail -f ~/Library/Logs/finance-signal/bot.log
```

---

## 4. Check it's really working

```bash
bash deploy/macos/ctl.sh health
# {"ok":true,"status":"ONLINE","connection":"CONNECTED","symbols":["ES","NQ"],...}
```
Then open the printed URL on the Mac. In the dashboard:
- The feed chip is **blue `DELAYED`** when using real Yahoo data, or
  **amber `SIMULATED`** (with a warning banner) if Yahoo is unreachable and it
  fell back to sample data — the two can never be confused.
- ES/NQ tabs, live chart, current price, VWAP/EMA/liquidity/structure, signals
  with Entry/SL/TP and WIN/LOSS/ACTIVE status, and the history table.

Re-run the deep check anytime:
```bash
( set -a; . .env; set +a; PYTHONPATH=. .venv/bin/python scripts/preflight.py )
```

---

## 5. iPhone access via Tailscale (private, not public)

The dashboard binds to **loopback only** (`BOT_MODE=local`, `127.0.0.1`) — it is
never exposed to your LAN or the internet. Tailscale reaches it privately.

**On the Mac:**
```bash
brew install --cask tailscale     # or download the app
sudo tailscale up
tailscale serve --bg 8000         # publish 127.0.0.1:8000 to YOUR tailnet only (HTTPS)
tailscale serve status            # shows the https URL (MagicDNS name)
```

**On the iPhone:**
1. Install the **Tailscale** app, sign in with the same account.
2. Open the HTTPS URL from `tailscale serve status`, e.g.
   `https://mac-mini.your-tailnet.ts.net/?token=<DASHBOARD_TOKEN>`
   (get the token with `grep DASHBOARD_TOKEN .env`). The token is remembered
   after the first visit.

Only your own Tailscale devices can reach it; the token is a second layer.
Nothing is opened to the public internet, and no ports are forwarded on your
router.

> Simpler quick test without `serve`: `tailscale ip -4` on the Mac, then set
> `BOT_HOST=0.0.0.0` in `.env` and `restart`, and browse
> `http://<tailscale-ip>:8000/?token=...`. `tailscale serve` is preferred
> because it keeps the bind on loopback and adds HTTPS.

---

## 6. Update from GitHub

```bash
cd ~/FINANCE            # your clone
bash deploy/macos/ctl.sh update
```

---

## 7. Uninstall

```bash
bash deploy/macos/uninstall.sh    # removes the LaunchAgent; keeps repo/.venv/.env/logs
```

---

## Security summary

- Secrets (`DASHBOARD_TOKEN`, `DISCORD_WEBHOOK_URL`) live only in `.env`
  (chmod 600, git-ignored) and are loaded as environment variables — never
  hard-coded, never written into the plist, never returned by the API.
- Auth token enforced on all `/api/*` except the unauthenticated, secret-free
  `/api/health`.
- Bind is loopback; remote access is via the Tailscale tailnet only.
- CORS is off unless you explicitly set `BOT_CORS_ORIGINS` (wildcard rejected).
- No inbound router ports are opened.
