"""
market_cache.py — RE:DD Market Pattern Cache
Caches Sonar Pro results to avoid repeated AI calls for similar properties.

Two-level cache:
  1. Project-level:  MD5("project:{project_name}:{property_type}") — most specific
                     ใช้เมื่อ project_name ยาว ≥6 ตัวอักษร
  2. District-level: MD5("area:{province}:{district}:{property_type}") — broader
                     ใช้เมื่อไม่มีชื่อโครงการ หรือ project_name ว่าง

Cache TTL: 90 วัน
Cache hit → reuse market price per sqm, recalc ROI via script (no AI call)
roi_data_source = "cache" (ต่างจาก "sonar_pro" ที่เป็นการวิเคราะห์ใหม่)
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from loguru import logger

CACHE_TTL_DAYS     = 90
RENO_COST_PER_SQM  = 5_000
TRANSFER_FEE_RATE  = 0.055


# ──────────────────────────────────────────────────────────────────
# Cache key helpers
# ──────────────────────────────────────────────────────────────────

def _parse_location(location: str) -> tuple[str, str]:
    """แยก 'district, province' จาก location string — returns (province, district)."""
    parts = [p.strip() for p in (location or "").split(",")]
    if len(parts) >= 2:
        return parts[1], parts[0]   # province, district
    return (parts[0] if parts else ""), ""


def make_cache_keys(deal: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (project_key, district_key) for a deal.

    project_key:  MD5("project:{project_name}:{property_type}")[:20]
                  None ถ้า project_name สั้นกว่า 6 ตัว (ไม่เชื่อถือพอ)
    district_key: MD5("area:{province}:{district}:{property_type}")[:20]
                  None ถ้าไม่มีทั้ง province และ district
    """
    ptype   = (deal.get("property_type") or "other").lower().strip()
    project = (deal.get("project_name") or "").strip()
    province, district = _parse_location(deal.get("location") or "")

    # Project key (only when project name is meaningful)
    proj_key = None
    if len(project) >= 6:
        raw = f"project:{project.lower()}:{ptype}"
        proj_key = hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]

    # District key
    dist_key = None
    if province or district:
        raw2 = f"area:{province.lower()}:{district.lower()}:{ptype}"
        dist_key = hashlib.md5(raw2.encode("utf-8")).hexdigest()[:20]

    return proj_key, dist_key


# ──────────────────────────────────────────────────────────────────
# ROI recalculation (script-based, no AI)
# ──────────────────────────────────────────────────────────────────

