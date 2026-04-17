"""
orchestrator.py — 100% Autonomous Crawler
โยน base URL → ระบบทำทุกอย่างเอง
"""

import asyncio
import hashlib
import json
import os
import random
import time
from datetime import datetime
from typing import Callable, Optional

from supabase import create_client, Client

from spider import CrawlConfig, RawListing, LinkHarvester, PageFetcher
from extractor import DetailExtractor, ROIEngine


# ─────────────────────────────────────────────
# SUPABASE WRITER
# ─────────────────────────────────────────────

class DBWriter:
    """
    Upsert deals ลง Supabase
    ไม่ duplicate — ใช้ listing_url เป็น unique key
    """

    def __init__(self, supabase_url: str, supabase_key: str):
        self.db: Client = create_client(supabase_url, supabase_key)

    def upsert_deal(self, data: dict, roi: dict) -> bool:
        try:
            record = {
                # Listing info
                "listing_url":      data.get("listing_url"),
                "source_domain":    data.get("source_domain"),
                "source_type":      data.get("source_type"),
                "property_type":    data.get("property_type"),
                "project_name":     data.get("project_name"),
                "address":          data.get("address"),
                "location":         data.get("location"),
                "title_deed":       data.get("title_deed"),
                "bedrooms":         data.get("bedrooms"),
                "bathrooms":        data.get("bathrooms"),
                "floors":           data.get("floors"),
                "condition":        data.get("condition"),
                "features":         data.get("features"),
                "auction_date":     data.get("auction_date"),
                "contact":          data.get("contact"),
                # Prices
                "price":            data.get("price"),
                "area_sqm":         data.get("area_sqm"),
                "usable_area_sqm":  data.get("usable_area_sqm"),
                "land_area_sqm":    data.get("land_area_sqm"),
                # ROI
                "roi_valid":        roi.get("roi_valid", False),
                "roi_percent":      roi.get("roi_percent"),
                "roi_flag":         roi.get("roi_flag"),
                "priority":         roi.get("priority"),
                "estimated_profit": roi.get("estimated_profit"),
                "total_cost":       roi.get("total_cost"),
                "market_value":     roi.get("market_value"),
                "reno_cost_total":  roi.get("reno_cost_total"),
                # Meta
                "scraped_at":       datetime.utcnow().isoformat(),
                "raw_data":         json.dumps({**data, **roi}, ensure_ascii=False),
            }

            # Remove None values
            record = {k: v for k, v in record.items() if v is not None}

            self.db.table("deals").upsert(
                record,
                on_conflict="listing_url"  # ไม่ duplicate
            ).execute()
            return True

        except Exception as e:
            print(f"  ❌ DB write error: {e}")
            return False

    def is_already_scraped(self, url: str, content_hash: str) -> bool:
        """เช็คว่าเคย scrape URL นี้แล้ว และเนื้อหาไม่เปลี่ยน"""
        try:
            r = self.db.table("scrape_cache") \
                .select("content_hash") \
                .eq("url", url) \
                .execute()
            if r.data:
                return r.data[0]["content_hash"] == content_hash
        except Exception:
            pass
        return False

    def update_cache(self, url: str, content_hash: str):
        try:
            self.db.table("scrape_cache").upsert({
                "url": url,
                "content_hash": content_hash,
                "checked_at": datetime.utcnow().isoformat(),
            }, on_conflict="url").execute()
        except Exception:
            pass


# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────

class CircuitBreaker:

    def __init__(self, threshold: int = 5, timeout: int = 300):
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self.threshold = threshold
        self.timeout = timeout

    def is_open(self, domain: str) -> bool:
        if domain not in self._opened_at:
            return False
        elapsed = time.time() - self._opened_at[domain]
        if elapsed > self.timeout:
            # Half-open: ลองใหม่
            self._failures[domain] = 0
            del self._opened_at[domain]
            return False
        return True

    def record_success(self, domain: str):
        self._failures[domain] = 0

    def record_failure(self, domain: str):
        self._failures[domain] = self._failures.get(domain, 0) + 1
        if self._failures[domain] >= self.threshold:
            self._opened_at[domain] = time.time()
            print(f"  🔴 Circuit OPEN: {domain} — หยุด {self.timeout}s")


# ─────────────────────────────────────────────
# MAIN ORCHESTRATOR — 100% Autonomous
# ─────────────────────────────────────────────

