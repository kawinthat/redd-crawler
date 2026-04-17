# RE:DD Autonomous Real Estate Arbitrage Scanner

> โดย กวินทัศน์ เมธีรัฐลีลากุล (Krit) — TITAN Financial Optimizer

ระบบ Python ที่โยน URL เดียวแล้ว crawl ทรัพย์อสังหาฯ อัตโนมัติ
คำนวณ ROI และแจ้งเตือน HOT DEALS (>30%) ผ่าน LINE Notify

---

## Quick Start

```bash
# 1. Copy env template
cp .env.example .env

# 2. Fill in your API keys in .env

# 3. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 4. Run a dry-run scan
python -m crawler.orchestrator --dry-run --url https://led.go.th/assets
```

## Stack

- **Scraping:** Playwright + httpx + BeautifulSoup4
- **AI Extraction:** Claude Haiku (claude-haiku-4-5)
- **Database:** Supabase (PostgreSQL)
- **Alerts:** LINE Notify
- **Scheduler:** Make.com
- **Hosting:** Render.com

## ROI Formula

```
ROI = (Market Value - Ask Price - Transfer Fee - Reno Cost) / Total Investment × 100
HOT DEAL threshold: ROI > 30%
```

## Target Sites (Priority Order)

1. กรมบังคับคดี — led.go.th/assets
2. Krungthai NPA — npa.krungthai.com
3. KBank Asset — kbankasset.com
4. SCB NPA — scbnpa.com
5. GH Bank — ghbhomecenter.com
6. DDProperty, Hipflat, Baania (marketplace)
