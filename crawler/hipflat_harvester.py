"""
Hipflat Harvester — REST API (hipflat.co.th)
Market Price Reference สำหรับ condo มือสอง
"""
import asyncio, httpx, logging
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.hipflat.co.th"
SEARCH_API = f"{BASE_URL}/api/v2/listings"
DETAIL_URL = f"{BASE_URL}/sale/condo/{{slug}}-{{prop_id}}"

TYPE_MAP = {
    "condo": "condo", "house": "house", "townhouse": "townhouse",
    "land": "land", "villa": "house", "apartment": "condo",
}

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "th-TH",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": f"{BASE_URL}/sale",
    "Origin": BASE_URL,
}


class HipflatHarvester:
    def __init__(self, rows_per_page: int = 30, delay: float = 1.5):
        self.rows_per_page = rows_per_page
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 20,
        max_listings: int = 500,
    ) -> list[dict]:
        results: list[dict] = []
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            for page in range(1, max_pages + 1):
                if len(results) >= max_listings:
                    break
                try:
                    params = {
                        "transaction_type": "sale",
                        "page": page,
                        "per_page": self.rows_per_page,
                        "sort": "price_asc",
                    }
                    resp = await client.get(SEARCH_API, params=params)
                    if resp.status_code != 200:
                        resp = await client.get(
                            f"{BASE_URL}/api/v1/properties",
                            params={"for_sale": True, "page": page, "limit": self.rows_per_page}
                        )
                    if resp.status_code != 200:
                        logger.warning(f"Hipflat page {page}: HTTP {resp.status_code}")
                        break
                    data = resp.json()
                    items = (data.get("listings") or data.get("data") or
                             data.get("results") or data.get("properties") or [])
                    if not items:
                        break
                    for item in items:
                        normalized = self._normalize(item)
                        if normalized:
                            results.append(normalized)
                    logger.info(f"Hipflat page {page}: +{len(items)} → total {len(results)}")
                    if len(items) < self.rows_per_page:
                        break
                    await asyncio.sleep(self.delay)
                except Exception as e:
                    logger.error(f"Hipflat page {page}: {e}")
                    break
        return results[:max_listings]

    def _normalize(self, item: dict) -> Optional[dict]:
        try:
            prop_id = item.get("id") or item.get("listing_id") or ""
            if not prop_id:
                return None
            price = float(item.get("price") or item.get("asking_price") or 0)
            area  = float(item.get("floor_size") or item.get("area_sqm") or item.get("size") or 0)
            ptype_raw = str(item.get("property_type") or item.get("type") or "condo").lower()
            ptype = TYPE_MAP.get(ptype_raw, "condo")
            cond  = "good"  # Hipflat ส่วนใหญ่เป็นตลาดทั่วไป
            province = item.get("province") or item.get("region_name") or ""
            district = item.get("district") or item.get("area") or ""
            location = ", ".join(filter(None, [district, province]))
            title = item.get("title") or item.get("project_name") or item.get("name") or ""
            slug = str(item.get("slug") or "property")
            url = item.get("url") or item.get("permalink") or DETAIL_URL.format(slug=slug, prop_id=prop_id)
            if not url.startswith("http"):
                url = BASE_URL + url
            return {
                "source_url":    url,
                "price":         price,
                "area_sqm":      area,
                "property_type": ptype,
                "condition":     cond,
                "location":      location,
                "title":         title,
                "source_domain": "hipflat.co.th",
                "is_benchmark":  True,
            }
        except Exception as e:
            logger.debug(f"Hipflat normalize: {e}")
            return None

    async def fetch_urls_only(self, max_pages: int = 20) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
