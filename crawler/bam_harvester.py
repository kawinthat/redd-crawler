"""
bam_harvester.py — บริษัทบริหารสินทรัพย์ กรุงเทพพาณิชย์ (BAM)
API: POST https://bam-els-sync-api-prd.bam.co.th/api/asset-detail/search
Returns JSON with totalData + data[] (201 status = success)

Fields: id, assetNo, province, district, subDistrict, projectTH,
        assetType, sellPrice, areaMeter, areaWa, rai, ngan, wa,
        bedroom, bathroom, condition, isHotDeal
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from loguru import logger

API_URL = "https://bam-els-sync-api-prd.bam.co.th/api/asset-detail/search"
DETAIL_BASE = "https://www.bam.co.th/npa/detail"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Origin": "https://www.bam.co.th",
    "Referer": "https://www.bam.co.th/npa",
    "Content-Type": "application/json",
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
    "ที่ดินเปล่า": "land",
    "อาคารพาณิชย์": "commercial",
    "อาคาร": "commercial",
    "ตึกแถว": "commercial",
    "โรงงาน": "commercial",
    "สำนักงาน": "commercial",
}


def _map_type(asset_type: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw in (asset_type or ""):
            return ptype
    return "other"


def _parse_area_sqm(item: dict) -> Optional[float]:
    """Convert BAM area fields → sqm."""
    # areaMeter = sqm directly
    if item.get("areaMeter") and float(item["areaMeter"]) > 0:
        return round(float(item["areaMeter"]), 2)
    return None


def _parse_land_sqm(item: dict) -> Optional[float]:
    """rai/ngan/wa → sqm. 1 rai = 1600 sqm, 1 ngan = 400 sqm, 1 wa = 4 sqm."""
    rai = float(item.get("rai") or 0)
    ngan = float(item.get("ngan") or 0)
    wa = float(item.get("wa") or item.get("areaWa") or 0)
    total = rai * 1600 + ngan * 400 + wa * 4
    return round(total, 2) if total > 0 else None


def _record_to_deal(item: dict) -> Optional[dict]:
    """Map BAM API record → RE:DD deal schema."""
    price = item.get("sellPrice") or item.get("discountPrice") or 0
    if not price or int(price) <= 0:
        return None

    asset_type = item.get("assetType", "")
    ptype = _map_type(asset_type)
    location_parts = [
        item.get("district", ""),
        item.get("province", ""),
    ]
    location = " ".join(p for p in location_parts if p).strip()

    # Listing URL
    asset_no = item.get("assetNo", "")
    listing_url = f"{DETAIL_BASE}?assetNo={asset_no}" if asset_no else ""

    # Area: use areaMeter for built area, rai/ngan/wa for land area
    area_sqm = _parse_area_sqm(item)
    land_sqm = _parse_land_sqm(item)

    # Condition: BAM has 'condition' field
    cond_raw = (item.get("condition") or "").lower()
    if "new" in cond_raw or "ใหม่" in cond_raw:
        condition = "new"
    elif "good" in cond_raw or "ดี" in cond_raw:
        condition = "good"
    elif "poor" in cond_raw or "ซ่อม" in cond_raw or "เสีย" in cond_raw:
        condition = "poor"
    else:
        condition = "fair"

    deal: dict = {
        "listing_url":   listing_url,
        "source_domain": "bam.co.th",
        "source_type":   "bank_npa",
        "property_type": ptype,
        "project_name":  item.get("projectTH") or "",
        "location":      location,
        "price":         int(price),
        "condition":     condition,
        "is_benchmark":  False,
        "raw_data":      {
            "asset_no":   asset_no,
            "asset_type": asset_type,
            "bedroom":    item.get("bedroom"),
            "bathroom":   item.get("bathroom"),
            "is_hot_deal": item.get("isHotDeal"),
        },
    }
    if area_sqm and area_sqm > 0:
        deal["area_sqm"] = area_sqm
    if land_sqm and land_sqm > 0:
        deal["land_area_sqm"] = land_sqm

    return deal


class BAMHarvester:
    """
    Harvest NPA listings from BAM via official REST API.
    POST https://bam-els-sync-api-prd.bam.co.th/api/asset-detail/search
    Returns 201 on success with data[] + totalData.
    """

    def __init__(self, page_size: int = 100, delay: float = 1.0):
        self.page_size = page_size
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 50,
        max_listings: int = 2000,
    ) -> list[dict]:
        results: list[dict] = []

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            page = 1
            while page <= max_pages and len(results) < max_listings:
                payload = {
                    "pageSize":       self.page_size,
                    "pageNumber":     page,
                    "inputText":      "",
                    "assetType":      "",
                    "bedroom":        "",
                    "bathroom":       "",
                    "startMeter":     "",
                    "endMeter":       "",
                    "province":       "",
                    "district":       "",
                    "firstPriceRange": "",
                    "secondPriceRange": "",
                    "sortby":         "",
                    "startTwoMeter":  "",
                    "endTwoMeter":    "",
                    "nearby":         "",
                    "groupType":      "",
                    "highLight":      "",
                    "isCampaign":     "",
                    "campaignName":   "",
                    "stars":          "",
                    "userKey":        "redd-crawler",
                }
                try:
                    resp = await client.post(API_URL, json=payload)
                    if resp.status_code not in (200, 201):
                        logger.warning(f"BAM page {page}: HTTP {resp.status_code}")
                        break

                    data = resp.json()
                    items = data.get("data") or []
                    total = data.get("totalData", 0)

                    if not items:
                        logger.info(f"BAM page {page}: no items, stopping")
                        break

                    page_deals = []
                    for item in items:
                        deal = _record_to_deal(item)
                        if deal:
                            page_deals.append(deal)

                    results.extend(page_deals)
                    logger.info(
                        f"BAM page {page}: +{len(page_deals)} deals "
                        f"(total so far: {len(results)}/{total})"
                    )

                    # Stop if we've fetched all
                    if len(results) >= total or len(items) < self.page_size:
                        break

                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"BAM page {page}: {e}")
                    break

        logger.info(f"BAM: harvested {len(results)} deals total")
        return results[:max_listings]
