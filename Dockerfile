# ==============================================================================
# StreamForge IPTV Studio - Production 24/7 Cloud Docker Container
# Compatible with Render, Koyeb, Hugging Face Spaces, Fly.io, Railway, and VPS
# ==============================================================================

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Install runtime utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies & Playwright headless chromium
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

# Copy application source
COPY . .

# Ensure storage directories exist
RUN mkdir -p /app/data /app/static

# Persistent data volume for SQLite databases and caches
VOLUME ["/app/data"]

EXPOSE 8080

# Start StreamForge IPTV Studio
CMD ["python", "app.py"]
