"""
kkp_harvester.py — KKP Propify (ธนาคารเกียรตินาคินภัทร)
URL: https://kkppropify.kkpfg.com/th/npa

ลอง 2 แนวทาง:
1. REST API (Next.js _next/data หรือ /api/properties)
2. HTML scraping ด้วย BeautifulSoup (fallback)

Note: KKP Propify ใช้ Next.js → มี __NEXT_DATA__ JSON ใน HTML
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional
from urllib.parse import urljoin, urlencode

import httpx
from bs4 import BeautifulSoup
from loguru import logger

BASE_URL   = "https://kkppropify.kkpfg.com"
LIST_URL   = f"{BASE_URL}/th/npa"
DETAIL_BASE = f"{BASE_URL}/th/npa"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/html, */*",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Referer":         BASE_URL,
}

TYPE_MAP = {
    "บ้านเดี่ยว": "house",  "บ้านแฝด": "house",  "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse", "ทาวน์โฮม": "townhouse",
    "คอนโด": "condo",     "ห้องชุด": "condo",
    "ที่ดิน": "land",
    "อาคารพาณิชย์": "commercial", "อาคาร": "commercial",
    "house": "house", "townhouse": "townhouse", "condo": "condo",
    "land": "land",   "commercial": "commercial",
}


def _map_type(txt: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw.lower() in (txt or "").lower():
            return ptype
    return "other"


def _parse_price(v) -> Optional[int]:
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None


def _parse_area(v) -> Optional[float]:
    try:
        a = float(str(v).replace(",", ""))
        return round(a, 2) if a > 0 else None
    except Exception:
        return None


def _normalize_api(item: dict) -> Optional[dict]:
    """แปลง KKP API record → deal dict"""
    price = _parse_price(
        item.get("price") or item.get("salePrice") or item.get("sellPrice") or 0
    )
    if not price or price <= 0:
        return None

    prop_id = item.get("id") or item.get("assetId") or item.get("propertyId") or ""
    slug    = item.get("slug") or item.get("code") or ""
    url     = (item.get("url") or
               (f"{DETAIL_BASE}/{slug}" if slug else
                f"{DETAIL_BASE}?id={prop_id}"))

    ptype_raw = str(
        item.get("propertyType") or item.get("assetType") or
        item.get("type") or item.get("typeName") or ""
    )
    ptype = _map_type(ptype_raw)

    location = ", ".join(filter(None, [
        str(item.get("district") or item.get("districtName") or ""),
        str(item.get("province") or item.get("provinceName") or ""),
    ]))

    area = _parse_area(
        item.get("usableArea") or item.get("area") or item.get("buildingArea") or 0
    )
    land = _parse_area(item.get("landArea") or item.get("landAreaSqm") or 0)

    deal: dict = {
        "listing_url":   url,
        "source_domain": "kkppropify.kkpfg.com",
        "source_type":   "bank_npa",
        "property_type": ptype,
        "project_name":  str(item.get("projectName") or item.get("name") or ""),
        "location":      location,
        "price":         price,
        "condition":     "fair",
    }
    if area and area > 0 and ptype != "land":
        deal["area_sqm"] = area
    if land and land > 0:
        deal["land_area_sqm"] = land

    return deal


def _parse_next_data(html: str) -> list[dict]:
    """ดึง deals จาก __NEXT_DATA__ JSON ของ Next.js"""
    deals: list[dict] = []
    try:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return []
        nd = json.loads(m.group(1))
        # หาข้อมูล properties ใน pageProps
        props = nd.get("props", {}).get("pageProps", {})
        items = (
            props.get("properties") or props.get("npaList") or
            props.get("assets") or props.get("data") or
            props.get("items") or []
        )
        if isinstance(items, dict):
            items = items.get("data") or items.get("items") or []
        for item in items:
            d = _normalize_api(item)
            if d:
                deals.append(d)
    except Exception as e:
        logger.warning(f"KKP __NEXT_DATA__ parse: {e}")
    return deals


def _parse_html_cards(html: str) -> list[dict]:
    """HTML fallback — parse card elements"""
    deals: list[dict] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(
            ".property-card, .npa-card, .asset-card, "
            "[class*='PropertyCard'], [class*='AssetCard'], "
            "[class*='npa-item'], article"
        )
        for card in cards:
            text = card.get_text(" ", strip=True)
            # ราคา
            m_price = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:บาท|฿)", text)
            if not m_price:
                continue
            price = _parse_price(m_price.group(1))
            if not price or price < 100_000:
                continue

            link = card.find("a", href=True)
            url  = urljoin(BASE_URL, link["href"]) if link else LIST_URL

            # ประเภท + ทำเล
            ptype = _map_type(text)
            m_loc = re.search(r"(?:จ\.|จังหวัด)\s*([ก-๙a-zA-Z]+)", text)
            location = m_loc.group(1) if m_loc else ""

            deals.append({
                "listing_url":   url,
                "source_domain": "kkppropify.kkpfg.com",
                "source_type":   "bank_npa",
                "property_type": ptype,
                "price":         price,
                "condition":     "fair",
                "location":      location,
            })
    except Exception as e:
        logger.error(f"KKP HTML parse: {e}")
    return deals


class KKPHarvester:
    """
    KKP Propify Harvester
    ลอง: 1) REST API  2) __NEXT_DATA__ JSON  3) HTML cards
    """

    # API endpoint candidates (Next.js ทั่วไป)
    API_CANDIDATES = [
        "/api/npa",
        "/api/properties",
        "/api/assets",
        "/th/npa/api",
    ]

    def __init__(self, delay: float = 1.5):
        self.delay = delay

    async def fetch_all(
        self, max_pages: int = 999_999, max_listings: int = 999_999
    ) -> list[dict]:
        results: list[dict] = []

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            # ── ลอง REST API ก่อน ────────────────────────────────────────
            api_results = await self._try_api(client, max_pages, max_listings)
            if api_results:
                logger.info(f"KKP API mode: {len(api_results)} deals")
                return api_results[:max_listings]

            # ── ลอง SSR scraping (HTML + __NEXT_DATA__) ──────────────────
            logger.info("KKP: ลอง HTML scraping mode")
            page = 1
            while page <= max_pages and len(results) < max_listings:
                try:
                    url = f"{LIST_URL}?page={page}" if page > 1 else LIST_URL
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(f"KKP page {page}: HTTP {resp.status_code}")
                        break

                    # ลอง __NEXT_DATA__ ก่อน
                    items = _parse_next_data(resp.text)
                    if not items:
                        items = _parse_html_cards(resp.text)
                    if not items:
                        logger.info(f"KKP page {page}: ว่าง — หยุด")
                        break

                    results.extend(items)
                    logger.info(f"KKP page {page}: +{len(items)} (total {len(results)})")

                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"KKP page {page}: {e}")
                    break

        logger.info(f"KKP: harvested {len(results)} total")
        return results[:max_listings]

    async def _try_api(
        self, client: httpx.AsyncClient, max_pages: int, max_listings: int
    ) -> list[dict]:
        """ลอง REST API endpoints — คืน list ถ้าสำเร็จ, [] ถ้าล้มเหลว"""
        for endpoint in self.API_CANDIDATES:
            for page in range(1, min(max_pages, 3) + 1):  # ลอง 3 หน้าแรก
                try:
                    url = f"{BASE_URL}{endpoint}"
                    resp = await client.get(url, params={"page": page, "limit": 50})
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    items = (
                        data.get("data") or data.get("items") or
                        data.get("properties") or data.get("assets") or []
                    )
                    if isinstance(items, dict):
                        items = items.get("data") or []
                    if not items:
                        break
                    # API ทำงาน!
                    results = [r for r in (_normalize_api(i) for i in items) if r]
                    if results:
                        logger.info(f"KKP API {endpoint} page {page}: +{len(results)}")
                        # ดึงต่อจนครบ
                        all_results = list(results)
                        for pg in range(2, max_pages + 1):
                            if len(all_results) >= max_listings:
                                break
                            try:
                                r2 = await client.get(url, params={"page": pg, "limit": 50})
                                d2 = r2.json()
                                it2 = (d2.get("data") or d2.get("items") or
                                       d2.get("properties") or [])
                                if not it2:
                                    break
                                all_results.extend(
                                    r for r in (_normalize_api(i) for i in it2) if r
                                )
                                await asyncio.sleep(self.delay)
                            except Exception:
                                break
                        return all_results[:max_listings]
                except Exception:
                    break
        return []
