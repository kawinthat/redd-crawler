-- Migration 006: เพิ่ม columns สำหรับ Rich AI Analysis (4-section report)
-- รัน: Supabase Dashboard → SQL Editor → paste แล้ว Run
-- ปลอดภัย: ใช้ ADD COLUMN IF NOT EXISTS ทุก column

-- ── ราคาตลาด 3 ระดับ ────────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS price_original_low   BIGINT,  -- สภาพเดิม ไม่รีโนเวท (ต่ำสุด)
  ADD COLUMN IF NOT EXISTS price_original_high  BIGINT,  -- สภาพเดิม ไม่รีโนเวท (สูงสุด)
  ADD COLUMN IF NOT EXISTS price_good_low       BIGINT,  -- สภาพดี / ต่อเติมบ้าง (ต่ำสุด)
  ADD COLUMN IF NOT EXISTS price_good_high      BIGINT,  -- สภาพดี / ต่อเติมบ้าง (สูงสุด)
  ADD COLUMN IF NOT EXISTS price_reno_low       BIGINT,  -- รีโนเวทใหม่ พร้อมอยู่ (ต่ำสุด) ← ใช้คำนวณ ROI
  ADD COLUMN IF NOT EXISTS price_reno_high      BIGINT,  -- รีโนเวทใหม่ พร้อมอยู่ (สูงสุด) ← ใช้คำนวณ ROI
  ADD COLUMN IF NOT EXISTS rental_monthly_est   INTEGER; -- ค่าเช่าโดยประมาณต่อเดือน (บาท)

-- ── market value range ───────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS market_value_min     BIGINT,  -- มูลค่าตลาดหลังรีโนเวท (ต่ำสุด)
  ADD COLUMN IF NOT EXISTS market_value_max     BIGINT;  -- มูลค่าตลาดหลังรีโนเวท (สูงสุด)

-- ── ฿/ตร.ม. range ────────────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS market_price_sqm_min INTEGER,
  ADD COLUMN IF NOT EXISTS market_price_sqm_max INTEGER;

-- ── ROI range ─────────────────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS roi_min              NUMERIC(10,2),  -- ROI conservative (จาก price_reno_low)
  ADD COLUMN IF NOT EXISTS roi_max              NUMERIC(10,2);  -- ROI optimistic (จาก price_reno_high)

-- ── กำไร range ──────────────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS estimated_profit_max BIGINT;  -- กำไรสูงสุด (จาก price_reno_high - total_cost)

-- ── ข้อมูลโครงการ ────────────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS project_official_name TEXT,   -- ชื่อโครงการทางการ
  ADD COLUMN IF NOT EXISTS project_developer     TEXT,   -- ผู้พัฒนา/เจ้าของโครงการ
  ADD COLUMN IF NOT EXISTS project_address       TEXT;   -- ที่อยู่จริง (ตำบล อำเภอ จังหวัด รหัสไปรษณีย์)

-- ── แก้ roi_data_source CHECK constraint ให้รองรับ 'cache' ───────────────
-- ลบ constraint เก่า แล้วสร้างใหม่ที่รองรับ: estimate | sonar_pro | cache
ALTER TABLE deals
  DROP CONSTRAINT IF EXISTS deals_roi_data_source_check;

ALTER TABLE deals
  ADD CONSTRAINT deals_roi_data_source_check
    CHECK (roi_data_source IN ('estimate', 'sonar_pro', 'cache'));

-- ── Indexes ──────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_deals_market_value_min
  ON deals (market_value_min)
  WHERE market_value_min IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_deals_roi_min
  ON deals (roi_min DESC)
  WHERE roi_min IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_deals_price_reno_low
  ON deals (price_reno_low)
  WHERE price_reno_low IS NOT NULL;

-- ── อัปเดต hot_deals view ให้รวม columns ใหม่ ───────────────────────────
CREATE OR REPLACE VIEW hot_deals AS
SELECT
  id,
  listing_url,
  source_domain,
  source_type,
  property_type,
  project_name,
  project_official_name,
  project_developer,
  project_address,
  location,
  price,
  buy_price,
  area_sqm,
  land_area_sqm,
  bedrooms,
  bathrooms,
  condition,
  -- ROI
  roi_percent,
  roi_min,
  roi_max,
  roi_flag,
  roi_valid,
  priority,
  roi_data_source,
  -- กำไร
  estimated_profit,
  estimated_profit_max,
  -- ราคาตลาด 3 ระดับ
  price_original_low,
  price_original_high,
  price_good_low,
  price_good_high,
  price_reno_low,
  price_reno_high,
  rental_monthly_est,
  -- market value range
  market_value,
  market_value_min,
  market_value_max,
  market_value_before_reno,
  -- ฿/ตร.ม.
  market_price_sqm,
  market_price_sqm_min,
  market_price_sqm_max,
  -- ต้นทุน
  total_cost,
  reno_cost_total,
  reno_cost_sqm,
  transfer_fee,
  -- AI
  ai_analysis,
  ai_analyzed_at,
  source_urls,
  -- timestamps
  scraped_at,
  updated_at
FROM deals
WHERE priority = 'HIGH'
  AND roi_valid = TRUE
ORDER BY COALESCE(roi_min, roi_percent, 0) DESC;
