# Minimal image for running the Telegram bot (Python only, MySQL is external)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY pyproject.toml .
COPY src ./src
COPY resources ./resources
COPY .env.example ./.env.example

# Expose no ports; the bot connects out to Telegram
CMD ["python", "-u", "-m", "fogmoe_bot"]
