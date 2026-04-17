"""
GHB Home Center Harvester — HTML scraper (ghbhomecenter.com)
ไม่มี public REST API → scrape HTML ด้วย httpx + regex

Listing page: https://www.ghbhomecenter.com/property-for-sale?page=X
  → ดึง property links รูปแบบ /property-XXXXXX (40 รายการ/หน้า)

Detail page:  https://www.ghbhomecenter.com/property-XXXXXX
  → ดึง OG title (type + area + project), OG description (location),
    price จาก body HTML (rendered server-side, ไม่ต้อง Playwright)
"""
import asyncio
import re
from typing import Optional

import httpx
from loguru import logger

BASE_URL = "https://www.ghbhomecenter.com"
LIST_URL = BASE_URL + "/property-for-sale"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

TYPE_MAP = {
    "คอนโด": "condo",   "ห้องชุด": "condo",   "คอนโดมิเนียม": "condo",
    "บ้านเดี่ยว": "house", "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse", "ทาวน์โฮม": "townhouse", "ทาวน์เฮาส์": "townhouse",
    "ที่ดิน": "land",
    "อาคาร": "commercial", "อาคารพาณิชย์": "commercial", "ตึกแถว": "commercial",
    "สิ่งปลูกสร้าง": "other",
    "condo": "condo", "house": "house", "townhouse": "townhouse",
    "land": "land",   "commercial": "commercial", "other": "other",
}


