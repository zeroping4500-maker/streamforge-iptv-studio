# ==============================================================================
# ZEMYIPTV IPTV Studio - lightweight production container
# Runs on Render, Koyeb, Fly.io, Railway, Hugging Face Spaces and any small VPS.
# No apt packages needed: runtime is python:3.11-slim + stdlib + lazy-imported
# yt-dlp. Runs as non-root with a stdlib HTTP healthcheck.
#
# Full build (adds headless Chromium for Televizo extraction, larger image):
#   docker build --build-arg WITH_PLAYWRIGHT=1 .
# Weak-host build (default, ~500 MB smaller; extraction degrades gracefully):
#   docker build .
# ==============================================================================
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

ARG WITH_PLAYWRIGHT=0

COPY requirements.txt .
RUN if [ "$WITH_PLAYWRIGHT" = "1" ]; then \
        mkdir -p /ms-playwright && \
        pip install --no-cache-dir -r requirements.txt && \
        playwright install --with-deps chromium && \
        chmod -R 777 /ms-playwright; \
    else \
        pip install --no-cache-dir yt-dlp; \
    fi

COPY . .

RUN mkdir -p /app/data /app/static && \
    useradd --system --uid 10001 zemy && \
    chown -R zemy:zemy /app && \
    chmod 777 /app/data

USER zemy

# Persistent data volume for SQLite databases and caches
VOLUME ["/app/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/playlists' % os.environ.get('PORT','8080'), timeout=4)"]

# Start ZEMYIPTV IPTV Studio
CMD ["python", "app.py"]
