FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Install Python dependencies
# Bump PIP_CACHEBUST whenever requirements.txt changes — Railway caches the
# pip layer and will otherwise reuse a stale install (see Known Gotchas).
ARG PIP_CACHEBUST=2026-05-18
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (already has dependencies)
RUN playwright install chromium

# Copy application code
COPY . .

# Expose port
ENV TZ="America/New_York"
EXPOSE 8080

# Start application (using shell form to expand $PORT)
CMD ["sh", "-c", "uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
