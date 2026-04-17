-- RE:DD Schema V2 — AI Market Analysis columns
-- รันบน Supabase SQL Editor เพื่อเพิ่ม Perplexity analysis storage

-- ── 1. เพิ่ม columns ──────────────────────────────────────────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS ai_analysis      JSONB,
  ADD COLUMN IF NOT EXISTS ai_analyzed_at   TIMESTAMPTZ;

-- ── 2. Index สำหรับ query "deals ที่ยังไม่ analyze" ──────────────────────────
CREATE INDEX IF NOT EXISTS idx_deals_ai_analyzed
  ON deals (ai_analyzed_at)
  WHERE ai_analyzed_at IS NULL;

-- ── 3. Index จังหวัด (ช่วย filter province บน dashboard) ────────────────────
-- location field: "สงขลา อำเภอสะเดา" → extract first word
CREATE INDEX IF NOT EXISTS idx_deals_province
  ON deals (split_part(location, ' ', 1))
  WHERE location IS NOT NULL;

-- ── 4. View: deals_pending_analysis ─────────────────────────────────────────
-- deals ที่ต้องการ AI analysis: มี project_name หรือ location, ยังไม่ analyze
CREATE OR REPLACE VIEW deals_pending_analysis AS
SELECT
  id, listing_url, source_domain, property_type,
  project_name, location, price, area_sqm, land_area_sqm,
  roi_percent, priority, scraped_at
FROM deals
WHERE ai_analyzed_at IS NULL
  AND (project_name IS NOT NULL OR location IS NOT NULL)
  AND price > 0
ORDER BY
  CASE priority WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
  scraped_at DESC;

-- ── 5. View: deals_with_analysis ─────────────────────────────────────────────
CREATE OR REPLACE VIEW deals_with_analysis AS
SELECT
  d.*,
  (d.ai_analysis->>'roi_percent')::NUMERIC        AS ai_roi_percent,
  (d.ai_analysis->'price_before_reno'->>'min')::BIGINT AS ai_price_min,
  (d.ai_analysis->'price_before_reno'->>'max')::BIGINT AS ai_price_max,
  (d.ai_analysis->'price_after_reno'->>'min')::BIGINT  AS ai_reno_price_min,
  (d.ai_analysis->'price_after_reno'->>'max')::BIGINT  AS ai_reno_price_max,
  (d.ai_analysis->'reno_cost'->>'max')::BIGINT         AS ai_reno_cost_max,
  d.ai_analysis->>'summary_th'                    AS ai_summary,
  (d.ai_analysis->'target_income_monthly'->>'min')::BIGINT AS ai_target_income_min,
  (d.ai_analysis->'target_income_monthly'->>'max')::BIGINT AS ai_target_income_max
FROM deals d
WHERE d.ai_analyzed_at IS NOT NULL;
