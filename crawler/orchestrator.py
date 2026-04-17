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
from typing import Optional
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


# ─────────────────────────────────────────────
# IMPORTS — crawler package
# ─────────────────────────────────────────────

from crawler.spider import CrawlConfig, RawListing, LinkHarvester, PageFetcher, RealEstateCrawler
from crawler.extractor import DetailExtractor, ROIEngine
from crawler.db_writer import SupabaseWriter


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
            logger.info(f"📋 พบ {len(listing_urls)} listing URLs (API records: {len(api_data)})")

            if not listing_urls and not api_data:
                logger.warning("ไม่พบ listing — จบการทำงาน")
                self._stats["finished_at"] = datetime.now(timezone.utc).isoformat()
                return self._stats

            hot_deals = []

            if api_data:
                # ── API Fast-Path: ข้าม Playwright ──────────────────────────────
                # มีข้อมูลจาก REST API แล้ว — ไม่ต้อง scrape detail pages
                logger.info(f"⚡ API fast-path: บันทึก {len(api_data)} records โดยตรง (ไม่ใช้ Playwright)")
                self._stats["scraped"] = len(api_data)
                self._stats["extracted"] = len(api_data)
                self._stats["pages_crawled"] = len(api_data)

                for url, record in api_data.items():
                    deal = self._api_record_to_deal(record)
                    if not deal.get("listing_url") or not deal.get("price"):
                        self._stats["skipped"] += 1
                        continue

                    roi = self.roi_engine.calculate(deal)
                    self._log_deal(deal, roi)
                    merged = {**deal, **roi}

                    if not self.dry_run:
                        ok = await self.db.upsert_deal(merged)
                        if ok:
                            self._stats["saved"] += 1
                    else:
                        self._stats["saved"] += 1

                    if roi.get("priority") == "HIGH":
                        hot_deals.append(merged)
                        self._stats["hot_deals"] += 1

            else:
                # ── Playwright Path: scrape detail pages ────────────────────────
                # ใช้เฉพาะสำหรับ sites ที่ไม่มี API harvester
                raw_listings = await self._scrape_details(listing_urls, config)
                self._stats["scraped"] = len(raw_listings)

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

                for data in extracted:
                    if data.get("error"):
                        self._stats["errors"] += 1
                        continue

                    roi = self.roi_engine.calculate(data)
                    self._log_deal(data, roi)
                    merged = {**data, **roi}

                    if not self.dry_run:
                        ok = await self.db.upsert_deal(merged)
                        if ok:
                            self._stats["saved"] += 1
                    else:
                        self._stats["saved"] += 1

                    if roi.get("priority") == "HIGH":
                        hot_deals.append(merged)
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

        ใช้ RealEstateCrawler จาก spider.py ซึ่ง route ทุก domain อัตโนมัติ:
        - npa.krungthai.com  → KrungthaiHarvester
        - www.scbnpa.com     → SCBNPAHarvester
        - www.ghbhomecenter.com → GHBankHarvester
        - www.led.go.th      → LEDHarvester
        - อื่นๆ              → Playwright spider (fallback)
        """
        from urllib.parse import urlparse
        domain = urlparse(base_url).netloc

        spider = RealEstateCrawler(config=config)
        api_harvester = spider._get_api_harvester(base_url)

        if api_harvester is not None:
            logger.info(f"  ⚡ API harvester: {type(api_harvester).__name__} for {domain}")
            listings = await api_harvester.fetch_all(
                max_pages=config.max_pages,
                max_listings=config.max_listings,
            )
            urls = [x["source_url"] for x in listings][:config.max_listings]
            # Build url → api_data map (price, location, type from API)
            api_data: dict[str, dict] = {
                x["source_url"]: x for x in listings if x.get("source_url")
            }
            self._stats["pages_crawled"] = len(listings)
            logger.info(f"  ✅ {type(api_harvester).__name__}: {len(urls)} listings")
            return urls, api_data

        # ── Playwright fallback (JS-heavy sites without API harvester) ──
        logger.info(f"  🎭 Playwright spider for {domain}")
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
    # API RECORD → DEAL FORMAT
    # ─────────────────────────────────────────

    def _api_record_to_deal(self, record: dict) -> dict:
        """
        แปลง normalized API record (จาก KrungthaiHarvester / SCBNPAHarvester / GHBankHarvester / etc.)
        → deal dict ที่ ROIEngine และ SupabaseWriter รับได้

        ทุก harvester ส่ง dict ที่มี keys เดียวกัน (normalized format):
          source_url, price, area_sqm, property_type, condition, location,
          title, source_domain, is_benchmark
        บาง harvester (krungthai) ส่ง asking_price, area_rai, property_type (ภาษาไทย)
        """
        # ── property type mapping ────────────────────────────────────────
        PTYPE_MAP = {
            "บ้านเดี่ยว": "house", "บ้าน": "house",
            "ทาวน์เฮ้าส์": "townhouse", "ทาวน์โฮม": "townhouse", "ทาวน์เฮาส์": "townhouse",
            "คอนโด": "condo", "ห้องชุด": "condo", "อาคารชุด": "condo",
            "ที่ดิน": "land", "ที่ดินเปล่า": "land",
            "อาคาร": "other", "สิ่งปลูกสร้าง": "other", "ตึกแถว": "other",
            "house": "house", "condo": "condo", "townhouse": "townhouse",
            "land": "land", "other": "other",
        }

        ptype_raw = str(record.get("property_type") or "")
        ptype = PTYPE_MAP.get(ptype_raw, "other")

        # ── price ────────────────────────────────────────────────────────
        price = 0.0
        for price_key in ("price", "asking_price", "appraisalPrice", "start_price"):
            raw_p = record.get(price_key)
            if raw_p:
                try:
                    price = float(str(raw_p).replace(",", ""))
                    if price > 0:
                        break
                except (ValueError, TypeError):
                    pass

        # ── area ─────────────────────────────────────────────────────────
        area_sqm = 0.0
        # Prefer direct sqm field
        for area_key in ("area_sqm", "usable_area", "floor_size"):
            raw_a = record.get(area_key)
            if raw_a:
                try:
                    area_sqm = float(str(raw_a).replace(",", ""))
                    if area_sqm > 0:
                        break
                except (ValueError, TypeError):
                    pass

        # Fallback: rai → sqm (1 rai = 1,600 sqm)
        if area_sqm == 0:
            raw_rai = record.get("area_rai")
            if raw_rai:
                try:
                    rai = float(str(raw_rai).replace(",", ""))
                    if rai > 0:
                        area_sqm = round(rai * 1600, 2)
                except (ValueError, TypeError):
                    pass

        # ── condition ────────────────────────────────────────────────────
        condition = record.get("condition") or ("fair" if record.get("source_domain") == "npa.krungthai.com" else "good")

        # ── location ─────────────────────────────────────────────────────
        location = record.get("location") or ""
        if not location:
            parts = filter(None, [record.get("district"), record.get("province")])
            location = " ".join(parts)

        # ── title / project_name ─────────────────────────────────────────
        title = record.get("title") or record.get("project_name") or ""

        return {
            "listing_url":   record.get("source_url") or record.get("listing_url", ""),
            "source_domain": record.get("source_domain") or record.get("source_site", ""),
            "source_type":   "NPA" if not record.get("is_benchmark") else "MARKET",
            "property_type": ptype,
            "project_name":  title,
            "location":      location,
            "price":         price,
            "area_sqm":      area_sqm,
            "condition":     condition,
            "scraped_at":    datetime.now(timezone.utc).isoformat(),
            "raw_data":      json.dumps({
                k: v for k, v in record.items()
                if k != "raw" and v is not None
            }, ensure_ascii=False)[:2000],
        }

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
