"""
SCB NPA Harvester — REST API (scbnpa.com)
API: https://www.scbnpa.com/api/v1/property/search
"""
import asyncio
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.scbnpa.com"
SEARCH_API = f"{BASE_URL}/api/v1/property/search"
DETAIL_URL = f"{BASE_URL}/propertyDetail/{{prop_id}}"

TYPE_MAP = {
    "CONDO": "condo", "HOUSE": "house", "TOWNHOUSE": "townhouse",
    "LAND": "land", "COMMERCIAL": "other", "SHOPHOUSE": "other",
    "1": "house", "2": "condo", "3": "townhouse", "4": "land", "5": "other",
}
COND_MAP = {
    "GOOD": "good", "FAIR": "fair", "POOR": "poor",
    "1": "good", "2": "fair", "3": "poor",
    "ดี": "good", "พอใช้": "fair", "ต้องซ่อม": "poor",
}

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "th-TH,th;q=0.9",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
}


class SCBNPAHarvester:
    """
    Harvest NPA from SCB NPA via REST API.
    No Playwright needed.
    """

    def __init__(self, rows_per_page: int = 50, delay: float = 1.0):
        self.rows_per_page = rows_per_page
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 40,
        max_listings: int = 2000,
    ) -> list[dict]:
        results: list[dict] = []
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            for page in range(1, max_pages + 1):
                if len(results) >= max_listings:
                    break
                try:
                    params = {
                        "page": page,
                        "pageSize": self.rows_per_page,
                        "sortBy": "price",
                        "sortOrder": "asc",
                    }
                    resp = await client.get(SEARCH_API, params=params)
                    if resp.status_code != 200:
                        # Try POST fallback
                        resp = await client.post(SEARCH_API, json={
                            "page": page, "pageSize": self.rows_per_page
                        })
                    if resp.status_code != 200:
                        logger.warning(f"SCB NPA page {page}: HTTP {resp.status_code}")
                        break
                    data = resp.json()
                    items = (data.get("data") or data.get("items") or
                             data.get("result") or data.get("properties") or [])
                    if not items:
                        break
                    for item in items:
                        normalized = self._normalize(item)
                        if normalized:
                            results.append(normalized)
                    total = data.get("total") or data.get("totalCount") or 0
                    logger.info(f"SCB NPA page {page}: +{len(items)} (total {len(results)}/{total})")
                    if len(items) < self.rows_per_page:
                        break
                    await asyncio.sleep(self.delay)
                except Exception as e:
                    logger.error(f"SCB NPA page {page}: {e}")
                    break
        return results[:max_listings]

    def _normalize(self, item: dict) -> Optional[dict]:
        try:
            prop_id = (item.get("propertyId") or item.get("id") or
                       item.get("assetId") or item.get("collGrpId") or "")
            if not prop_id:
                return None
            price = float(item.get("price") or item.get("salePrice") or item.get("askingPrice") or 0)
            area  = float(item.get("areaSqm") or item.get("area") or item.get("usableArea") or 0)
            ptype_raw = str(item.get("propertyType") or item.get("assetType") or "").upper()
            ptype = TYPE_MAP.get(ptype_raw, "other")
            cond_raw = str(item.get("condition") or item.get("assetCondition") or "").upper()
            cond = COND_MAP.get(cond_raw, "fair")
            province = item.get("province") or item.get("provinceName") or ""
            district = item.get("district") or item.get("districtName") or ""
            location = ", ".join(filter(None, [district, province]))
            title = item.get("title") or item.get("assetName") or item.get("name") or ""
            return {
                "source_url":    DETAIL_URL.format(prop_id=prop_id),
                "price":         price,
                "area_sqm":      area,
                "property_type": ptype,
                "condition":     cond,
                "location":      location,
                "title":         title,
                "source_domain": "scbnpa.com",
            }
        except Exception as e:
            logger.debug(f"SCB normalize error: {e}")
            return None

    async def fetch_urls_only(self, max_pages: int = 40) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
