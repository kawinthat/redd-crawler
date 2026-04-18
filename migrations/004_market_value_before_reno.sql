-- Migration 004: เพิ่ม market_value_before_reno และ roi_data_source
-- รัน: Supabase Dashboard → SQL Editor → paste แล้ว Run

-- ราคาตลาดก่อนรีโนเวท (สภาพเดิม) — ได้จาก Perplexity Sonar Pro
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS market_value_before_reno BIGINT;

-- แหล่งข้อมูล ROI: 'estimate' (fallback) หรือ 'sonar_pro' (real data)
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS roi_data_source TEXT
    CHECK (roi_data_source IN ('estimate', 'sonar_pro'))
    DEFAULT 'estimate';

-- Index สำหรับ filter deals ที่ยังใช้ค่า estimate (ต้องการ Sonar Pro analyze)
CREATE INDEX IF NOT EXISTS idx_deals_roi_data_source
  ON deals (roi_data_source)
  WHERE roi_data_source = 'estimate';

-- อัปเดต deals เก่าให้มี roi_data_source = 'estimate'
UPDATE deals
  SET roi_data_source = 'estimate'
  WHERE roi_data_source IS NULL;

-- View อัปเดต: เพิ่ม market_value_before_reno
CREATE OR REPLACE VIEW hot_deals AS
SELECT
  id,
  listing_url,
  source_domain,
  source_type,
  property_type,
  project_name,
  location,
  price,
  area_sqm,
  condition,
  roi_percent,
  roi_flag,
  priority,
  roi_data_source,
  estimated_profit,
  total_cost,
  market_value             AS market_value_after_reno,
  market_value_before_reno,
  market_price_sqm,
  reno_cost_total,
  reno_cost_sqm,
  transfer_fee,
  buy_price,
  ai_analysis,
  ai_analyzed_at,
  scraped_at,
  updated_at
FROM deals
WHERE priority = 'HIGH'
  AND roi_valid = TRUE
ORDER BY roi_percent DESC;

-- View summary stats อัปเดต
CREATE OR REPLACE VIEW deals_by_source AS
SELECT
  source_domain,
  COUNT(*)                            AS total,
  COUNT(*) FILTER (WHERE priority = 'HIGH')   AS hot,
  COUNT(*) FILTER (WHERE priority = 'MEDIUM') AS medium,
  COUNT(*) FILTER (WHERE roi_data_source = 'sonar_pro') AS sonar_analyzed,
  ROUND(AVG(roi_percent) FILTER (WHERE roi_valid), 2) AS avg_roi,
  MAX(scraped_at)                     AS last_scraped
FROM deals
GROUP BY source_domain
ORDER BY total DESC;
