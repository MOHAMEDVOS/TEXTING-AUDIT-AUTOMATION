FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (already has dependencies)
RUN playwright install chromium

# Copy application code
COPY . .

# Expose port
EXPOSE 8080

# Start application (using shell form to expand $PORT)
CMD ["sh", "-c", "uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
