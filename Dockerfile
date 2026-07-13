# Minimal image for running the Telegram bot (Python only, PostgreSQL is external)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install Python dependencies from pyproject.toml.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# Copy application code
COPY resources ./resources
COPY alembic.ini ./alembic.ini

# Runtime configuration is deliberately not baked into the image. Docker Compose
# mounts the operator-owned /app/config.json as a read-only file.
# Expose no ports; the bot connects out to Telegram
CMD ["fogmoe-bot"]
