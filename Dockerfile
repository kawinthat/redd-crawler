FROM python:3.11-slim

# ── System deps ─────────────────────────────────────────────────────────────
# tesseract-ocr     → CAPTCHA solving for กรมบังคับคดี (LED)
# tesseract-ocr-tha → Thai language pack (ไม่บังคับ แต่ช่วย accuracy)
# Playwright deps   → Chromium headless browser for JS-heavy sites
RUN apt-get update && apt-get install -y \
    wget curl ca-certificates gnupg \
    # Tesseract OCR (for LED CAPTCHA)
    tesseract-ocr \
    tesseract-ocr-tha \
    # Playwright / Chromium system libs
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libxss1 libgtk-3-0 libasound2 \
    fonts-thai-tlwg fonts-noto fonts-liberation \
    # General utils
    libglib2.0-0 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright Chromium browser ──────────────────────────────────────────────
# ติดตั้ง Chromium สำหรับ sites ที่ต้องการ JS rendering (LED, GH Bank, etc.)
# NOTE: ไม่ใช้ 2>/dev/null || true เพราะถ้า install ล้มเหลว build ต้อง fail ให้เห็น
RUN playwright install chromium --with-deps

# ── Source code ──────────────────────────────────────────────────────────────
COPY . .

# ── Healthcheck ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
  CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
