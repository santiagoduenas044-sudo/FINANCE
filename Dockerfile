# 24/7 Futures Signal Engine — dashboard + bot loop (Phase 13).
# Simple, reliable single-stage image. No broker, no order execution.
FROM python:3.11-slim

# Faster, quieter, no .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY signal_engine/ ./signal_engine/
COPY scripts/ ./scripts/
COPY tests/ ./tests/

# Run as a non-root user; give it a writable data_store for the signal log.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data_store \
    && chown -R bot:bot /app
USER bot

# Serve on 0.0.0.0 inside the container (map/reverse-proxy on the host).
ENV BOT_MODE=remote \
    BOT_HOST=0.0.0.0 \
    BOT_PORT=8000 \
    BOT_AUTO_START=true \
    BOT_LOG_FILE=/app/data_store/bot.log

EXPOSE 8000

# Container-native health check hits the unauthenticated /api/health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else sys.exit(1)"

# Dashboard (includes the bot loop). For a headless bot with no UI, override
# the command with: python scripts/run_bot.py
CMD ["python", "scripts/run_dashboard.py"]
