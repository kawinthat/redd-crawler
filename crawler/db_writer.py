"""RE:DD Crawler — Supabase DB Writer
Handles upsert of deals and crawl job logging.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from loguru import logger

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
    "scraped_at", "updated_at", "raw_data",
})


class SupabaseWriter:
    """Async-friendly Supabase writer for RE:DD crawler."""

    def __init__(self) -> None:
        self._client = _get_client()

    @property
    def enabled(self) -> bool:
        return self._client is not None

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
        """
        if not self.enabled:
            logger.debug("DB disabled — skipping upsert_deal")
            return False

        try:
            # Ensure timestamp
            deal.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

            # Strip fields not in the deals schema (e.g. ROI engine internals)
            clean = {k: v for k, v in deal.items() if k in DEALS_COLUMNS and v is not None}

            result = (
                self._client.table("deals")
                .upsert(clean, on_conflict="listing_url")
                .execute()
            )
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