def recalc_roi_from_pattern(deal: dict, pattern_prices: dict) -> dict:
    """
    Recalculate ROI using cached market prices.
    Script-only — no AI call needed.

    Args:
        deal:           deal dict (needs price, area_sqm / land_area_sqm, property_type)
        pattern_prices: {
            "market_price_sqm_before": int or None,
            "market_price_sqm_after":  int or None,
            "source_urls":             list[str] or None,
        }

    Returns:
        enrichment dict ready to merge into deal — roi_data_source = "cache"
    """
    area_sqm  = deal.get("area_sqm") or deal.get("land_area_sqm") or 0
    buy_price = deal.get("price") or 0
    ptype     = (deal.get("property_type") or "other").lower()

    mkt_before_sqm = pattern_prices.get("market_price_sqm_before")
    mkt_after_sqm  = pattern_prices.get("market_price_sqm_after")

    enrichment: dict[str, Any] = {
        "roi_data_source": "cache",
        "ai_analyzed_at":  datetime.now(timezone.utc).isoformat(),
    }

    # Always carry source_urls from cache
    src_urls = pattern_prices.get("source_urls")
    if src_urls:
        enrichment["source_urls"] = src_urls

    if not area_sqm or not buy_price:
        return enrichment

    # ── Market values ────────────────────────────────────────────
    if mkt_before_sqm and mkt_before_sqm > 0:
        enrichment["market_value_before_reno"] = int(mkt_before_sqm * area_sqm)

    market_value_after = None
    if mkt_after_sqm and mkt_after_sqm > 0:
        market_value_after = int(mkt_after_sqm * area_sqm)
        enrichment["market_value"]     = market_value_after
        enrichment["market_price_sqm"] = int(mkt_after_sqm)

    # ── ROI calc ─────────────────────────────────────────────────
    if market_value_after and buy_price > 0 and area_sqm > 0:
        reno_total   = area_sqm * RENO_COST_PER_SQM if ptype != "land" else 0
        transfer_fee = buy_price * TRANSFER_FEE_RATE
        total_cost   = buy_price + reno_total + transfer_fee
        profit       = market_value_after - total_cost
        roi_pct      = (profit / total_cost) * 100

        enrichment["reno_cost_total"]  = round(reno_total)
        enrichment["reno_cost_sqm"]    = RENO_COST_PER_SQM if ptype != "land" else 0
        enrichment["transfer_fee"]     = round(transfer_fee)
        enrichment["total_cost"]       = round(total_cost)
        enrichment["estimated_profit"] = round(profit)
        enrichment["roi_percent"]      = round(roi_pct, 2)
        enrichment["roi_valid"]        = True

        if roi_pct >= 30:
            enrichment["roi_flag"] = "🟢 ควรซื้อ"
            enrichment["priority"] = "HIGH"
        elif roi_pct >= 15:
            enrichment["roi_flag"] = "🟡 พิจารณา"
            enrichment["priority"] = "MEDIUM"
        else:
            enrichment["roi_flag"] = "🔴 ข้ามไป"
            enrichment["priority"] = "LOW"

    return enrichment


# ──────────────────────────────────────────────────────────────────
# MarketCache class
# ──────────────────────────────────────────────────────────────────

