"""
gsb_harvester.py — ธนาคารออมสิน (GSB) NPA
URL: https://npa-assets.gsb.or.th/?page=N&page_size=50
Strategy: SSR — each page embeds __NEXT_DATA__ JSON with listNpa.data.

Response structure:
  pageProps.listNpa.data = list of asset-type groups, each with:
    asset_name (str)        → type label (ที่ดิน, ที่ดินพร้อมสิ่งปลูกสร้าง, คอนโด...)
    asset_type_id (int)
    asset_type_count (int)  → items in current batch (= page_size)
    total_page (int)        → WARNING: often returns 1 even when more pages exist;
                               stop when all groups return 0 items instead
    asset_list (list)       → actual property records

Record fields:
  asset_id, asset_group_id_npa, asset_type_desc, asset_subtype_desc
  xprice (discounted), xprice_normal (original), current_offer_price
  sum_rai, sum_ngan, sum_square_wa  → land area
  square_meter                      → built area sqm
  sub_district_name, district_name, province_name
  image_id  → https://npa-assets.gsb.or.th/npa/image?id={image_id}&asset_type_id={type_id}
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
from typing import Optional

import httpx
from loguru import logger

BASE_URL = "https://npa-assets.gsb.or.th"
SEARCH_URL = BASE_URL + "/"
IMAGE_URL = BASE_URL + "/npa/image"
DETAIL_BASE = BASE_URL + "/asset/gsb"

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
    "ห้องชุด": "condo",
    "อาคารชุด": "condo",
    "ที่ดินพร้อมสิ่งปลูกสร้าง": "house",
    "ที่ดิน": "land",
    "อาคารพาณิชย์": "commercial",
    "อาคาร": "commercial",
}

PAGE_SIZE = 50


def _map_type(type_th: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw in (type_th or ""):
            return ptype
    return "other"


def _parse_land_sqm(item: dict) -> Optional[float]:
    """sum_rai/sum_ngan/sum_square_wa → sqm."""
    rai  = float(item.get("sum_rai")  or 0)
    ngan = float(item.get("sum_ngan") or 0)
    wa   = float(item.get("sum_square_wa") or 0)
    total = rai * 1600 + ngan * 400 + wa * 4
    return round(total, 2) if total > 0 else None


def _record_to_deal(item: dict) -> Optional[dict]:
    """Map GSB asset record → RE:DD deal schema."""
    # Price: prefer discounted (xprice), fallback to xprice_normal / current_offer_price
    price = (
        item.get("xprice")
        or item.get("xprice_normal")
        or item.get("current_offer_price")
        or item.get("group_sell_price")
        or 0
    )
    try:
        price = int(float(price))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    asset_type_desc  = item.get("asset_type_desc", "")
    asset_subtype    = item.get("asset_subtype_desc", "")
    ptype = _map_type(asset_subtype or asset_type_desc)

    location_parts = [
        item.get("district_name", ""),
        item.get("province_name", ""),
    ]
    location = " ".join(p for p in location_parts if p).strip()

    asset_id       = item.get("asset_id", "")
    asset_group_id = item.get("asset_group_id_npa") or item.get("asset_group_id") or ""
    listing_url    = f"{DETAIL_BASE}?id={asset_id}" if asset_id else ""

    # Area
    land_sqm  = _parse_land_sqm(item)
    built_sqm = None
    try:
        sqm = float(item.get("square_meter") or 0)
        if sqm > 0:
            built_sqm = round(sqm, 2)
    except (TypeError, ValueError):
        pass

    # Image URL
    image_id      = item.get("image_id")
    asset_type_id = item.get("asset_type_id")
    images = []
    if image_id and asset_type_id:
        images = [f"{IMAGE_URL}?id={image_id}&asset_type_id={asset_type_id}"]

    deal: dict = {
        "listing_url":   listing_url,
        "source_domain": "npa-assets.gsb.or.th",
        "source_type":   "bank_npa",
        "property_type": ptype,
        "project_name":  asset_group_id,
        "location":      location,
        "price":         price,
        "condition":     "fair",
        "is_benchmark":  False,
        "raw_data": {
            "asset_id":       str(asset_id),
            "asset_group_id": asset_group_id,
            "asset_type":     asset_type_desc,
            "asset_subtype":  asset_subtype,
            "province":       item.get("province_name", ""),
            "district":       item.get("district_name", ""),
            "images":         images,
        },
    }
    if built_sqm:
        deal["area_sqm"] = built_sqm
    if land_sqm:
        deal["land_area_sqm"] = land_sqm

    return deal


def _parse_page(html_raw: str) -> tuple[list[dict], bool]:
    """
    Parse one GSB SSR page → (deals list, has_more).
    Returns has_more=False when all asset groups returned 0 items.
    """
    try:
        nd_m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html_raw, re.DOTALL
        )
        if not nd_m:
            return [], False
        nd = json.loads(nd_m.group(1))
        props = nd["props"]["pageProps"]
    except Exception as e:
        logger.warning(f"GSB __NEXT_DATA__ parse error: {e}")
        return [], False

    list_npa = props.get("listNpa", {})
    groups   = list_npa.get("data", [])

    deals: list[dict] = []
    any_items = False

    for group in groups:
        asset_list = group.get("asset_list", [])
        if asset_list:
            any_items = True
        for item in asset_list:
            deal = _record_to_deal(item)
            if deal:
                deals.append(deal)

    return deals, any_items


class GSBHarvester:
    """
    Harvest NPA listings from GSB via SSR HTML scraping.
    Each page embeds __NEXT_DATA__ JSON with listNpa.data.
    50 items/page across 3 asset-type groups (150 max per request).
    """

    def __init__(self, delay: float = 2.0):
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 100,
        max_listings: int = 2000,
    ) -> list[dict]:
        results: list[dict] = []

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            page = 1
            while page <= max_pages and len(results) < max_listings:
                try:
                    resp = await client.get(SEARCH_URL, params={"page": page, "page_size": PAGE_SIZE})
                    if resp.status_code != 200:
                        logger.warning(f"GSB page {page}: HTTP {resp.status_code}")
                        break

                    deals, has_more = _parse_page(resp.text)

                    if not has_more:
                        logger.info(f"GSB page {page}: no more items, stopping")
                        break

                    results.extend(deals)
                    logger.info(
                        f"GSB page {page}: +{len(deals)} deals "
                        f"(total: {len(results)})"
                    )

                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"GSB page {page}: {e}")
                    break

        logger.info(f"GSB: harvested {len(results)} deals total")
        return results[:max_listings]
