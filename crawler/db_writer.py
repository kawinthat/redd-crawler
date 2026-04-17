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
                  Required: source_url, price, area_sqm, location, property_type
                  Optional: roi_percent, condition, source_site, raw_html_hash, ...

        Returns:
            True on success, False on failure (logs error, does not raise).
        """
        if not self.enabled:
            logger.debug("DB disabled — skipping upsert_deal")
            return False

        try:
            # Ensure timestamp
            deal.setdefault("updated_at", datetime.now(timezone.utc).isoformat())

            result = (
                self._client.table("deals")
                .upsert(deal, on_conflict="source_url")
                .execute()
            )
            logger.debug(f"upsert_deal OK — url={deal.get('source_url', '?')[:60]}")
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
            logger.debug(f"log_crawl_job OK — site={job.get('site_url', '?')[:60]}")
            return True
        except Exception as exc:
            logger.error(f"log_crawl_job FAILED: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # Scrape Cache
    # ------------------------------------------------------------------ #

    async def cache_page(self, url: str, html_hash: str, html_content: str) -> bool:
        """Cache a scraped page in the `scrape_cache` table."""
        if not self.enabled:
            return False

        try:
            self._client.table("scrape_cache").upsert(
                {
                    "url": url,
                    "html_hash": html_hash,
                    "html_content": html_content,
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="url",
            ).execute()
            return True
        except Exception as exc:
            logger.error(f"cache_page FAILED: {exc}")
            return False

    async def get_cached_page(self, url: str) -> str | None:
        """Return cached HTML content for a URL, or None if not cached."""
        if not self.enabled:
            return None

        try:
            result = (
                self._client.table("scrape_cache")
                .select("html_content")
                .eq("url", url)
                .single()
                .execute()
            )
            return result.data.get("html_content") if result.data else None
        except Exception:
            return None
