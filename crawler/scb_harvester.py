"""
scb_harvester.py — SCB Asset (ธนาคารไทยพาณิชย์) NPA
API: GET https://asset.home.scb/api/project/cmd?command=get_project&page=N&limit=100
Returns JSON: {s: "y", m: "Success", d: [...], total: 3986}

Fields: project_id, project_type, project_title, price, price_discount,
        project_address_detail, project_address (district, province),
        area_use, area_sqm, land_area, latitude, longitude,
        image_use → "stocks/project/c300x200/{gen_path}/{img}"
        (prepend https://asset.home.scb/ for full URL)
        project_type_name (Thai type label)
        project_sold_out (T/F), project_booking (T/F)

~3,986 total listings at 100/page → ~40 pages.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from loguru import logger

BASE_URL   = "https://asset.home.scb"
API_URL    = f"{BASE_URL}/api/project/cmd"
IMAGE_BASE = f"{BASE_URL}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/project",
}

TYPE_MAP = {
    "house":        "house",
    "บ้านเดี่ยว":  "house",
    "บ้านแฝด":     "house",
    "townhouse":    "townhouse",
    "ทาวน์เฮ้าส์": "townhouse",
    "ทาวน์โฮม":    "townhouse",
    "ทาวน์เฮาส์":  "townhouse",
    "condominiums": "condo",
    "คอนโด":       "condo",
    "ห้องชุด":     "condo",
    "คอนโดมิเนียม":"condo",
    "อาคารชุด":    "condo",
    "land":         "land",
    "ที่ดิน":      "land",
    "commercial":   "commercial",
    "อาคารพาณิชย์":"commercial",
}

LIMIT = 100


def _map_type(ptype_raw: str, ptype_name: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw in (ptype_raw or ""):
            return ptype
    for kw, ptype in TYPE_MAP.items():
        if kw in (ptype_name or ""):
            return ptype
    return "other"


def _parse_price(item: dict) -> Optional[int]:
    """Return discounted price if > 0, else original price."""
    try:
        discount = int(str(item.get("price_discount") or "0").replace(",", ""))
        if discount > 0:
            return discount
    except (ValueError, TypeError):
        pass
    try:
        raw = str(item.get("price") or "0").replace(",", "")
        val = int(float(raw))
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def _record_to_deal(item: dict) -> Optional[dict]:
    """Map SCB Asset project record → RE:DD deal schema."""
    price = _parse_price(item)
    if not price or price <= 0:
        return None

    project_type      = item.get("project_type", "")
    project_type_name = item.get("project_type_name", "")
    ptype = _map_type(project_type, project_type_name)

    # Location: "district, province" stored in project_address field
    location = (item.get("project_address") or "").strip()

    project_id   = item.get("project_id", "")
    slug         = item.get("slug", "")
    listing_url  = f"{BASE_URL}/project/{slug}" if slug else f"{BASE_URL}/project?id={project_id}"

    # Area: area_sqm / area_use (built), land_area (land)
    area_sqm  = None
    land_sqm  = None
    try:
        a = float(item.get("area_sqm") or item.get("area_use") or 0)
        if a > 0:
            area_sqm = round(a, 2)
    except (TypeError, ValueError):
        pass
    try:
        la = float(item.get("land_area") or 0)
        if la > 0:
            land_sqm = round(la, 2)
    except (TypeError, ValueError):
        pass

    # Image: image_use = "stocks/project/c300x200/xx/xx/{gen}/{img}.png"
    images = []
    img_path = item.get("image_use") or ""
    if img_path:
        images = [f"{IMAGE_BASE}{img_path}"]

    deal: dict = {
        "listing_url":   listing_url,
        "source_domain": "asset.home.scb",
        "source_type":   "bank_npa",
        "property_type": ptype,
        "project_name":  item.get("project_title") or "",
        "location":      location,
        "price":         price,
        "condition":     "fair",
        "is_benchmark":  False,
        "raw_data": {
            "project_id":   str(project_id),
            "project_type": project_type,
            "sold_out":     item.get("project_sold_out") == "T",
            "booking":      item.get("project_booking") == "T",
            "lat":          item.get("latitude"),
            "lng":          item.get("longitude"),
            "images":       images,
        },
    }
    if area_sqm:
        deal["area_sqm"] = area_sqm
    if land_sqm:
        deal["land_area_sqm"] = land_sqm

    return deal


class SCBHarvester:
    """
    Harvest NPA listings from SCB Asset via REST API.
    GET /api/project/cmd?command=get_project&page=N&limit=100
    Returns {s:"y", d:[...], total:3986}
    ~40 pages at limit=100.
    """

    def __init__(self, limit: int = 100, delay: float = 1.5):
        self.limit = limit
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 60,
        max_listings: int = 5000,
    ) -> list[dict]:
        results: list[dict] = []
        total_remote = 0

        async with httpx.AsyncClient(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            page = 1
            while page <= max_pages and len(results) < max_listings:
                try:
                    resp = await client.get(
                        API_URL,
                        params={"command": "get_project", "page": page, "limit": self.limit},
                    )
                    if resp.status_code != 200:
                        logger.warning(f"SCB page {page}: HTTP {resp.status_code}")
                        break

                    data = resp.json()
                    if data.get("s") != "y":
                        logger.warning(f"SCB page {page}: api error {data.get('m')}")
                        break

                    items = data.get("d") or []
                    if not items:
                        logger.info(f"SCB page {page}: no items, stopping")
                        break

                    if page == 1:
                        total_remote = int(data.get("total") or 0)
                        logger.info(f"SCB: {total_remote} total listings")

                    page_deals = []
                    for item in items:
                        deal = _record_to_deal(item)
                        if deal:
                            page_deals.append(deal)

                    results.extend(page_deals)
                    logger.info(
                        f"SCB page {page}: +{len(page_deals)} deals "
                        f"(total: {len(results)}/{total_remote})"
                    )

                    # Stop if fetched all
                    expected_pages = (total_remote + self.limit - 1) // self.limit if total_remote else max_pages
                    if page >= expected_pages or len(items) < self.limit:
                        break

                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"SCB page {page}: {e}")
                    break

        logger.info(f"SCB: harvested {len(results)} deals total")
        return results[:max_listings]