class AutonomousCrawler:
    """
    โยน URL เดียว → ระบบทำทุกอย่างเอง

    Flow:
    1. เปิดหน้า listing → scroll → เก็บ listing URLs
    2. ตาม pagination จนหมด
    3. เข้าแต่ละ detail page → scrape HTML
    4. Batch AI extraction
    5. ROI calculation
    6. Save Supabase
    7. LINE alert ถ้า HOT
    """

    def __init__(
        self,
        anthropic_key: str,
        supabase_url: str,
        supabase_key: str,
        line_token: Optional[str] = None,
        on_progress: Optional[Callable] = None,
    ):
        self.extractor    = DetailExtractor(anthropic_key)
        self.db           = DBWriter(supabase_url, supabase_key)
        self.roi_engine   = ROIEngine()
        self.harvester    = LinkHarvester()
        self.fetcher      = PageFetcher()
        self.breaker      = CircuitBreaker()
        self.line_token   = line_token
        self.on_progress  = on_progress or (lambda msg, data=None: print(msg))
        self._stats       = {}

    # ──────────────────────────────────────────
    # ENTRY POINT
    # ──────────────────────────────────────────

    async def run(self, base_url: str, config: CrawlConfig) -> dict:
        """
        โยน URL เดียว → return สรุปผล
        """
        domain = base_url.split("/")[2]
        self._stats = {
            "base_url":      base_url,
            "started_at":    datetime.utcnow().isoformat(),
            "pages_crawled": 0,
            "links_found":   0,
            "scraped":       0,
            "extracted":     0,
            "saved":         0,
            "hot_deals":     0,
            "skipped":       0,
            "errors":        0,
        }

        self.on_progress(f"🚀 เริ่ม crawl: {base_url}")

        try:
            await self.fetcher.start()

            # ─ Phase 1: หา listing URLs ─
            listing_urls = await self._harvest_all_listings(
                base_url, config
            )
            self._stats["links_found"] = len(listing_urls)
            self.on_progress(
                f"📋 พบ {len(listing_urls)} listing URLs",
                {"phase": "harvest_done", "count": len(listing_urls)}
            )

            # ─ Phase 2: Scrape detail pages (parallel) ─
            raw_listings = await self._scrape_details_parallel(
                listing_urls, config
            )
            self._stats["scraped"] = len(raw_listings)

            # ─ Phase 3: AI Extract (batch) ─
            extracted = await self.extractor.extract_batch(
                [{"url": r.url, "html": r.html,
                  "source_domain": r.source_domain}
                 for r in raw_listings]
            )
            self._stats["extracted"] = len(extracted)
            self.on_progress(
                f"🤖 Extracted {len(extracted)} records",
                {"phase": "extract_done"}
            )

            # ─ Phase 4: ROI + Save ─
            hot_deals = []
            for data in extracted:
                if data.get("error"):
                    self._stats["errors"] += 1
                    continue

                roi = self.roi_engine.calculate(data)
                saved = self.db.upsert_deal(data, roi)

                if saved:
                    self._stats["saved"] += 1
                    if roi.get("priority") == "HIGH":
                        hot_deals.append({**data, **roi})
                        self._stats["hot_deals"] += 1

            # ─ Phase 5: Alert ─
            for deal in hot_deals:
                await self._send_line_alert(deal)
                self.on_progress(
                    f"🔥 HOT DEAL: {deal.get('project_name', deal.get('address', '?'))} "
                    f"ROI {deal.get('roi_percent')}%",
                    {"phase": "alert", "deal": deal}
                )

        finally:
            await self.fetcher.stop()

        self._stats["finished_at"] = datetime.utcnow().isoformat()
        self.on_progress("✅ สแกนเสร็จ", {"phase": "done", "stats": self._stats})
        return self._stats

    # ──────────────────────────────────────────
    # PHASE 1: HARVEST ALL LISTING URLS
    # ──────────────────────────────────────────

    async def _harvest_all_listings(
        self, base_url: str, config: CrawlConfig
    ) -> list[str]:

        all_urls: set[str] = set()
        current_url = base_url
        page_num = 1
        domain = base_url.split("/")[2]
        consecutive_empty = 0

        while (current_url
               and page_num <= config.max_pages
               and len(all_urls) < config.max_listings):

            if self.breaker.is_open(domain):
                self.on_progress(f"⚠️  Circuit open — ข้าม {domain}")
                break

            self.on_progress(
                f"  🌐 หน้า {page_num}: {current_url[:80]}...",
                {"phase": "crawling", "page": page_num, "found": len(all_urls)}
            )

            try:
                html = await self.fetcher.fetch(current_url)

                if not html:
                    self.breaker.record_failure(domain)
                    break

                self.breaker.record_success(domain)
                self._stats["pages_crawled"] += 1

                # Extract listing links
                new_links = self.harvester.extract_listing_links(html, current_url)

                if not new_links:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        self.on_progress(f"  ⏹  ไม่พบ listing 3 หน้าติด — หยุด")
                        break
                else:
                    consecutive_empty = 0
                    before = len(all_urls)
                    all_urls.update(new_links)
                    after = len(all_urls)
                    self.on_progress(
                        f"  ✅ +{after - before} URLs (รวม {after})"
                    )

                # หาหน้าถัดไป
                next_url = self.harvester.find_next_page(html, current_url, page_num)

                # Validate — ต้อง same domain + ไม่ซ้ำ
                if (next_url
                        and next_url != current_url
                        and domain in next_url):
                    current_url = next_url
                    page_num += 1
                else:
                    break

                # Rate limit
                await asyncio.sleep(random.uniform(
                    config.delay_min, config.delay_max
                ))

            except Exception as e:
                self.breaker.record_failure(domain)
                self.on_progress(f"  ❌ Error หน้า {page_num}: {e}")
                self._stats["errors"] += 1
                break

        return list(all_urls)[:config.max_listings]

    # ──────────────────────────────────────────
    # PHASE 2: SCRAPE DETAIL PAGES (PARALLEL)
    # ──────────────────────────────────────────

    async def _scrape_details_parallel(
        self, urls: list[str], config: CrawlConfig
    ) -> list[RawListing]:

        semaphore = asyncio.Semaphore(config.concurrency)
        domain = urls[0].split("/")[2] if urls else ""
        results: list[RawListing] = []

        async def scrape_one(url: str, idx: int) -> Optional[RawListing]:
            async with semaphore:
                try:
                    html = await self.fetcher.fetch(url)
                    if not html:
                        return None

                    content_hash = hashlib.md5(html.encode()).hexdigest()

                    # Change detection — ข้ามถ้าไม่เปลี่ยน
                    if self.db.is_already_scraped(url, content_hash):
                        self._stats["skipped"] += 1
                        return None

                    self.db.update_cache(url, content_hash)

                    # Random delay ป้องกัน ban
                    await asyncio.sleep(random.uniform(
                        config.delay_min * 0.5,
                        config.delay_max * 0.5
                    ))

                    if idx % 10 == 0:
                        self.on_progress(
                            f"  📄 Scraped {idx}/{len(urls)}",
                            {"phase": "scraping", "progress": idx, "total": len(urls)}
                        )

                    return RawListing(
                        url=url,
                        source_domain=domain,
                        html=html,
                        content_hash=content_hash,
                    )

                except Exception as e:
                    self._stats["errors"] += 1
                    return None

        # รัน parallel
        tasks = [scrape_one(url, i) for i, url in enumerate(urls)]
        raw = await asyncio.gather(*tasks, return_exceptions=False)
        results = [r for r in raw if r is not None]

        self.on_progress(
            f"  ✅ Scraped {len(results)} pages "
            f"(skipped {self._stats['skipped']} unchanged)"
        )
        return results

    # ──────────────────────────────────────────
    # LINE NOTIFY
    # ──────────────────────────────────────────

    async def _send_line_alert(self, deal: dict):
        if not self.line_token:
            return
        try:
            import httpx
            msg = (
                f"\n🔥 HOT DEAL พบใหม่!\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📍 {deal.get('location', '?')}\n"
                f"🏠 {deal.get('property_type', '?')} "
                f"| {deal.get('area_sqm', '?')} ตร.ม.\n"
                f"💰 ราคา: ฿{deal.get('buy_price', 0):,.0f}\n"
                f"📈 ROI: {deal.get('roi_percent', 0):.1f}% "
                f"| กำไรคาด ฿{deal.get('estimated_profit', 0):,.0f}\n"
                f"🔗 {deal.get('listing_url', '')[:60]}"
            )
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://notify-api.line.me/api/notify",
                    headers={"Authorization": f"Bearer {self.line_token}"},
                    data={"message": msg},
                    timeout=10,
                )
        except Exception:
            pass


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

async def main():
    # config จาก environment variables
    config = CrawlConfig(
        base_url   = os.getenv("TARGET_URL", "https://led.go.th/assets"),
        max_pages  = int(os.getenv("MAX_PAGES", "30")),
        max_listings = int(os.getenv("MAX_LISTINGS", "300")),
        concurrency  = int(os.getenv("CONCURRENCY", "5")),
    )

    crawler = AutonomousCrawler(
        anthropic_key  = os.environ["ANTHROPIC_API_KEY"],
        supabase_url   = os.environ["SUPABASE_URL"],
        supabase_key   = os.environ["SUPABASE_KEY"],
        line_token     = os.getenv("LINE_NOTIFY_TOKEN"),
    )

    stats = await crawler.run(config.base_url, config)

    print("\n" + "═" * 40)
    print("📊 SUMMARY")
    print("═" * 40)
    for k, v in stats.items():
        print(f"  {k:20s}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
