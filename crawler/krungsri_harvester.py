"""
krungsri_harvester.py — ธนาคารกรุงศรีอยุธยา NPA
URL: https://www.krungsriproperty.com/search-result?page=N
Server-side rendered ASP.NET MVC — scrape HTML directly, no API needed.

Card structure:
  data-code="BX1492"        → property code
  _card_{code}_name         → project name
  _card_{code}_location     → district, province
  _card_{code}_type         → property type (Thai)
  _card_{code}_bedRoom/bathRoom → bedroom/bathroom count
  result-card-propertysize  → 0 ไร่ 0 งาน 51 ตร.ว.
  originalPrice span        → original price in Thai baht
  promoPrice span           → discounted price

10 cards per page, ~1,690 total listings.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import re
from typing import Optional

import httpx
from loguru import logger

BASE_URL = "https://www.krungsriproperty.com"
SEARCH_URL = f"{BASE_URL}/search-result"
DETAIL_BASE = f"{BASE_URL}/detail?code="

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

TYPE_MAP = {
    "บ้านเดี่ยว": "house",
    "บ้านแฝด": "house",
    "ทาวน์เฮ้าส์": "townhouse",
    "ทาวน์โฮม": "townhouse",
    "ทาวน์เฮาส์": "townhouse",
    "คอนโด": "condo",
    "คอนโดมิเนียม": "condo",
    "ห้องชุด": "condo",
    "ที่ดิน": "land",
    "อาคารพาณิชย์": "commercial",
    "อาคาร": "commercial",
}

PER_PAGE = 10


def _map_type(type_th: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw in type_th:
            return ptype
    return "other"


def _parse_price(text: str) -> Optional[int]:
    """Extract numeric price from Thai baht string like '2,990,000 บาท'."""
    m = re.search(r"([\d,]+)\s*(?:บาท|฿)", text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def _parse_area_wa(text: str) -> Optional[float]:
    """Parse 'X ไร่ Y งาน Z ตร.ว.' → sqm. 1 ไร่ = 1600 sqm, 1 งาน = 400 sqm, 1 ตร.ว. = 4 sqm."""
    m = re.search(r"(\d+)\s*ไร่\s*(\d+)\s*งาน\s*([\d.]+)\s*ตร\.ว", text)
    if m:
        rai, ngan, wa = float(m.group(1)), float(m.group(2)), float(m.group(3))
        total = rai * 1600 + ngan * 400 + wa * 4
        return round(total, 2) if total > 0 else None
    m2 = re.search(r"([\d.]+)\s*ตร\.(?:ว|ม)", text)
    if m2:
        val = float(m2.group(1))
        is_sqm = "ตร.ม" in text
        return round(val, 2) if is_sqm else round(val * 4, 2)
    return None


def _parse_page(html_raw: str) -> list[dict]:
    """Parse one search-result page → list of deal dicts."""
    html = html_lib.unescape(html_raw)
    deals = []

    # Find all property codes from data-code attribute
    codes = list(dict.fromkeys(re.findall(r'data-code="([A-Z0-9]+)"', html)))
    if not codes:
        return deals

    for code in codes:
        # Anchor for this code's block
        anchor = f'data-code="{code}"'
        idx = html.find(anchor)
        if idx < 0:
            continue

        # Get card block (next card starts with next anchor)
        next_anchor_idx = html.find('data-code="', idx + len(anchor))
        block = html[idx: next_anchor_idx if next_anchor_idx > 0 else idx + 4000]

        # Project name
        name_m = re.search(
            rf'_{code}_name[^>]*>\s*([^<]+)', block
        )
        project_name = name_m.group(1).strip() if name_m else ""

        # Location
        loc_m = re.search(rf'_{code}_location[^>]*>([^<]+)', block)
        location = loc_m.group(1).strip() if loc_m else ""

        # Property type
        type_m = re.search(rf'_{code}_type[^>]*>([^<]+)', block)
        type_th = type_m.group(1).strip() if type_m else ""
        ptype = _map_type(type_th)

        # Bedrooms / bathrooms
        bed_m = re.search(rf'_{code}_bedRoom[^>]*>([^<]+)', block)
        bath_m = re.search(rf'_{code}_bathRoom[^>]*>([^<]+)', block)
        bedroom = int(bed_m.group(1).strip()) if bed_m else None
        bathroom = int(bath_m.group(1).strip()) if bath_m else None

        # Area (ไร่/งาน/ตร.ว.)
        size_m = re.search(r'result-card-propertysize[^>]*>.*?<span[^>]*>\s*([^<]+)', block, re.DOTALL)
        area_sqm = _parse_area_wa(size_m.group(1).strip()) if size_m else None

        # Price: prefer promoPrice (discounted), fallback to originalPrice
        promo_m = re.search(rf'_{code}_promoPrice[^>]*>([\d,\s]+(?:บาท|฿))', block)
        orig_m = re.search(rf'_{code}_originalPrice[^>]*>([\d,\s]+(?:บาท|฿))', block)
        price = _parse_price(promo_m.group(1) if promo_m else "") or \
                _parse_price(orig_m.group(1) if orig_m else "")

        if not price or price <= 0:
            continue

        deal: dict = {
            "listing_url":   f"{DETAIL_BASE}{code}",
            "source_domain": "krungsriproperty.com",
            "source_type":   "bank_npa",
            "property_type": ptype,
            "project_name":  project_name,
            "location":      location,
            "price":         price,
            "condition":     "fair",
            "is_benchmark":  False,
            "raw_data": {
                "code": code,
                "type_th": type_th,
                "bedroom": bedroom,
                "bathroom": bathroom,
            },
        }
        if area_sqm and area_sqm > 0:
            deal["land_area_sqm"] = area_sqm

        deals.append(deal)

    return deals


def _parse_total(html_raw: str) -> int:
    """Extract total result count from page."""
    html = html_lib.unescape(html_raw)
    m = re.search(r'totalResult[^>]+>\s*([\d,]+)', html)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return 0


class KrungsriHarvester:
    """
    Harvest NPA listings from Krungsri Property via HTML scraping.
    10 listings per page — paginates via ?page=N query param.
    """

    def __init__(self, delay: float = 1.5):
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 200,
        max_listings: int = 2000,
    ) -> list[dict]:
        results: list[dict] = []
        total_remote = 0

        async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            page = 1
            while page <= max_pages and len(results) < max_listings:
                try:
                    resp = await client.get(SEARCH_URL, params={"page": page})
                    if resp.status_code != 200:
                        logger.warning(f"Krungsri page {page}: HTTP {resp.status_code}")
                        break

                    if page == 1:
                        total_remote = _parse_total(resp.text)
                        logger.info(f"Krungsri: {total_remote} total listings")

                    deals = _parse_page(resp.text)
                    if not deals:
                        logger.info(f"Krungsri page {page}: no deals found, stopping")
                        break

                    results.extend(deals)
                    logger.info(
                        f"Krungsri page {page}: +{len(deals)} deals "
                        f"(total: {len(results)}/{total_remote})"
                    )

                    # Stop if last page
                    expected_pages = (total_remote + PER_PAGE - 1) // PER_PAGE if total_remote else max_pages
                    if page >= expected_pages or len(deals) < PER_PAGE:
                        break

                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"Krungsri page {page}: {e}")
                    break

        logger.info(f"Krungsri: harvested {len(results)} deals total")
        return results[:max_listings]
