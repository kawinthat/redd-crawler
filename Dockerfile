FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

# ── System deps ─────────────────────────────────────────────────────────────
# Base image มี Chromium + ทุก dependency ครบแล้ว
# เพิ่มเฉพาะ Tesseract OCR สำหรับ LED CAPTCHA
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-tha \
    fonts-thai-tlwg fonts-noto fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright Chromium browser ──────────────────────────────────────────────
# Base image (mcr.microsoft.com/playwright/python) มี Chromium ติดมาแล้ว
# ไม่ต้อง playwright install อีก → ไม่มีปัญหา download timeout บน Render free tier
# PLAYWRIGHT_BROWSERS_PATH ถูก set เป็น /ms-playwright ใน base image อัตโนมัติ

# ── Source code ──────────────────────────────────────────────────────────────
COPY . .

# ── Healthcheck ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s \
  CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
