"""
GH Bank NPA Harvester — REST API (ghbhomecenter.com)
API: https://www.ghbhomecenter.com/api/property/search
"""
import asyncio
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.ghbhomecenter.com"
SEARCH_API = f"{BASE_URL}/api/property/search"
DETAIL_URL = f"{BASE_URL}/property/detail/{{prop_id}}"

# Property type mapping
TYPE_MAP = {
    "1": "house", "2": "condo", "3": "townhouse",
    "4": "land",  "5": "other"
}

COND_MAP = {
    "1": "good", "2": "fair", "3": "poor", "good": "good",
    "fair": "fair", "poor": "poor", "ดี": "good", "พอใช้": "fair", "ต้องซ่อม": "poor"
}

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": BASE_URL,
}


class GHBankHarvester:
    """
    Harvest NPA listings from GH Bank via REST API.
    No Playwright needed — pure httpx.
    """

    def __init__(self, rows_per_page: int = 50, delay: float = 1.0):
        self.rows_per_page = rows_per_page
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 30,
        max_listings: int = 1000,
    ) -> list[dict]:
        """
        Returns list of normalized dicts:
        {source_url, price, area_sqm, property_type, condition, location, title, source_domain}
        """
        results: list[dict] = []
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            for page in range(1, max_pages + 1):
                if len(results) >= max_listings:
                    break
                try:
                    payload = {
                        "page": page,
                        "limit": self.rows_per_page,
                        "propertyType": "",
                        "province": "",
                        "minPrice": 0,
                        "maxPrice": 0,
                    }
                    resp = await client.post(SEARCH_API, json=payload)
                    if resp.status_code != 200:
                        logger.warning(f"GHBank page {page}: HTTP {resp.status_code}")
                        break
                    data = resp.json()
                    items = data.get("data") or data.get("items") or data.get("result") or []
                    if not items:
                        break
                    for item in items:
                        normalized = self._normalize(item)
                        if normalized:
                            results.append(normalized)
                    total = data.get("total") or data.get("totalCount") or 0
                    logger.info(f"GHBank page {page}: +{len(items)} (total {len(results)}/{total})")
                    if len(items) < self.rows_per_page:
                        break
                    await asyncio.sleep(self.delay)
                except Exception as e:
                    logger.error(f"GHBank page {page} error: {e}")
                    break
        return results[:max_listings]

    def _normalize(self, item: dict) -> Optional[dict]:
        try:
            prop_id = item.get("propertyId") or item.get("id") or item.get("propId") or ""
            if not prop_id:
                return None
            price = float(item.get("price") or item.get("salePrice") or item.get("askingPrice") or 0)
            area  = float(item.get("areaSqm") or item.get("area") or item.get("usableArea") or 0)
            ptype_raw = str(item.get("propertyType") or item.get("typeCode") or "5")
            ptype = TYPE_MAP.get(ptype_raw, "other")
            cond_raw = str(item.get("condition") or item.get("conditionCode") or item.get("assetCondition") or "fair").lower()
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
                "source_domain": "ghbhomecenter.com",
            }
        except Exception as e:
            logger.debug(f"GHBank normalize error: {e}")
            return None

    async def fetch_urls_only(self, max_pages: int = 30) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
