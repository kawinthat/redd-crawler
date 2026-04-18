# Issues Log
> Cowork เพิ่ม entry เมื่อพบปัญหา — อัปเดต 2026-04-18

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
