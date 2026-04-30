FROM python:3.12-slim

# Install Chromium + chromedriver so undetected-chromedriver can find them
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_EXECUTABLE_PATH=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV UC_HEADLESS=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 1 worker keeps the APScheduler thread alive; 300s timeout covers long scrapes
CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1 --timeout 300"
