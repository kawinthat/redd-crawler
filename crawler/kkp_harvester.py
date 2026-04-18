"""
kkp_harvester.py — KKP Propify (ธนาคารเกียรตินาคินภัทร)
URL: https://kkppropify.kkpfg.com/th/npa

ลอง 3 แนวทาง:
1. REST API (Next.js _next/data หรือ /api/properties)
2. __NEXT_DATA__ JSON ใน HTML (SSR Next.js)
3. HTML scraping ด้วย BeautifulSoup (fallback)

Note: KKP Propify ใช้ Next.js → มักมี __NEXT_DATA__ JSON ใน HTML
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

BASE_URL    = "https://kkppropify.kkpfg.com"
LIST_URL    = f"{BASE_URL}/th/npa"
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
    "บ้านเดี่ยว": "house",   "บ้านแฝด": "house", "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse", "ทาวน์โฮม": "townhouse",
    "ทาวน์เฮาส์": "townhouse",
    "คอนโด": "condo",       "ห้องชุด": "condo", "อาคารชุด": "condo",
    "ที่ดิน": "land",        "ที่ดินเปล่า": "land",
    "อาคารพาณิชย์": "commercial", "อาคาร": "commercial", "ตึกแถว": "commercial",
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
        p = int(float(str(v).replace(",", "")))
        return p if p >= 100_000 else None
    except Exception:
        return None


def _parse_area(v) -> Optional[float]:
    try:
        a = float(str(v).replace(",", ""))
        return round(a, 2) if a > 0 else None
    except Exception:
        return None


def _normalize_api(item: dict, source_url: str = "") -> Optional[dict]:
    """แปลง KKP API/JSON record → deal dict"""
    # ราคา — ลองหลาย field
    price = None
    for pk in ("price", "salePrice", "sellPrice", "asking_price", "askingPrice",
               "startPrice", "startingPrice", "minPrice", "amount"):
        price = _parse_price(item.get(pk))
        if price:
            break
    if not price:
        return None

    # ID + URL
    prop_id = (item.get("id") or item.get("assetId") or item.get("propertyId")
               or item.get("npaId") or "")
    slug    = item.get("slug") or item.get("code") or item.get("assetCode") or ""
    url     = (
        item.get("url") or item.get("link") or item.get("detailUrl") or
        (f"{DETAIL_BASE}/{slug}" if slug else
         f"{DETAIL_BASE}?id={prop_id}" if prop_id else source_url)
    )

    # ประเภท
    ptype_raw = str(
        item.get("propertyType") or item.get("assetType") or item.get("type") or
        item.get("typeName") or item.get("property_type") or ""
    )
    ptype = _map_type(ptype_raw)

    # Location
    location = ", ".join(filter(None, [
        str(item.get("district") or item.get("districtName") or item.get("subDistrict") or ""),
        str(item.get("province") or item.get("provinceName") or ""),
    ]))
    if not location:
        location = str(item.get("location") or item.get("address") or "")

    # Area
    area = _parse_area(
        item.get("usableArea") or item.get("area") or item.get("buildingArea") or
        item.get("floorArea") or item.get("usable_area") or 0
    )
    land = _parse_area(
        item.get("landArea") or item.get("landAreaSqm") or item.get("land_area") or 0
    )

    deal: dict = {
        "listing_url":   url,
        "source_domain": "kkppropify.kkpfg.com",
        "source_type":   "bank_npa",
        "property_type": ptype,
        "project_name":  str(item.get("projectName") or item.get("name") or item.get("title") or ""),
        "location":      location or None,
        "price":         price,
        "condition":     "fair",
    }
    if area and area > 0 and ptype != "land":
        deal["area_sqm"] = area
    if land and land > 0:
        deal["land_area_sqm"] = land
    return deal


def _deep_find_list(obj, depth: int = 0) -> list:
    """Recursively find the first list of dicts (likely the property list) in a JSON object"""
    if depth > 6:
        return []
    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        # Check if items look like properties (have price-like or type-like keys)
        sample = obj[0]
        prop_keys = {"price", "salePrice", "sellPrice", "askingPrice", "id",
                     "assetId", "propertyId", "npaId", "slug", "code",
                     "propertyType", "assetType", "type", "province", "district"}
        if prop_keys & set(sample.keys()):
            return obj
    if isinstance(obj, dict):
        # Try common keys first
        for key in ("data", "items", "properties", "assets", "npaList", "list",
                    "result", "records", "rows", "content", "npa"):
            if key in obj:
                found = _deep_find_list(obj[key], depth + 1)
                if found:
                    return found
        # Then try all keys
        for v in obj.values():
            found = _deep_find_list(v, depth + 1)
            if found:
                return found
    return []


def _parse_next_data(html: str, source_url: str = "") -> list[dict]:
    """ดึง deals จาก __NEXT_DATA__ JSON ของ Next.js"""
    deals: list[dict] = []
    try:
        m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                      html, re.DOTALL)
        if not m:
            # Try without id attribute
            m = re.search(r'__NEXT_DATA__\s*=\s*(\{.*?\})(?:\s*;|\s*</script>)',
                          html, re.DOTALL)
        if not m:
            return []

        nd = json.loads(m.group(1))
        logger.debug(f"KKP __NEXT_DATA__ keys: {list(nd.keys())[:8]}")

        # Deep search for the property list
        items = _deep_find_list(nd)
        if not items:
            logger.debug("KKP __NEXT_DATA__: no property list found via deep search")
            return []

        for item in items:
            d = _normalize_api(item, source_url)
            if d:
                deals.append(d)
        logger.info(f"KKP __NEXT_DATA__: {len(deals)} deals parsed")
    except Exception as e:
        logger.warning(f"KKP __NEXT_DATA__ parse error: {e}")
    return deals


def _parse_html_cards(html: str, source_url: str = "") -> list[dict]:
    """HTML fallback — parse card elements with multiple selector strategies"""
    deals: list[dict] = []
    soup = BeautifulSoup(html, "html.parser")

    CARD_SELECTORS = [
        # ชื่อ class จาก Next.js (มักเป็น hash หรือ semantic)
        "[class*='PropertyCard']", "[class*='AssetCard']", "[class*='NpaCard']",
        "[class*='property-card']", "[class*='asset-card']", "[class*='npa-card']",
        "[class*='npa-item']", "[class*='property-item']",
        # Generic patterns
        "article", ".card", "[data-testid*='property']", "[data-testid*='asset']",
        # KKP-specific guesses
        "[class*='PropertyList'] > div", "[class*='NpaList'] > div",
        ".col-md-4", ".col-sm-6",  # grid columns often contain cards
    ]

    for selector in CARD_SELECTORS:
        try:
            cards = soup.select(selector)
            if len(cards) < 2:
                continue

            parsed: list[dict] = []
            for card in cards:
                text = card.get_text(" ", strip=True)
                if not text or len(text) < 20:
                    continue

                # ราคา
                price = None
                for price_sel in ["[class*='price']", "[class*='Price']",
                                  "strong", "b", "span[class*='value']"]:
                    el = card.select_one(price_sel)
                    if el:
                        price = _parse_price(el.get_text())
                        if price:
                            break
                if not price:
                    for m in re.finditer(r"([\d,]{6,}(?:\.\d+)?)", text):
                        p = _parse_price(m.group(1))
                        if p and p >= 100_000:
                            price = p
                            break
                if not price:
                    continue

                # Link
                link = card.find("a", href=True)
                url  = urljoin(BASE_URL, link["href"]) if link else source_url

                # ประเภท + ทำเล
                ptype = _map_type(text)
                m_loc = re.search(r"(?:จ\.|จังหวัด)\s*([ก-๙a-zA-Z]+)", text)
                location = m_loc.group(1) if m_loc else ""

                # Area
                area = None
                m_area = re.search(r"([\d,]+\.?\d*)\s*ตร\.?ม", text)
                if m_area:
                    area = _parse_area(m_area.group(1))

                d: dict = {
                    "listing_url":   url,
                    "source_domain": "kkppropify.kkpfg.com",
                    "source_type":   "bank_npa",
                    "property_type": ptype,
                    "price":         price,
                    "condition":     "fair",
                    "location":      location or None,
                }
                if area and area > 0 and ptype != "land":
                    d["area_sqm"] = area
                parsed.append(d)

            if parsed:
                logger.debug(f"KKP HTML selector '{selector}': {len(parsed)} deals")
                deals.extend(parsed)
                break  # ใช้ selector แรกที่ได้ผล

        except Exception as e:
            logger.debug(f"KKP selector '{selector}' error: {e}")
            continue

    return deals


class KKPHarvester:
    """
    KKP Propify Harvester
    ลำดับ: 1) REST API  2) __NEXT_DATA__ JSON  3) HTML cards
    """

    # API endpoint candidates (Next.js ทั่วไป + KKP-specific guesses)
    API_CANDIDATES = [
        "/api/npa",
        "/api/properties",
        "/api/assets",
        "/th/npa/api",
        "/api/npa/list",
        "/api/v1/npa",
        "/api/v1/properties",
    ]

    def __init__(self, delay: float = 1.5):
        self.delay = delay

    async def fetch_all(
        self, max_pages: int = 999_999, max_listings: int = 999_999
    ) -> list[dict]:
        results: list[dict] = []
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(
            headers=HEADERS, timeout=30, follow_redirects=True
        ) as client:
            # ── ลอง REST API ก่อน ────────────────────────────────────────────
            api_results = await self._try_api(client, max_pages, max_listings)
            if api_results:
                logger.info(f"KKP API mode: {len(api_results)} deals")
                return api_results[:max_listings]

            # ── ลอง SSR scraping (HTML + __NEXT_DATA__) ──────────────────────
            logger.info("KKP: ลอง HTML/__NEXT_DATA__ scraping mode")
            page = 1
            consecutive_empty = 0

            while page <= max_pages and len(results) < max_listings:
                try:
                    url = f"{LIST_URL}?page={page}" if page > 1 else LIST_URL
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(f"KKP page {page}: HTTP {resp.status_code}")
                        break

                    # ลอง __NEXT_DATA__ ก่อน
                    items = _parse_next_data(resp.text, url)
                    if not items:
                        items = _parse_html_cards(resp.text, url)

                    # Dedup
                    new_items = [x for x in items
                                 if x.get("listing_url") and x["listing_url"] not in seen_urls]
                    for x in new_items:
                        seen_urls.add(x["listing_url"])

                    if not new_items:
                        consecutive_empty += 1
                        logger.info(f"KKP page {page}: ว่าง (empty={consecutive_empty})")
                        if consecutive_empty >= 3:
                            break
                    else:
                        consecutive_empty = 0
                        results.extend(new_items)
                        logger.info(f"KKP page {page}: +{len(new_items)} (total {len(results)})")

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
            try:
                url = f"{BASE_URL}{endpoint}"
                resp = await client.get(url, params={"page": 1, "limit": 50, "per_page": 50})
                if resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue

                # Deep search for property list
                items = _deep_find_list(data)
                if not items:
                    continue

                # API works! Paginate
                all_results = [r for r in (_normalize_api(i) for i in items) if r]
                if not all_results:
                    continue

                logger.info(f"KKP API {endpoint}: {len(all_results)} deals (page 1)")

                for pg in range(2, min(max_pages, 100) + 1):
                    if len(all_results) >= max_listings:
                        break
                    try:
                        r2 = await client.get(url, params={"page": pg, "limit": 50})
                        d2 = r2.json()
                        it2 = _deep_find_list(d2)
                        if not it2:
                            break
                        new = [r for r in (_normalize_api(i) for i in it2) if r]
                        if not new:
                            break
                        all_results.extend(new)
                        await asyncio.sleep(self.delay)
                    except Exception:
                        break

                return all_results[:max_listings]

            except Exception:
                continue
        return []
