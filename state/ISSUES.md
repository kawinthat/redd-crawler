# Issues Log
> Cowork เพิ่ม entry เมื่อพบปัญหา — อัปเดต 2026-04-18

---

## [2026-04-18] ISS-005 — DEALS_COLUMNS ขาด migration 006 fields

- **Phase:** Production
- **Severity:** CRITICAL
- **Symptom:** หลัง analyze ด้วย Sonar Pro, ข้อมูล market_value_min/max, roi_min/max, price tiers, project info ไม่ถูกบันทึกเมื่อ crawl upsert ทับ
- **Root cause:** db_writer.py DEALS_COLUMNS frozenset ไม่มี 15+ fields จาก migration 006 → `upsert_deal()` strip fields เหล่านี้ออก
- **Fix:** เพิ่มทุก field ใน DEALS_COLUMNS (commit 4b0688d)
- **Status:** FIXED (2026-04-18) — commit 4b0688d

---

## [2026-04-18] ISS-006 — ROI พุ่งสูงเมื่อ area_sqm = 0

- **Phase:** Production
- **Severity:** HIGH
- **Symptom:** deals ที่ไม่มีข้อมูลพื้นที่ (area_sqm=0) → reno_cost=0 → ROI เกิน 100% ทันที
- **Root cause:** `_calc_roi` ใช้ `area_sqm * 5000` = 0 เมื่อไม่รู้พื้นที่
- **Fix:** fallback reno = `buy_price * 15%` เมื่อ area=0 (commit 4b0688d)
- **Status:** FIXED (2026-04-18) — commit 4b0688d

---

## [2026-04-18] ISS-007 — buy_price ไม่ถูก set สำหรับ NPA harvesters

- **Phase:** Production
- **Severity:** HIGH
- **Symptom:** orchestrator ไม่ pass `buy_price` → analyzer ต้อง fallback ไป `price` แต่ไม่ชัดเจน
- **Root cause:** `_normalize()` ไม่ pass harvester-provided `buy_price` และไม่ auto-set สำหรับ NPA
- **Fix:** orchestrator.py pass `buy_price` จาก harvester ถ้ามี, else auto-set สำหรับ bank_npa/enforcement (commit 406287e)
- **Status:** FIXED (2026-04-18) — commit 406287e

---

## [2026-04-18] ISS-008 — HOT count ใน src stats ใช้ roi_percent เก่า

- **Phase:** Production
- **Severity:** MEDIUM
- **Symptom:** hot count per-source ใน source cards ไม่ตรงกับ HOT badge หลัก
- **Root cause:** `computeSrcStats()` ใช้ `d.roi_percent >= 30` แต่ HOT badge ใช้ `calcFreshRoi + roi_data_source`
- **Fix:** เปลี่ยน computeSrcStats() ให้ consistent กับ HOT logic หลัก (commit 406287e)
- **Status:** FIXED (2026-04-18) — commit 406287e

---

## [2026-04-18] ISS-009 — AVG ROI รวม unanalyzed deals

- **Phase:** Production
- **Severity:** MEDIUM
- **Symptom:** AVG ROI บน dashboard นับ deals ที่ยังไม่ได้ AI analyze (roi_percent จาก crawl estimate)
- **Root cause:** `validRois` filter ไม่กรอง `roi_data_source`
- **Fix:** เพิ่ม `roi_data_source === 'sonar_pro' || 'cache'` filter ใน AVG ROI calc (commit 406287e)
- **Status:** FIXED (2026-04-18) — commit 406287e

---

## [2026-04-18] ISS-010 — usable_area_sqm ไม่ถูก fetch สำหรับ analyze

- **Phase:** Production
- **Severity:** MEDIUM
- **Symptom:** condo deals ที่เก็บพื้นที่ใน usable_area_sqm ถูกคำนวณ reno ด้วย fallback (buy*15%) แทนที่จะใช้ sqm*5000
- **Root cause:** FIELDS query ใน analyze endpoint ไม่รวม usable_area_sqm
- **Fix:** เพิ่ม usable_area_sqm ใน FIELDS + ปรับ priority order ใน analyzer (commit 29d0fa9)
- **Status:** FIXED (2026-04-18) — commit 29d0fa9

---

## [2026-04-18] ISS-002 — ปุ่ม "ล้าง AI" ไม่ล้างข้อมูล

- **Phase:** Production (deployed on Render)
- **Severity:** HIGH
- **Symptom:** กดปุ่ม "ล้าง AI" แล้ว refresh — ข้อมูล AI analysis ยังอยู่เหมือนเดิม
- **Root cause (สันนิษฐาน):**
  1. git ยังไม่ถูก push → deployed code บน Render ไม่มี `/deals/reset-analysis` endpoint (เพิ่งเขียนใหม่)
  2. หรือ Supabase `update().neq("id", 0)` ทำงานแต่ไม่ return data → `cleared=0` → ไม่รู้ว่าสำเร็จหรือไม่
  3. หรือ frontend ไม่ได้ refetch หลัง reset สำเร็จ
- **Fix needed:**
  1. Push git → redeploy Render
  2. ตรวจสอบว่า Supabase update bulk ทำงานจริงไหม
  3. หลัง reset สำเร็จ → frontend reload deals ใหม่อัตโนมัติ
- **Status:** FIXED (2026-04-18) — commit 786c3d5

---

## [2026-04-18] ISS-003 — AVG ROI แสดง 120% (สูงผิดปกติ)

- **Phase:** Production
- **Severity:** MEDIUM
- **Symptom:** dashboard แสดง AVG ROI = 120.0% ซึ่งสูงเกินจริง
- **Root cause:** Sonar Pro บางครั้ง return ราคา ฿/ตร.ม. มาเป็นราคารวม → ROI พุ่งสูง
  sanity check `roi_max > 200%` ดักได้บางส่วน แต่ case ที่ 100-200% หลุดผ่าน
- **Fix:** perplexity_analyzer.py roi_max > 200 → 120 + frontend exclude roi > 120% จาก AVG
- **Status:** FIXED (2026-04-18) — commit 786c3d5

---

## [2026-04-18] ISS-004 — LED Harvester คืน 0 listings

- **Phase:** Production
- **Severity:** MEDIUM
- **Symptom:** scan LED [asset.led.go.th] → pages=0 saved=0 ทุกครั้ง
- **Root cause:** Playwright ต้องใช้ `networkidle` wait ให้ JS set CAPTCHA ก่อน
  code fix เขียนแล้วใน led_harvester_v2.py แต่ยังไม่ push
- **Fix:** networkidle wait + 5s poll loop ใน led_harvester_v2.py (push 2026-04-18)
- **Status:** PENDING VERIFY — ทดสอบใน dashboard หลัง Render deploy

---

## [2026-04-17] ISS-001 — Playwright install ใน sandbox

- **Phase:** Phase 1
- **Severity:** LOW
- **Resolution:** ใช้ Render Docker image ที่มี Playwright built-in
- **Status:** RESOLVED
