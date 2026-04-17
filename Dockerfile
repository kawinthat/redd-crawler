FROM python:3.11-slim

# Minimal system deps (no Playwright fonts — using REST API harvester, not browser)
RUN apt-get update && apt-get install -y \
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Skip playwright chromium install — KrungthaiHarvester uses REST API, no browser needed

# Copy source
COPY . .

# Expose port
EXPOSE 8000

# Start FastAPI server
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
