# ==============================================================================
# StreamForge IPTV Studio - Production 24/7 Cloud Docker Container
# Compatible with Render, Koyeb, Hugging Face Spaces, Fly.io, Railway, and VPS
# ==============================================================================

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install runtime utilities & Playwright/Node prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    wget \
    ca-certificates \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies & Playwright headless chromium
COPY requirements.txt .
RUN mkdir -p /ms-playwright && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium && \
    chmod -R 777 /ms-playwright && \
    rm -rf /var/lib/apt/lists/*

# Copy application source
COPY . .

# Ensure storage directories exist
RUN mkdir -p /app/data /app/static

# Persistent data volume for SQLite databases and caches
VOLUME ["/app/data"]

EXPOSE 8080

# Start StreamForge IPTV Studio
CMD ["python", "app.py"]
