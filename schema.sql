-- ═══════════════════════════════════════════════
-- RE:DD Autonomous Scanner — Supabase Schema
-- ═══════════════════════════════════════════════

-- ─── DEALS TABLE ────────────────────────────────
CREATE TABLE IF NOT EXISTS deals (
  id              BIGSERIAL PRIMARY KEY,

  -- Source
  listing_url     TEXT UNIQUE NOT NULL,
  source_domain   TEXT,
  source_type     TEXT CHECK (source_type IN (
                    'bank_npa','enforcement','private',
                    'developer','agent')),

  -- Property Info
  property_type   TEXT CHECK (property_type IN (
                    'condo','house','townhouse',
                    'land','commercial','other')),
  project_name    TEXT,
  address         TEXT,
  location        TEXT,
  title_deed      TEXT,
  bedrooms        INT,
  bathrooms       INT,
  floors          INT,
  condition       TEXT CHECK (condition IN ('new','good','fair','poor')),
  features        TEXT,
  auction_date    TEXT,
  contact         TEXT,

  -- Financials
  price           BIGINT,
  area_sqm        NUMERIC(10,2),
  usable_area_sqm NUMERIC(10,2),
  land_area_sqm   NUMERIC(10,2),

  -- ROI
  roi_valid       BOOLEAN DEFAULT FALSE,
  roi_percent     NUMERIC(8,2),
  roi_flag        TEXT,
  priority        TEXT CHECK (priority IN ('HIGH','MEDIUM','LOW')),
  estimated_profit BIGINT,
  total_cost      BIGINT,
  market_value    BIGINT,
  reno_cost_total BIGINT,

  -- Meta
  scraped_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  raw_data        JSONB,

  -- Indexes
  CONSTRAINT deals_price_positive CHECK (price IS NULL OR price > 0),
  CONSTRAINT deals_area_positive  CHECK (area_sqm IS NULL OR area_sqm > 0)
);

-- Indexes สำหรับ query เร็ว
CREATE INDEX IF NOT EXISTS idx_deals_priority    ON deals (priority);
CREATE INDEX IF NOT EXISTS idx_deals_roi         ON deals (roi_percent DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_deals_source      ON deals (source_domain);
CREATE INDEX IF NOT EXISTS idx_deals_location    ON deals (location);
CREATE INDEX IF NOT EXISTS idx_deals_scraped     ON deals (scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_deals_property    ON deals (property_type);


-- ─── SCRAPE CACHE TABLE ─────────────────────────
CREATE TABLE IF NOT EXISTS scrape_cache (
  url           TEXT PRIMARY KEY,
  content_hash  TEXT NOT NULL,
  etag          TEXT,
  last_modified TEXT,
  checked_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cache_checked ON scrape_cache (checked_at DESC);


-- ─── PRICE CACHE TABLE ──────────────────────────
CREATE TABLE IF NOT EXISTS price_cache (
  cache_key   TEXT PRIMARY KEY,
  value       JSONB NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);


-- ─── CRAWL JOBS TABLE ───────────────────────────
CREATE TABLE IF NOT EXISTS crawl_jobs (
  id          BIGSERIAL PRIMARY KEY,
  base_url    TEXT NOT NULL,
  status      TEXT DEFAULT 'pending'
              CHECK (status IN ('pending','running','done','failed')),
  config      JSONB,
  stats       JSONB,
  started_at  TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);


-- ─── USEFUL VIEWS ───────────────────────────────

-- HOT DEALS — ROI > 30%
CREATE OR REPLACE VIEW hot_deals AS
  SELECT
    listing_url, source_domain, source_type,
    property_type, location, address,
    price, area_sqm, condition,
    roi_percent, roi_flag, estimated_profit,
    total_cost, market_value, reno_cost_total,
    scraped_at
  FROM deals
  WHERE priority = 'HIGH'
    AND roi_valid = TRUE
  ORDER BY roi_percent DESC;


-- DEAL SUMMARY BY SOURCE
CREATE OR REPLACE VIEW deals_by_source AS
  SELECT
    source_domain,
    COUNT(*)                                  AS total,
    COUNT(*) FILTER (WHERE priority='HIGH')   AS hot,
    COUNT(*) FILTER (WHERE priority='MEDIUM') AS medium,
    AVG(roi_percent)                          AS avg_roi,
    MAX(roi_percent)                          AS max_roi,
    MAX(scraped_at)                           AS last_scraped
  FROM deals
  WHERE roi_valid = TRUE
  GROUP BY source_domain
  ORDER BY hot DESC;


-- ─── RLS POLICIES (ถ้าใช้ Auth) ─────────────────
-- ALTER TABLE deals ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Authenticated users can read deals"
--   ON deals FOR SELECT TO authenticated USING (true);


-- ─── AUTO UPDATE updated_at ──────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER deals_updated_at
  BEFORE UPDATE ON deals
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ─── CLEANUP EXPIRED CACHE ───────────────────────
CREATE OR REPLACE FUNCTION cleanup_expired_cache()
RETURNS void AS $$
BEGIN
  DELETE FROM price_cache WHERE expires_at < NOW();
  DELETE FROM scrape_cache
    WHERE checked_at < NOW() - INTERVAL '30 days';
END;
$$ LANGUAGE plpgsql;
