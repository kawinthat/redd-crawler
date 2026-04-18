"""RE:DD Crawler — Supabase DB Writer
Handles upsert of deals and crawl job logging.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Any

from loguru import logger


# ─────────────────────────────────────────────
# DEDUP FINGERPRINT
# ─────────────────────────────────────────────

def make_dedup_key(deal: dict):
    """สร้าง fingerprint สำหรับตรวจ duplicate ข้ามไซต์ (95% similarity).

    Logic:
    - price bucket: ปัดเข้าหาหลัก 50,000 บาทที่ใกล้ที่สุด
    - area bucket : ปัดเข้าหาหลัก 5 ตร.ม. ที่ใกล้ที่สุด
    - property_type: ตรงตัว
    - province    : คำแรกของ location (เขต/จังหวัด)

    Returns 16-char hex string (MD5 prefix)
    หรือ None ถ้าข้อมูลไม่เพียงพอ — ข้าม dedup ใช้ listing_url เป็น unique key แทน
    """
    price = deal.get("price") or 0
    area  = (deal.get("area_sqm") or deal.get("usable_area_sqm")
             or deal.get("land_area_sqm") or 0)
    ptype = (deal.get("property_type") or "").strip()
    loc   = (deal.get("location") or "").strip().split()[0] if deal.get("location") else ""

    # Buckets — เผื่อ ±5%
    price_bucket = round(price / 50_000) if price else 0
    area_bucket  = round(area  / 5)      if area  else 0

    # ถ้าไม่มีข้อมูลพื้นที่หรือราคา → ไม่ dedup เพื่อไม่ให้ข้าม deals จริง
    if not price_bucket or not area_bucket:
        return None

    raw = f"{price_bucket}:{area_bucket}:{ptype}:{loc}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

try:
    from supabase import create_client, Client
except ImportError:
    logger.warning("supabase package not installed — DB writes disabled")
    Client = None  # type: ignore


def _get_client() -> "Client | None":
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_KEY not set — DB writes skipped")
        return None
    return create_client(url, key)


DEALS_COLUMNS = frozenset({
    "listing_url", "source_domain", "source_type",
    "property_type", "project_name", "address", "location",
    "title_deed", "bedrooms", "bathrooms", "floors",
    "condition", "features", "auction_date", "contact",
    "price", "area_sqm", "usable_area_sqm", "land_area_sqm",
    "roi_valid", "roi_percent", "roi_flag", "priority",
    "estimated_profit", "total_cost", "market_value", "reno_cost_total",
    "reno_cost_sqm", "transfer_fee", "market_price_sqm", "buy_price",
    "dedup_key", "is_duplicate",
    "scraped_at", "updated_at", "raw_data",
})


class SupabaseWriter:
    """Async-friendly Supabase writer for RE:DD crawler."""

    def __init__(self) -> None:
        self._client = _get_client()
        # In-scan dedup: track (dedup_key, source_domain) pairs seen this session
        self._seen_keys: dict[str, str] = {}   # dedup_key → first listing_url seen
        self._dedup_skipped: int = 0

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def reset_dedup(self) -> None:
        """เรียกต้นสแกนใหม่ทุกครั้ง เพื่อ clear in-memory dedup state."""
        self._seen_keys.clear()
        self._dedup_skipped = 0

    # ------------------------------------------------------------------ #
    # Deals
    # ------------------------------------------------------------------ #

    async def upsert_deal(self, deal: dict[str, Any]) -> bool:
        """Upsert a deal record into the `deals` table.

        Args:
            deal: Dict with keys matching the `deals` schema.
                  Required: listing_url, price, area_sqm, location, property_type
                  Optional: roi_percent, condition, source_domain, raw_data, ...

        Returns:
            True on success, False on failure (logs error, does not raise).
            Returns False (no error) for detected duplicates — use is_duplicate flag.
        """
        if not self.enabled:
            logger.debug("DB disabled — skipping upsert_deal")
            return False

        try:
            # ── Generate dedup fingerprint ──
            dkey = make_dedup_key(deal)
            deal["dedup_key"] = dkey  # None = ไม่มีข้อมูล area/price → ข้าม dedup

            if dkey is not None:
                # ── In-scan dedup: ตรวจ duplicate ใน scan เดียวกัน ──
                existing_url = self._seen_keys.get(dkey)
                if existing_url and existing_url != deal.get("listing_url", ""):
                    self._dedup_skipped += 1
                    logger.debug(
                        f"DEDUP skip — {deal.get('listing_url','')[:50]}\n"
                        f"  ≈ {existing_url[:50]} (key={dkey})"
                    )
                    return False   # skip duplicate, not an error

                # ── Register in seen map ──
                self._seen_keys[dkey] = deal.get("listing_url", "")

            # ── DB-level cross-scan dedup: ตรวจ dedup_key ใน DB (เฉพาะเมื่อมี key) ──
            if dkey is not None and self.enabled and deal.get("listing_url"):
                try:
                    existing = (
                        self._client.table("deals")
                        .select("listing_url, source_domain")
                        .eq("dedup_key", dkey)
                        .neq("listing_url", deal["listing_url"])
                        .limit(1)
                        .execute()
                    )
                    if existing.data:
                        ex = existing.data[0]
                        # Same source domain → let it upsert (price update)
                        # Different source domain → cross-site duplicate → skip
                        if ex.get("source_domain") != deal.get("source_domain"):
                            self._dedup_skipped += 1
                            logger.info(
                                f"CROSS-SITE DEDUP — skip {deal.get('source_domain','')} "
                                f"(ซ้ำกับ {ex.get('source_domain','')} url={ex.get('listing_url','')[:50]})"
                            )
                            return False
                except Exception:
                    pass  # DB dedup failed → proceed with upsert

            # ── Ensure timestamp ──
            deal.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

            # ── Strip fields not in the deals schema ──
            clean = {k: v for k, v in deal.items() if k in DEALS_COLUMNS and v is not None}

            self._client.table("deals") \
                .upsert(clean, on_conflict="listing_url") \
                .execute()
            logger.debug(f"upsert_deal OK — url={clean.get('listing_url', '?')[:60]}")
            return True
        except Exception as exc:
            logger.error(f"upsert_deal FAILED: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # Crawl Jobs
    # ------------------------------------------------------------------ #

    async def log_crawl_job(self, job: dict[str, Any]) -> bool:
        """Insert a crawl job record into the `crawl_jobs` table.

        Args:
            job: Dict with keys: site_url, status, pages_crawled,
                 listings_found, duration_seconds, error_message (optional)

        Returns:
            True on success, False on failure.
        """
        if not self.enabled:
            logger.debug("DB disabled — skipping log_crawl_job")
            return False

        try:
            job.setdefault("created_at", datetime.now(timezone.utc).isoformat())

            self._client.table("crawl_jobs").insert(job).execute()
            logger.debug(f"log_crawl_job OK — site={job.get('base_url', '?')[:60]}")
            return True
        except Exception as exc:
            logger.error(f"log_crawl_job FAILED: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # Scrape Cache
    # ------------------------------------------------------------------ #

    async def cache_page(self, url: str, content_hash: str,
                          etag: str | None = None,
                          last_modified: str | None = None) -> bool:
        """Cache a scraped page's hash in the `scrape_cache` table.

        Args:
            url:           The page URL (primary key).
            content_hash:  MD5/SHA of the raw HTML — used to detect changes.
            etag:          HTTP ETag header value, if any.
            last_modified: HTTP Last-Modified header value, if any.
        """
        if not self.enabled:
            return False

        try:
            self._client.table("scrape_cache").upsert(
                {
                    "url": url,
                    "content_hash": content_hash,
                    "etag": etag,
                    "last_modified": last_modified,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="url",
            ).execute()
            return True
        except Exception as exc:
            logger.error(f"cache_page FAILED: {exc}")
            return False

    async def is_page_changed(self, url: str, new_hash: str) -> bool:
        """Return True if the page hash differs from what's cached (or not cached yet)."""
        if not self.enabled:
            return True  # assume changed if DB disabled

        try:
            result = (
                self._client.table("scrape_cache")
                .select("content_hash")
                .eq("url", url)
                .single()
                .execute()
            )
            cached_hash = result.data.get("content_hash") if result.data else None
            return cached_hash != new_hash
        except Exception:
            return True  # treat errors as "changed" so we re-scrape
