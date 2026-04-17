"""
spider.py — Autonomous Site Spider
โยน URL เดียว ระบบค้นหา listing → เข้าทุก detail page → extract → save DB
"""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, Browser


# ─────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────

@dataclass
class CrawlConfig:
    base_url: str
    max_pages: int = 50          # จำนวนหน้า listing สูงสุด
    max_listings: int = 500      # จำนวน listing สูงสุด
    concurrency: int = 5         # parallel detail fetches
    delay_min: float = 1.5       # random delay ต่ำสุด (วินาที)
    delay_max: float = 3.5       # random delay สูงสุด
    use_headless: bool = True


@dataclass
class RawListing:
    url: str
    source_domain: str
    html: str = ""
    text: str = ""
    content_hash: str = ""
    scraped_at: float = field(default_factory=time.time)


# ─────────────────────────────────────────────
# LINK HARVESTER — หา listing URLs อัตโนมัติ
# ─────────────────────────────────────────────

class LinkHarvester:
    """
    เข้าหน้า listing → ดึง URL ทุก listing card อัตโนมัติ
    ไม่ต้องรู้ selector ล่วงหน้า — ใช้ heuristic scoring
    """

    # pattern ที่บ่งบอกว่า URL เป็น detail page ของ listing
    DETAIL_PATTERNS = [
        r'/propertyDetail/\d+',       # Krungthai NPA, SCB NPA
        r'/property/\d+',             # DDProperty, generic
        r'/property-for-sale/',       # DDProperty detail
        r'/asset/detail/',            # generic
        r'/asset/\w{6,}',             # generic
        r'/listing/\d+',              # generic
        r'/detail/\d+',               # generic
        r'/for-sale/\d+',             # generic
        r'/buy/\d+',                  # generic
        r'/ประกาศ/\d+',               # Thai sites
        r'[?&]id=\d+',                # query string style
        r'/npa/\d+',                  # NPA generic
        r'/properties/\d+',           # Hipflat, Baania
        r'/p/\d+',                    # short form
        r'/home-for-sale/',           # GH Bank
        r'/condo-for-sale/',          # condo sites
        r'/house-for-sale/',          # house sites
    ]

    # pattern หน้าถัดไป
    NEXT_PAGE_PATTERNS = [
        r'/page/(\d+)',
        r'[?&]page=(\d+)',
        r'[?&]page_number=(\d+)',
        r'[?&]p=(\d+)',
    ]

    def __init__(self):
        self._compiled = [re.compile(p) for p in self.DETAIL_PATTERNS]

    def is_detail_url(self, url: str) -> bool:
        return any(p.search(url) for p in self._compiled)

    def score_link(self, tag, base_url: str) -> float:
        """
        ให้คะแนน <a> tag ว่าน่าจะเป็น listing card แค่ไหน
        0 = ไม่ใช่, 1 = แน่นอน
        """
        href = tag.get("href", "")
        if not href or href.startswith("#"):
            return 0.0

        abs_url = urljoin(base_url, href)
        score = 0.0

        # URL pattern
        if self.is_detail_url(abs_url):
            score += 0.6

        # Class/ID hints
        classes = " ".join(tag.get("class", []))
        text = tag.get_text(strip=True)
        for hint in ["card", "listing", "property", "item", "result",
                     "ทรัพย์", "ประกาศ", "asset", "npa"]:
            if hint in classes.lower() or hint in str(tag).lower():
                score += 0.1

        # มี text ที่บ่งบอกว่าเป็นชื่อทรัพย์
        if len(text) > 5 and any(kw in text for kw in
                                  ["คอนโด","บ้าน","ทาวน์","อาคาร",
                                   "ที่ดิน","ห้อง","Condo","House",
                                   "บาท","฿","ตร."]):
            score += 0.2

        # ป้องกัน nav/footer
        parent = tag.parent
        for _ in range(3):
            if parent is None:
                break
            if parent.name in ["nav", "footer", "header"]:
                score -= 0.5
            parent = parent.parent

        return min(score, 1.0)

    def extract_listing_links(self, html: str, base_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        links = []

        for tag in soup.find_all("a", href=True):
            score = self.score_link(tag, base_url)
            if score >= 0.4:
                abs_url = urljoin(base_url, tag["href"])
                # ต้อง same domain
                if urlparse(abs_url).netloc == urlparse(base_url).netloc:
                    if abs_url not in seen:
                        seen.add(abs_url)
                        links.append(abs_url)

        return links

    def find_next_page(self, html: str, current_url: str, page_num: int) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")

        # วิธี 1: ปุ่ม "ถัดไป" / "Next"
        for selector in [
            'a:contains("ถัดไป")', 'a:contains("Next")',
            '[aria-label="Next page"]', '.pagination-next a',
            '.next a', '[rel="next"]',
        ]:
            try:
                el = soup.select_one(selector)
                if el and el.get("href"):
                    return urljoin(current_url, el["href"])
            except Exception:
                pass

        # วิธี 2: URL pattern เพิ่ม page number
        for pattern in self.NEXT_PAGE_PATTERNS:
            m = re.search(pattern, current_url)
            if m:
                current_n = int(m.group(1))
                next_url = current_url.replace(
                    m.group(0),
                    m.group(0).replace(str(current_n), str(current_n + 1))
                )
                return next_url

        # วิธี 3: ลองต่อ /page/{n+1} หรือ ?page={n+1}
        base = current_url.split("?")[0].rstrip("/")
        candidates = [
            f"{base}/page/{page_num + 1}",
            f"{current_url}{'&' if '?' in current_url else '?'}page={page_num + 1}",
        ]
        return candidates[0]  # จะ validate ในขั้นต่อไป


# ─────────────────────────────────────────────
# PAGE FETCHER — ดึง HTML ฉลาด
# ─────────────────────────────────────────────

class PageFetcher:
    """
    Tier 1: httpx (เร็ว ฟรี)
    Tier 2: Playwright (JS-rendered)
    Auto-detect ว่าต้องใช้ tier ไหน
    """

    JS_SIGNALS = [
        # React / Next.js
        "__NEXT_DATA__", "data-reactroot", "react-root",
        # Vue / Nuxt.js
        "__NUXT__", "data-n-head", "data-server-rendered", "__vue__",
        # Angular
        "ng-app", "ng-version",
        # Generic SPA
        "app-loading", "window.__INITIAL_STATE__",
        "window.__APP_STATE__", "window.App",
    ]

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def __init__(self):
        self._browser: Optional[Browser] = None
        self._playwright = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
                "--disable-extensions",
            ]
        )

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _needs_js(self, html: str) -> bool:
        if not html or len(html) < 500:
            return True
        # Known JS framework signals
        if any(sig in html for sig in self.JS_SIGNALS):
            return True
        # Heuristic: very little real content despite having HTML
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html, "html.parser")
        links = soup.find_all("a", href=True)
        if len(html) < 15000 and len(links) < 5:
            return True
        return False

    async def fetch(self, url: str) -> str:
        """Auto-select tier"""
        html = await self._fetch_static(url)
        if self._needs_js(html):
            html = await self._fetch_dynamic(url)
        return html

    async def _fetch_static(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers=self.HEADERS
            ) as client:
                r = await client.get(url)
                return r.text if r.status_code == 200 else ""
        except Exception:
            return ""

    async def _fetch_dynamic(self, url: str) -> str:
        if not self._browser:
            await self.start()
        try:
            context = await self._browser.new_context(
                user_agent=self.HEADERS["User-Agent"],
                locale="th-TH",
                viewport={"width": 1920, "height": 1080},
                extra_http_headers={"Accept-Language": "th-TH,th;q=0.9,en;q=0.8"},
            )
            # Hide automation signals
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                "window.chrome = {runtime: {}};"
                "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
            )
            page = await context.new_page()

            # Block images/fonts/ads (เร็วขึ้น 40%)
            await page.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
                lambda r: r.abort()
            )
            await page.route(
                "**/ads/**", lambda r: r.abort()
            )

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)  # JS render time

            # Scroll เพื่อ trigger lazy load
            await self._scroll_page(page)

            html = await page.content()
            await context.close()
            return html
        except Exception as e:
            print(f"  ⚠️  Dynamic fetch failed: {e}")
            return ""

    async def _scroll_page(self, page: Page):
        """Scroll ลงไปเรื่อยๆ จน infinite scroll โหลดครบ"""
        prev_height = 0
        for _ in range(8):  # max 8 scrolls
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
            curr_height = await page.evaluate("document.body.scrollHeight")
            if curr_height == prev_height:
                break
            prev_height = curr_height


