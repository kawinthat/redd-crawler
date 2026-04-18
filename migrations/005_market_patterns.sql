-- ============================================================
-- Migration 005 — Market Pattern Cache + source_urls
-- Run in Supabase SQL Editor (Dashboard → SQL Editor → New query)
-- ============================================================

-- ── 1. Create market_patterns cache table ──────────────────
CREATE TABLE IF NOT EXISTS market_patterns (
  id                      UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
  cache_key               TEXT         NOT NULL UNIQUE,
  cache_level             TEXT         CHECK (cache_level IN ('project', 'district')),
  property_type           TEXT,
  project_name            TEXT,
  province                TEXT,
  district                TEXT,
  market_price_sqm_before INTEGER,           -- ฿/ตร.ม. สภาพเดิม (ก่อนรีโนเวท)
  market_price_sqm_after  INTEGER,           -- ฿/ตร.ม. สภาพดี (หลังรีโนเวท)
  source_urls             JSONB        DEFAULT '[]'::jsonb,  -- URL อ้างอิงจาก Sonar Pro
  comparable_projects     JSONB        DEFAULT '[]'::jsonb,  -- โครงการเทียบเคียง
  sample_count            INTEGER      DEFAULT 1,            -- deals ที่ใช้ cache นี้
  confidence_score        NUMERIC(3,2),                      -- 0.00–1.00 (optional)
  analyzed_at             TIMESTAMPTZ  DEFAULT now(),
  expires_at              TIMESTAMPTZ  NOT NULL,             -- 90 วันจาก analyzed_at
  raw_sonar_response      JSONB                              -- JSON เต็มจาก Sonar Pro (debug)
);

-- Indexes for fast lookup
CREATE INDEX IF NOT EXISTS idx_market_patterns_cache_key
  ON market_patterns (cache_key);

CREATE INDEX IF NOT EXISTS idx_market_patterns_level
  ON market_patterns (cache_level);

CREATE INDEX IF NOT EXISTS idx_market_patterns_expires
  ON market_patterns (expires_at);

CREATE INDEX IF NOT EXISTS idx_market_patterns_province_district
  ON market_patterns (province, district, property_type);

-- ── 2. Add source_urls column to deals ─────────────────────
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS source_urls JSONB;

-- ── 3. Update roi_data_source constraint to include 'cache' ─
--    Drop old constraint, re-add with 'cache' value
ALTER TABLE deals
  DROP CONSTRAINT IF EXISTS deals_roi_data_source_check;

ALTER TABLE deals
  ADD CONSTRAINT deals_roi_data_source_check
    CHECK (roi_data_source IN ('estimate', 'sonar_pro', 'cache'));

-- ── 4. Index on roi_data_source = 'cache' for monitoring ───
CREATE INDEX IF NOT EXISTS idx_deals_roi_data_source_cache
  ON deals (roi_data_source)
  WHERE roi_data_source = 'cache';

-- ── 5. Optional: refresh hot_deals view if it exists ───────
--    (safe to skip if view was already updated in migration 004)
-- No changes needed — roi_data_source handled in deals table

-- ── Verify ─────────────────────────────────────────────────
-- SELECT COUNT(*) FROM market_patterns;
-- SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'deals' AND column_name IN ('source_urls','roi_data_source');
