# RE:DD Crawler — Master Task List
> Cowork อ่านไฟล์นี้ทุก session เพื่อรู้ว่าต้องทำอะไรต่อ
> อัปเดต `[ ]` → `[x]` เมื่อ task เสร็จ + บันทึก date

---

## 🔴 BLOCKING — ต้องทำก่อน (ตามลำดับ)

### PHASE 1 — Environment Setup ✅ DONE (2026-04-17)
- [x] **1.1** สร้าง project folder structure + git init (local repo ใน redd-handoff/redd-crawler/)
  - _หมายเหตุ:_ ยังไม่ได้ push ไป GitHub remote — Krit push เองได้เลย
- [x] **1.2** สร้าง `requirements.txt` และ `pyproject.toml` — 2026-04-17
- [x] **1.3** ตั้ง `.env.example` template — 2026-04-17
- [x] **1.4** `pip install` ทุก dependency ✅ — `playwright install chromium` ⚠️ ต้องรันบน machine จริง
  - _Test:_ `python -c "import playwright, anthropic, supabase, bs4; print('OK')"` → ✅ OK
- [x] **1.5** Copy code files จาก handoff package → redd-crawler/crawler/ — 2026-04-17
  - Files: `spider.py`, `extractor.py`, `orchestrator.py`, `schema.sql`, `db_writer.py` (ใหม่)

### PHASE 2 — Test Spider (led.go.th ก่อน)
- [ ] **2.1** สร้าง `test_spider.py` — test LinkHarvester กับ led.go.th
  - _Antigravity command:_ `02_crawler.md § Test`
  - _Expected:_ พบ listing URLs ≥ 10 รายการ
- [ ] **2.2** Debug JS detection — ตรวจว่า led.go.th ต้องใช้ Playwright ไหม
- [ ] **2.3** Test pagination — กด "ถัดไป" ได้จริง
- [ ] **2.4** Test กับ npa.krungthai.com
- [ ] **2.5** Test กับ ddproperty.com (JS-heavy)

### PHASE 3 — Test AI Extractor
- [ ] **3.1** สร้าง `fixtures/` — เก็บ sample HTML จากแต่ละเว็บ
  - _Antigravity command:_ `03_extractor.md § Fixtures`
- [ ] **3.2** Test batch extraction กับ 5 sample HTMLs
  - _Expected:_ ได้ JSON ที่มี price, area, location ≥ 80% accuracy
- [ ] **3.3** Verify ROI calculation — ทดสอบด้วยตัวเลขที่รู้คำตอบแล้ว
  - _Test case:_ price=1,650,000, area=120, condition=poor, location=ปทุมธานี → ROI ≈ 41%

### PHASE 4 — Setup Supabase
- [ ] **4.1** Krit สร้าง Supabase project (ต้องทำเอง)
- [ ] **4.2** รัน `schema.sql` บน Supabase SQL Editor
  - _Verify:_ tables `deals`, `scrape_cache`, `price_cache`, `crawl_jobs` มีอยู่
- [ ] **4.3** สร้าง `db_writer.py` — test upsert 1 dummy record
  - _Test:_ `python test_db.py` → record ปรากฏใน Supabase dashboard

### PHASE 5 — Integration Test (Dry Run)
- [ ] **5.1** สร้าง `--dry-run` flag ใน orchestrator.py
  - _Antigravity command:_ `05_orchestrator.md § DryRun`
- [ ] **5.2** รัน full pipeline dry run กับ led.go.th (max 2 pages, max 10 listings)
  - _Expected:_ log แสดง extracted data + ROI ที่ถูกต้อง แต่ไม่ save DB
- [ ] **5.3** รัน full pipeline จริง (max 5 pages)
  - _Expected:_ records ปรากฏใน Supabase `deals` table

---

## 🟡 IMPORTANT — ทำหลัง blocking เสร็จ

### PHASE 6 — Deploy
- [ ] **6.1** สร้าง `Dockerfile` และ `render.yaml`
  - _Antigravity command:_ `06_deploy.md § Render`
- [ ] **6.2** Set environment variables บน Render.com dashboard
- [ ] **6.3** Deploy และ test endpoint `POST /scan`
- [ ] **6.4** Setup Make.com scenario — trigger ทุก 6 ชั่วโมง
- [ ] **6.5** Test LINE Notify — ส่ง test alert

### PHASE 7 — Dashboard Integration
- [ ] **7.1** สร้าง FastAPI `GET /deals` endpoint
- [ ] **7.2** สร้าง `GET /deals/hot` endpoint (ROI > 30%)
- [ ] **7.3** Connect crawler-dashboard-v3.jsx กับ real API
- [ ] **7.4** Deploy frontend บน Lovable / Vercel

---

## 🟢 NICE TO HAVE — ทำเมื่อมีเวลา

- [ ] **8.1** Perplexity Sonar Pro integration — ราคา reno ตลาดจริง
- [ ] **8.2** Comparable Sales Engine — ใช้ข้อมูลตัวเองแทน Perplexity
- [ ] **8.3** Email digest รายสัปดาห์
- [ ] **8.4** Admin UI — เพิ่ม/ลบ target site
- [ ] **8.5** Multi-user auth — Supabase Auth
- [ ] **8.6** Export deals เป็น Excel

---

## ✅ COMPLETED

- [x] **Phase 1 — Environment Setup** (2026-04-17) — folder structure, code files, config files, db_writer.py, git init, pip install OK

---

## หมายเหตุ Cowork

ก่อนสั่ง Antigravity ทุกครั้ง:
1. อ่าน task นี้ใน `commands/antigravity/{phase}.md`
2. ตรวจว่า prerequisite task เสร็จแล้ว
3. ถ้า task fail → log ใน `state/ISSUES.md`
4. ถ้า task สำเร็จ → update checkbox + `PROJECT_STATE.json`
