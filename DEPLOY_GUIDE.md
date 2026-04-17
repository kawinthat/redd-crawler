# RE:DD Crawler — Deploy Guide
> ขั้นตอนทั้งหมดที่ Krit ต้องทำเอง (ต้องการ browser + terminal)

---

## STEP 1 — Push Code ไป GitHub

เปิด Terminal แล้วรัน:

```bash
cd /Users/machd/Downloads/redd-handoff/redd-crawler
git push -u origin main
```

ถ้าขอ username/password:
- Username: `kawinthat`
- Password: **ใช้ Personal Access Token** (ไม่ใช่ password จริง)
  - ไปสร้างที่: https://github.com/settings/tokens/new
  - Scope ที่ต้องเลือก: `repo` (ทั้งหมด)
  - Copy token → paste เป็น password

---

## STEP 2 — Deploy บน Render.com

1. ไปที่ https://dashboard.render.com/new/web
2. เลือก **New Web Service**
3. เลือก **Deploy from GitHub**
4. Connect repo: `kawinthat/redd-crawler`
5. ตั้งค่า:
   - **Name**: `redd-crawler`
   - **Runtime**: Docker
   - **Region**: Singapore (Southeast Asia)
   - **Branch**: `main`
   - **Plan**: Starter ($7/mo)

6. เพิ่ม **Environment Variables** (คลิก Add Environment Variable):

| Key | Value |
|-----|-------|
| `OPENROUTER_API_KEY` | `sk-or-v1-9a32482...` (จาก .env) |
| `SUPABASE_URL` | `https://augdbueqlaomvhmwvsyp.supabase.co` |
| `SUPABASE_KEY` | service_role key (จาก .env) |
| `LINE_NOTIFY_TOKEN` | ดู Step 3 ด้านล่าง |
| `MAX_PAGES` | `56` |
| `MAX_LISTINGS` | `2000` |
| `CONCURRENCY` | `3` |

7. คลิก **Create Web Service**
8. รอ ~10 นาที (Render จะ build Docker image)

---

## STEP 3 — สร้าง LINE Notify Token

1. ไปที่ https://notify-bot.line.me/th/
2. Login → คลิก **สร้าง token**
3. ตั้งชื่อ: `RE:DD HOT DEALS`
4. เลือก chat: **1-on-1 chat with LINE Notify** (หรือ group)
5. Copy token → ใส่ใน Render.com env var `LINE_NOTIFY_TOKEN`

---

## STEP 4 — Test Endpoint

หลัง Render deploy เสร็จ ให้ test:

```bash
# Health check
curl https://redd-crawler.onrender.com/health

# Dry run (ไม่ save DB)
curl -X POST https://redd-crawler.onrender.com/scan \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true, "max_pages": 2, "max_listings": 10}'

# ดู status
curl https://redd-crawler.onrender.com/scan/status
```

---

## STEP 5 — เปิด Make.com Scheduler

Make.com scenario สร้างไว้แล้ว (ID: 4779755)

1. ไปที่ https://us2.make.com/
2. หา scenario: **"RE:DD — สแกนอสังหา ทุก 6 ชั่วโมง"**
3. ถ้า Render URL ไม่ใช่ `https://redd-crawler.onrender.com/scan` → แก้ URL ในโมดูล HTTP
4. คลิก **Activate** (toggle สีเขียว)

Scenario จะรันทุก 6 ชั่วโมง → POST /scan → ส่ง LINE alert ถ้าเจอ HOT DEAL (ROI > 30%)

---

## สรุป Checklist

- [ ] `git push -u origin main` จาก Terminal
- [ ] Render.com → New Web Service → Docker → set env vars → Deploy
- [ ] ทดสอบ `/health` endpoint
- [ ] สร้าง LINE Notify token → ใส่ใน Render env var
- [ ] เปิด Make.com scenario

**เมื่อทุก step เสร็จ → RE:DD ทำงาน 100% อัตโนมัติ 🚀**