class MarketCache:
    """
    Supabase-backed market pattern cache.

    Usage:
        cache = MarketCache()
        hit = await cache.get_pattern(deal)
        if hit:
            return hit   # roi_data_source = "cache", no AI call
        enrichment = await analyzer.analyze_deal(deal)
        if enrichment:
            await cache.save_pattern(deal, enrichment)
        return enrichment
    """

    def __init__(self, client=None):
        """Pass Supabase client directly, or None to auto-create from env."""
        self._client = client if client is not None else self._make_client()

    def _make_client(self):
        try:
            from supabase import create_client
            url = os.getenv("SUPABASE_URL")
            key = os.getenv("SUPABASE_KEY")
            if url and key:
                return create_client(url, key)
        except Exception as e:
            logger.warning(f"MarketCache: cannot create Supabase client — {e}")
        return None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ── Read ──────────────────────────────────────────────────────

    async def get_pattern(self, deal: dict) -> Optional[dict]:
        """
        Look up cache for this deal.

        Tries project-level key first (exact match), then district-level (broader).
        Returns enrichment dict on hit (roi_data_source="cache"), None on miss.
        """
        if not self.enabled:
            return None

        proj_key, dist_key = make_cache_keys(deal)
        now = datetime.now(timezone.utc)

        for key, level in [(proj_key, "project"), (dist_key, "district")]:
            if not key:
                continue
            try:
                result = (
                    self._client.table("market_patterns")
                    .select(
                        "cache_key, cache_level, market_price_sqm_before, "
                        "market_price_sqm_after, source_urls, sample_count, "
                        "project_name, province, district"
                    )
                    .eq("cache_key", key)
                    .gt("expires_at", now.isoformat())
                    .limit(1)
                    .execute()
                )
                if result.data:
                    rec = result.data[0]
                    pattern_prices = {
                        "market_price_sqm_before": rec.get("market_price_sqm_before"),
                        "market_price_sqm_after":  rec.get("market_price_sqm_after"),
                        "source_urls":             rec.get("source_urls") or [],
                    }
                    enrichment = recalc_roi_from_pattern(deal, pattern_prices)
                    logger.info(
                        f"📦 Cache HIT ({level}) key={key[:12]}… "
                        f"project={rec.get('project_name') or rec.get('district','-')} "
                        f"ROI={enrichment.get('roi_percent','?')}%"
                    )
                    # Bump sample_count (best-effort, non-blocking)
                    try:
                        new_count = (rec.get("sample_count") or 1) + 1
                        self._client.table("market_patterns") \
                            .update({"sample_count": new_count}) \
                            .eq("cache_key", key) \
                            .execute()
                    except Exception:
                        pass
                    return enrichment

            except Exception as e:
                logger.warning(f"MarketCache.get_pattern ({level}): {e}")

        return None   # cache miss

    # ── Write ─────────────────────────────────────────────────────

    async def save_pattern(self, deal: dict, enrichment: dict) -> None:
        """
        Persist a Sonar Pro result to the market_patterns table.
        Saves both project-level and district-level rows (upsert).

        Call this only after a successful sonar_pro analysis
        (enrichment["roi_data_source"] == "sonar_pro").
        """
        if not self.enabled:
            return

        # ── Extract per-sqm prices ──────────────────────────────
        ai = enrichment.get("ai_analysis") or {}

        mkt_before_sqm: Optional[int] = None
        mkt_after_sqm:  Optional[int] = None

        # Prefer explicit per-sqm from Sonar Pro JSON
        if ai.get("market_price_sqm_before_reno"):
            mkt_before_sqm = int(ai["market_price_sqm_before_reno"])
        if ai.get("market_price_sqm_after_reno"):
            mkt_after_sqm = int(ai["market_price_sqm_after_reno"])

        # Fallback: divide total by area
        area = deal.get("area_sqm") or deal.get("land_area_sqm") or 0
        if not mkt_before_sqm and enrichment.get("market_value_before_reno") and area > 0:
            mkt_before_sqm = int(enrichment["market_value_before_reno"] / area)
        if not mkt_after_sqm and enrichment.get("market_value") and area > 0:
            mkt_after_sqm = int(enrichment["market_value"] / area)

        # Nothing to cache if no prices found
        if not mkt_before_sqm and not mkt_after_sqm:
            logger.debug("MarketCache.save_pattern: no market prices — skipped")
            return

        proj_key, dist_key = make_cache_keys(deal)
        province, district = _parse_location(deal.get("location") or "")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS)
        ).isoformat()

        source_urls    = enrichment.get("source_urls") or ai.get("data_sources") or []
        comparable     = ai.get("comparable_projects") or []

        base: dict[str, Any] = {
            "property_type":          deal.get("property_type"),
            "province":               province,
            "district":               district,
            "market_price_sqm_before": mkt_before_sqm,
            "market_price_sqm_after":  mkt_after_sqm,
            "source_urls":             source_urls,
            "comparable_projects":     comparable,
            "sample_count":            1,
            "analyzed_at":             datetime.now(timezone.utc).isoformat(),
            "expires_at":              expires_at,
            "raw_sonar_response":      ai if ai else None,
        }

        rows_to_save = []
        if proj_key:
            rows_to_save.append((proj_key, "project", deal.get("project_name") or ""))
        if dist_key:
            rows_to_save.append((dist_key, "district", ""))

        for key, level, proj_name in rows_to_save:
            record: dict[str, Any] = {**base, "cache_key": key, "cache_level": level}
            if level == "project" and proj_name:
                record["project_name"] = proj_name

            try:
                self._client.table("market_patterns") \
                    .upsert(record, on_conflict="cache_key") \
                    .execute()
                logger.info(
                    f"💾 Cache SAVE ({level}) key={key[:12]}… "
                    f"before={mkt_before_sqm} after={mkt_after_sqm} ฿/ตร.ม."
                )
            except Exception as e:
                logger.warning(f"MarketCache.save_pattern ({level}): {e}")
