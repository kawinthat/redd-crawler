-- RE:DD Migration: เพิ่ม dedup_key column + index
-- รันใน Supabase SQL Editor ครั้งเดียว

-- 1. เพิ่ม columns
ALTER TABLE deals
  ADD COLUMN IF NOT EXISTS dedup_key      TEXT,
  ADD COLUMN IF NOT EXISTS is_duplicate   BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS reno_cost_sqm  NUMERIC,
  ADD COLUMN IF NOT EXISTS transfer_fee   NUMERIC,
  ADD COLUMN IF NOT EXISTS market_price_sqm NUMERIC,
  ADD COLUMN IF NOT EXISTS buy_price      NUMERIC;

-- 2. Index สำหรับ fast dedup lookup
CREATE INDEX IF NOT EXISTS idx_deals_dedup_key
  ON deals (dedup_key)
  WHERE dedup_key IS NOT NULL;

-- 3. (Optional) backfill dedup_key สำหรับ records เก่าด้วย md5 ของ listing_url
-- UPDATE deals SET dedup_key = md5(listing_url)::text WHERE dedup_key IS NULL;

-- Done ✅
