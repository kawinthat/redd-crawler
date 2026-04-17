"""
orchestrator.py — RE:DD Autonomous Crawler Pipeline
รัน: python -m crawler.orchestrator [--dry-run] [--url URL] [--max-pages N] [--max-listings N]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


# ─────────────────────────────────────────────
# IMPORTS — crawler package
# ─────────────────────────────────────────────

from crawler.spider import CrawlConfig, RawListing, LinkHarvester, PageFetcher
from crawler.extractor import DetailExtractor, ROIEngine
from crawler.db_writer import SupabaseWriter
from crawler.krungthai_harvester import KrungthaiHarvester


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

API_HARVESTER_DOMAINS = {
    "npa.krungthai.com": "krungthai",
}


# ─────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────

class AutonomousCrawler:
    """
    โยน URL เดียว → ระบบทำทุกอย่างเอง

    Flow:
    1. Harvest listing URLs (API harvester หรือ Playwright spider)
    2. Scrape detail pages
    3. AI batch extraction
    4. ROI calculation
    5. Save Supabase (ข้ามถ้า --dry-run)
    6. LINE alert สำหรับ HOT deals
    """

    def __init__(
        self,
        dry_run: bool = False,
        line_token: Optional[str] = None,
    ):
        self.dry_run     = dry_run
        self.line_token  = line_token
        self.extractor   = DetailExtractor()           # reads OPENROUTER_API_KEY from env
        self.roi_engine  = ROIEngine()
        self.db          = SupabaseWriter()            # reads SUPABASE_URL/KEY from env
        self.harvester   = LinkHarvester()
        self.fetcher     = PageFetcher()
        self._stats: dict = {}

    # ─────────────────────────────────────────
    # ENTRY POINT
    # ─────────────────────────────────────────

    async def run(self, base_url: str, config: CrawlConfig) -> dict:
        domain = base_url.split("/")[2]
        self._stats = {
            "base_url":      base_url,
            "dry_run":       self.dry_run,
            "started_at":    datetime.now(timezone.utc).isoformat(),
            "pages_crawled": 0,
            "links_found":   0,
            "scraped":       0,
            "extracted":     0,
            "saved":         0,
            "hot_deals":     0,
            "skipped":       0,
            "errors":        0,
        }

        mode = "🧪 DRY-RUN" if self.dry_run else "🚀 LIVE"
        logger.info(f"{mode} — เริ่ม crawl: {base_url}")

        try:
            # ── Phase 1: Harvest listing URLs + API data ──
            listing_urls, api_data = await self._harvest(base_url, config)
            self._stats["links_found"] = len(listing_urls)
            logger.info(f"📋 พบ {len(listing_urls)} listing URLs (API price data: {len(api_data)} records)")

            if not listing_urls:
                logger.warning("ไม่พบ listing — จบการทำงาน")
                self._stats["finished_at"] = datetime.now(timezone.utc).isoformat()
                return self._stats

            # ── Phase 2: Scrape detail pages ──
            raw_listings = await self._scrape_details(listing_urls, config)
            self._stats["scraped"] = len(raw_listings)

            # ── Phase 3: AI Extract ──
            if not raw_listings:
                logger.warning("ไม่มี HTML ให้ extract")
                self._stats["finished_at"] = datetime.now(timezone.utc).isoformat()
                return self._stats

            extracted = await self.extractor.extract_batch(
                [{"url": r.url, "html": r.html, "source_domain": r.source_domain}
                 for r in raw_listings],
                batch_size=5,
            )
            self._stats["extracted"] = len(extracted)
            logger.info(f"🤖 Extracted {len(extracted)} records")

            # ── Phase 4: ROI + Save ──
            hot_deals = []
            for data in extracted:
                if data.get("error"):
                    self._stats["errors"] += 1
                    continue

                # Merge API price data — krungthai hides prices in HTML
                url = data.get("listing_url", "")
                if url in api_data:
                    api = api_data[url]
                    # Use API price if AI couldn't find one from HTML
                    if not data.get("price") and api.get("asking_price"):
                        try:
                            data["price"] = int(str(api["asking_price"]).replace(",", ""))
                        except (ValueError, TypeError):
                            pass
                    if not data.get("location") and api.get("location"):
                        data["location"] = api["location"]
                    if not data.get("property_type") and api.get("property_type"):
                        data["property_type"] = api["property_type"]
                    # Convert area_rai → area_sqm if area missing (1 Rai = 1,600 sqm)
                    if not data.get("area_sqm") and api.get("area_rai"):
                        try:
                            rai = float(str(api["area_rai"]).replace(",", ""))
                            if rai > 0:
                                data["area_sqm"] = round(rai * 1600, 2)
                        except (ValueError, TypeError):
                            pass

                roi = self.roi_engine.calculate(data)
                self._log_deal(data, roi)

                if not self.dry_run:
                    merged = {**data, **roi}
                    ok = await self.db.upsert_deal(merged)
                    if ok:
                        self._stats["saved"] += 1
                else:
                    self._stats["saved"] += 1   # count as "would save"

                if roi.get("priority") == "HIGH":
                    hot_deals.append({**data, **roi})
                    self._stats["hot_deals"] += 1

            # ── Phase 5: Alert ──
            if not self.dry_run:
                for deal in hot_deals:
                    await self._send_line_alert(deal)

        finally:
            await self.fetcher.stop()

        self._stats["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.success(f"{'[DRY-RUN] ' if self.dry_run else ''}สแกนเสร็จ — {self._stats}")
        return self._stats

    # ─────────────────────────────────────────
    # PHASE 1: HARVEST
    # ─────────────────────────────────────────

    async def _harvest(
        self, base_url: str, config: CrawlConfig
    ) -> tuple[list[str], dict[str, dict]]:
        """
        Returns (urls, api_data_by_url)
        api_data_by_url มี price/location/type จาก API (ใช้เมื่อ HTML ไม่มีราคา)
        """
        domain = base_url.split("/")[2]

        # ── API mode (ไม่ต้อง Playwright) ──
        if API_HARVESTER_DOMAINS.get(domain) == "krungthai":
            logger.info(f"  ⚡ Using KrungthaiHarvester (API mode) — ไม่ต้อง Playwright")
            harvester = KrungthaiHarvester(delay=0.5)
            listings = await harvester.fetch_all(max_pages=min(config.max_pages, 140))
            urls = [x["source_url"] for x in listings][:config.max_listings]
            # Build url → api_data map (price, location, type from API)
            api_data: dict[str, dict] = {
                x["source_url"]: x for x in listings if x.get("source_url")
            }
            self._stats["pages_crawled"] = min(config.max_pages, 140)
            return urls, api_data

        # ── Playwright mode (JS-heavy sites) ──
        logger.info(f"  🎭 Using Playwright spider")
        await self.fetcher.start()
        all_urls: set[str] = set()
        current_url = base_url
        page_num = 1

        while current_url and page_num <= config.max_pages and len(all_urls) < config.max_listings:
            try:
                html = await self.fetcher.fetch(current_url)
                if not html:
                    break

                self._stats["pages_crawled"] += 1
                new_links = self.harvester.extract_listing_links(html, current_url)
                all_urls.update(new_links)
                logger.info(f"  Page {page_num}: +{len(new_links)} URLs (total {len(all_urls)})")

                next_url = self.harvester.find_next_page(html, current_url, page_num)
                if next_url and next_url != current_url and domain in next_url:
                    current_url = next_url
                    page_num += 1
                else:
                    break

                await asyncio.sleep(random.uniform(config.delay_min, config.delay_max))

            except Exception as e:
                logger.error(f"  Harvest error page {page_num}: {e}")
                self._stats["errors"] += 1
                break

        return list(all_urls)[:config.max_listings], {}

    # ─────────────────────────────────────────
    # PHASE 2: SCRAPE DETAILS
    # ─────────────────────────────────────────

    async def _scrape_details(
        self, urls: list[str], config: CrawlConfig
    ) -> list[RawListing]:
        """Fetch detail pages with concurrency limit."""
        if not self.fetcher._browser:
            await self.fetcher.start()

        semaphore = asyncio.Semaphore(config.concurrency)
        results: list[RawListing] = []

        async def _fetch_one(url: str, idx: int) -> Optional[RawListing]:
            async with semaphore:
                try:
                    domain = url.split("/")[2]
                    html = await self.fetcher.fetch(url)
                    if not html:
                        return None

                    content_hash = hashlib.md5(html.encode()).hexdigest()

                    if idx % 10 == 0 or idx < 5:
                        logger.info(f"  📄 [{idx+1}/{len(urls)}] scraped {url[:70]}")

                    await asyncio.sleep(random.uniform(
                        config.delay_min * 0.4, config.delay_max * 0.4
                    ))

                    return RawListing(
                        url=url,
                        source_domain=domain,
                        html=html,
                        content_hash=content_hash,
                    )
                except Exception as e:
                    self._stats["errors"] += 1
                    logger.warning(f"  Scrape error {url[:60]}: {e}")
                    return None

        tasks = [_fetch_one(url, i) for i, url in enumerate(urls)]
        raw = await asyncio.gather(*tasks)
        results = [r for r in raw if r is not None]
        logger.info(f"  ✅ Scraped {len(results)}/{len(urls)} pages")
        return results

    # ─────────────────────────────────────────
    # LOGGING + ALERTS
    # ─────────────────────────────────────────

    def _log_deal(self, data: dict, roi: dict):
        flag = roi.get("roi_flag", "")
        pct  = roi.get("roi_percent", 0)
        loc  = data.get("location", "?")
        price = data.get("price")
        price_str = f"฿{price:,}" if price else "฿?"
        logger.info(
            f"  {flag} [{loc}] {price_str} "
            f"| ROI {pct}% | {data.get('property_type','?')}"
        )

    async def _send_line_alert(self, deal: dict):
        if not self.line_token or self.line_token == "your-line-token-here":
            return
        try:
            import httpx
            msg = (
                f"\n🔥 HOT DEAL!\n"
                f"━━━━━━━━━━━━━\n"
                f"📍 {deal.get('location','?')}\n"
                f"🏠 {deal.get('property_type','?')} {deal.get('area_sqm','?')} ตร.ม.\n"
                f"💰 ฿{deal.get('price',0):,.0f}\n"
                f"📈 ROI {deal.get('roi_percent',0):.1f}% | กำไรคาด ฿{deal.get('estimated_profit',0):,.0f}\n"
                f"🔗 {str(deal.get('listing_url',''))[:60]}"
            )
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://notify-api.line.me/api/notify",
                    headers={"Authorization": f"Bearer {self.line_token}"},
                    data={"message": msg},
                    timeout=10,
                )
        except Exception as e:
            logger.warning(f"LINE alert failed: {e}")


# ─────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="RE:DD Autonomous Real Estate Scanner")
    parser.add_argument("--url",          default=os.getenv("TARGET_URL", "https://npa.krungthai.com"),
                        help="Base URL to crawl")
    parser.add_argument("--max-pages",    type=int, default=int(os.getenv("MAX_PAGES", "5")))
    parser.add_argument("--max-listings", type=int, default=int(os.getenv("MAX_LISTINGS", "20")))
    parser.add_argument("--concurrency",  type=int, default=int(os.getenv("CONCURRENCY", "3")))
    parser.add_argument("--dry-run",      action="store_true",
                        help="Run pipeline but do NOT save to Supabase")
    args = parser.parse_args()

    config = CrawlConfig(
        base_url      = args.url,
        max_pages     = args.max_pages,
        max_listings  = args.max_listings,
        concurrency   = args.concurrency,
        delay_min     = 0.5,
        delay_max     = 1.5,
    )

    crawler = AutonomousCrawler(
        dry_run    = args.dry_run,
        line_token = os.getenv("LINE_NOTIFY_TOKEN"),
    )

    stats = await crawler.run(args.url, config)

    print("\n" + "═" * 50)
    print(f"  {'[DRY-RUN] ' if args.dry_run else ''}📊 PIPELINE SUMMARY")
    print("═" * 50)
    for k, v in stats.items():
        if k not in ("started_at", "finished_at", "base_url"):
            print(f"  {k:20s}: {v}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