def _parse_type(text: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw.lower() in text.lower():
            return ptype
    return "other"


def _parse_area_sqm(text: str) -> Optional[float]:
    """Extract area from Thai text: 'ตร.ว.' × 4 → sqm  |  'ตร.ม.' direct."""
    m = re.search(r"([\d,]+\.?\d*)\s*ตร\.?ว", text)
    if m:
        try:
            return round(float(m.group(1).replace(",", "")) * 4, 2)
        except ValueError:
            pass
    m = re.search(r"([\d,]+\.?\d*)\s*ตร\.?ม", text)
    if m:
        try:
            return round(float(m.group(1).replace(",", "")), 2)
        except ValueError:
            pass
    return None


def _parse_price(html: str) -> Optional[int]:
    """Extract sale price from GHB detail page body (100,000 – 500,000,000 ฿)."""
    patterns = [
        r'class="[^"]*price[^"]*"[^>]*>\s*฿?\s*([\d,]+)',
        r'(?:ราคา|Price)[^฿\d]{0,30}฿?\s*([\d,]{6,})',
        r'฿\s*([\d,]{6,})',
        r'([\d,]{7,})\s*(?:บาท|฿)',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            try:
                price = int(m.group(1).replace(",", ""))
                if 100_000 < price < 500_000_000:
                    return price
            except ValueError:
                pass
    return None


def _parse_location(og_desc: str) -> str:
    """
    GHB OG desc: '...บางพูน เมืองปทุมธานี ปทุมธานี จากธนาคาร...'
    → 'ปทุมธานี เมืองปทุมธานี'
    """
    # Pattern 1: "เขตXXX กรุงเทพ..." or "อำเภอXXX จังหวัดXXX"
    m = re.search(
        r"(?:อำเภอ|เขต)\s*([ก-๙]+(?:\s[ก-๙]+)?)\s+(?:จังหวัด\s*)?([ก-๙]+)",
        og_desc,
    )
    if m:
        return f"{m.group(2).strip()} {m.group(1).strip()}"

    # Pattern 2: "DISTRICT PROVINCE จากธนาคาร"
    m2 = re.search(
        r"([ก-๙]+(?:[ก-๙\s]+)?)\s+([ก-๙]+)\s+จากธนาคาร",
        og_desc,
    )
    if m2:
        # last two words before จากธนาคาร
        parts = m2.group(0).replace("จากธนาคาร", "").strip().split()
        if len(parts) >= 2:
            province = parts[-1]
            district = parts[-2]
            return f"{province} {district}"

    # Pattern 3: จังหวัด keyword
    m3 = re.search(r"จังหวัด\s*([ก-๙]+)", og_desc)
    if m3:
        return m3.group(1).strip()

    return ""


class GHBankHarvester:
    """
    Harvest NPA listings from GH Bank Home Center via HTML scraping.
    No Playwright needed — httpx only.

    Flow:
      1. Paginate /property-for-sale?page=X → collect /property-XXXXXX URLs
      2. Scrape each detail page concurrently → extract price/type/area/location
    """

    def __init__(self, per_page: int = 40, delay: float = 1.2, concurrency: int = 5):
        self.per_page    = per_page
        self.delay       = delay
        self.concurrency = concurrency

    async def fetch_all(
        self,
        max_pages: int = 999999,
        max_listings: int = 999999,
    ) -> list[dict]:
        listing_urls: list[str] = []

        async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            # ── Phase 1: collect property URLs ──────────────────────
            for page in range(1, max_pages + 1):
                if len(listing_urls) >= max_listings:
                    break
                try:
                    resp = await client.get(LIST_URL, params={"page": page})
                    if resp.status_code != 200:
                        logger.warning(f"GHB listing page {page}: HTTP {resp.status_code}")
                        break

                    found = re.findall(
                        r'href="(https://www\.ghbhomecenter\.com/property-(\d+))"',
                        resp.text,
                    )
                    seen = set(listing_urls)
                    # deduplicate both within page and across pages
                    new_urls = list(dict.fromkeys(
                        url for url, _ in found if url not in seen
                    ))
                    listing_urls.extend(new_urls)
                    logger.info(f"GHB page {page}: +{len(new_urls)} URLs (total {len(listing_urls)})")

                    if len(new_urls) < 5:
                        break
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"GHB listing page {page}: {e}")
                    break

            listing_urls = listing_urls[:max_listings]
            if not listing_urls:
                logger.warning("GHB: ไม่พบ property URLs บน listing page")
                return []

            # ── Phase 2: scrape detail pages ────────────────────────
            sem = asyncio.Semaphore(self.concurrency)
            results: list[dict] = []

            async def _scrape_one(url: str) -> Optional[dict]:
                async with sem:
                    try:
                        r = await client.get(url)
                        if r.status_code == 200:
                            deal = self._parse_detail(r.text, url)
                            await asyncio.sleep(0.3)
                            return deal
                        return None
                    except Exception as e:
                        logger.debug(f"GHB detail {url[:60]}: {e}")
                        return None

            raw = await asyncio.gather(*[_scrape_one(u) for u in listing_urls])
            results = [r for r in raw if r is not None]

        logger.info(f"GHB: scrape สำเร็จ {len(results)}/{len(listing_urls)} รายการ")
        return results

    # ────────────────────────────────────────────────────
    def _parse_detail(self, html: str, url: str) -> Optional[dict]:
        """OG tags + price from body → normalized deal dict."""
        og_title = og_desc = ""
        m = re.search(r'<meta property="og:title"\s+content="([^"]+)"', html)
        if m: og_title = m.group(1)
        m = re.search(r'<meta property="og:description"\s+content="([^"]+)"', html)
        if m: og_desc = m.group(1)

        if not og_title:
            return None

        ptype    = _parse_type(og_title)
        area_sqm = _parse_area_sqm(og_title) or _parse_area_sqm(og_desc)
        location = _parse_location(og_desc)
        price    = _parse_price(html)

        # Clean title: remove type keyword prefix
        title = re.sub(
            r'^(?:ขาย(?:ด่วน)?|บ้าน|คอนโด|ทาวน์เฮ้าส์|ที่ดิน|อาคาร|สิ่งปลูกสร้าง)\s*',
            "", og_title
        ).strip()

        if not price:
            return None  # ไม่มีราคา → skip (deals schema requires price > 0)

        result: dict = {
            "listing_url":   url,
            "source_domain": "ghbhomecenter.com",
            "source_type":   "bank_npa",
            "property_type": ptype,
            "project_name":  title[:200],
            "location":      location,
            "price":         price,
            "condition":     "fair",
            "is_benchmark":  False,
        }
        if area_sqm and area_sqm > 0:
            result["area_sqm"] = area_sqm

        return result