# ─────────────────────────────────────────────
# MAIN CRAWLER — ประสาน LinkHarvester + PageFetcher
# ─────────────────────────────────────────────

class RealEstateCrawler:
    """
    ใส่ URL เดียว → ได้ list ของ RawListing ทุกรายการ
    ใช้ได้กับทุกเว็บใน target_sites

    Auto-routing:
        npa.krungthai.com → KrungthaiHarvester (REST API, ไม่ต้อง Playwright)
        อื่นๆ             → LinkHarvester + PageFetcher (Playwright)
    """

    # เว็บที่มี API harvester พิเศษ (domain → harvester class name)
    API_ROUTES: dict = {
        "npa.krungthai.com":   "KrungthaiHarvester",
        "www.scbnpa.com":      "SCBNPAHarvester",
        "scbnpa.com":          "SCBNPAHarvester",
        "www.ghbhomecenter.com": "GHBankHarvester",
        "ghbhomecenter.com":   "GHBankHarvester",
    }

    def __init__(self, config: Optional[CrawlConfig] = None):
        self.config = config
        self.harvester = LinkHarvester()
        self.fetcher = PageFetcher()

    def _get_api_harvester(self, base_url: str):
        """คืน API harvester instance ถ้าเว็บนี้รองรับ"""
        from urllib.parse import urlparse
        domain = urlparse(base_url).netloc
        harvester_name = self.API_ROUTES.get(domain)
        if harvester_name == "KrungthaiHarvester":
            from crawler.krungthai_harvester import KrungthaiHarvester
            return KrungthaiHarvester(rows_per_page=50, delay=0.8)
        elif harvester_name == "SCBNPAHarvester":
            from crawler.scbnpa_harvester import SCBNPAHarvester
            return SCBNPAHarvester(rows_per_page=50, delay=1.0)
        elif harvester_name == "GHBankHarvester":
            from crawler.ghbank_harvester import GHBankHarvester
            return GHBankHarvester(rows_per_page=50, delay=1.0)
        return None

    async def harvest(self, base_url: str,
                      max_pages: int = 5,
                      max_listings: int = 100) -> list:
        """
        Crawl listing pages และดึง URL ทุก listing

        Returns:
            list[str]: รายการ URL ของ detail pages
        """
        # ── API fast-path ──────────────────────────────────────
        api_harvester = self._get_api_harvester(base_url)
        if api_harvester is not None:
            print(f"  ⚡ Using API harvester for {base_url}")
            listings = await api_harvester.fetch_all(max_pages=max_pages)
            return [x["source_url"] for x in listings[:max_listings]]
        # ───────────────────────────────────────────────────────

        cfg = self.config or CrawlConfig(base_url=base_url,
                                          max_pages=max_pages,
                                          max_listings=max_listings)
        await self.fetcher.start()

        all_urls: list = []
        seen_pages: set = set()
        current_url = base_url
        page_num = 1

        try:
            while current_url and page_num <= cfg.max_pages:
                if current_url in seen_pages:
                    break
                seen_pages.add(current_url)

                print(f"  📄 Page {page_num}: {current_url}")
                html = await self.fetcher.fetch(current_url)

                if not html:
                    print(f"  ⚠️  ไม่ได้ HTML จาก {current_url}")
                    break

                links = self.harvester.extract_listing_links(html, current_url)
                new = [u for u in links if u not in all_urls]
                all_urls.extend(new)
                print(f"  🔗 พบ {len(new)} links ใหม่ (รวม {len(all_urls)})")

                if len(all_urls) >= cfg.max_listings:
                    print(f"  ✅ ถึง max_listings ({cfg.max_listings}) แล้ว")
                    break

                next_url = self.harvester.find_next_page(html, current_url, page_num)
                if not next_url or next_url == current_url:
                    print("  🏁 ไม่มีหน้าถัดไปแล้ว")
                    break

                # ตรวจว่า next_url ใช้ได้จริง
                test_html = await self.fetcher._fetch_static(next_url)
                if not test_html or len(test_html) < 200:
                    print(f"  🏁 Next page ไม่ตอบสนอง: {next_url}")
                    break

                current_url = next_url
                page_num += 1

                # Delay礼貌
                import random
                await asyncio.sleep(random.uniform(cfg.delay_min, cfg.delay_max))

        finally:
            await self.fetcher.stop()

        return all_urls[:cfg.max_listings]

    async def fetch_listings(self, urls: list) -> list:
        """
        ดึง HTML ของทุก detail page และคืน list[RawListing]
        """
        await self.fetcher.start()
        results = []

        import random
        semaphore = asyncio.Semaphore(
            self.config.concurrency if self.config else 3
        )

        async def fetch_one(url: str) -> Optional[RawListing]:
            async with semaphore:
                html = await self.fetcher.fetch(url)
                if not html:
                    return None
                content_hash = hashlib.md5(html.encode()).hexdigest()
                domain = urlparse(url).netloc
                listing = RawListing(
                    url=url,
                    source_domain=domain,
                    html=html,
                    text=BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:2000],
                    content_hash=content_hash,
                )
                await asyncio.sleep(random.uniform(0.5, 1.5))
                return listing

        tasks = [fetch_one(u) for u in urls]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        results = [r for r in raw if isinstance(r, RawListing)]

        await self.fetcher.stop()
        return results
