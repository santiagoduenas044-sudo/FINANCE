# GHOST + ORBIT in the cloud (docker compose, see deploy/cloud/README.md).
# The image holds Python and the dependencies; the code is the repository
# folder mounted at /app, so "Buscar actualización e instalar" (git pull) and
# a restart update it like on the Mac. Paper trading only: no broker, no orders.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    BOT_IN_DOCKER=1

# git: the in-app update. Its credential (a read-only GitHub token, only when
# the repository is private) is mounted from the server, never in the image.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /app \
    && git config --system credential.helper "store --file=/run/ghost/git-credentials"

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
# a copy of the code too, so the image also runs on its own
COPY signal_engine/ ./signal_engine/
COPY scripts/ ./scripts/

ENV BOT_MODE=remote \
    BOT_HOST=0.0.0.0 \
    BOT_PORT=8000 \
    BOT_AUTO_START=true
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "scripts/run_dashboard.py"]
