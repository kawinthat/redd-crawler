"""
DDProperty Harvester — REST API (ddproperty.com)
ใช้เป็น Market Price Reference — ราคาตลาดจริงสำหรับ benchmark ROI
"""
import asyncio, httpx, logging
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.ddproperty.com"
SEARCH_API = f"{BASE_URL}/api/v2/listing/search"
DETAIL_URL = f"{BASE_URL}/property-for-sale/{{slug}}-{{prop_id}}"

TYPE_MAP = {
    "CONDO": "condo", "HOUSE": "house", "TOWNHOUSE": "townhouse",
    "LAND": "land", "COMMERCIAL": "other", "SHOPHOUSE": "other",
    "APT": "condo", "APARTMENT": "condo",
}
COND_MAP = {"NEW": "good", "GOOD": "good", "FAIR": "fair", "POOR": "poor"}

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "th",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/buy",
    "Origin": BASE_URL,
    "X-Requested-With": "XMLHttpRequest",
}


class DDPropertyHarvester:
    """
    Harvest sale listings from DDProperty.
    Returns normalized dicts for ROI benchmark comparison.
    """

    def __init__(self, rows_per_page: int = 30, delay: float = 1.5):
        self.rows_per_page = rows_per_page
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 20,
        max_listings: int = 500,
        province: str = "Bangkok",
    ) -> list[dict]:
        results: list[dict] = []
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            for page in range(1, max_pages + 1):
                if len(results) >= max_listings:
                    break
                try:
                    params = {
                        "market": "for-sale",
                        "page": page,
                        "pageSize": self.rows_per_page,
                        "sortBy": "price_asc",
                        "region_code_name": "bangkok",
                    }
                    resp = await client.get(SEARCH_API, params=params)
                    if resp.status_code != 200:
                        # Fallback: try different endpoint
                        resp = await client.get(
                            f"{BASE_URL}/api/v1/property-searches/",
                            params={"listing_type": "sale", "page": page, "page_size": self.rows_per_page}
                        )
                    if resp.status_code != 200:
                        logger.warning(f"DDProperty page {page}: HTTP {resp.status_code}")
                        break
                    data = resp.json()
                    items = (data.get("listings") or data.get("data") or
                             data.get("results") or data.get("items") or [])
                    if not items:
                        break
                    for item in items:
                        normalized = self._normalize(item)
                        if normalized:
                            results.append(normalized)
                    logger.info(f"DDProperty page {page}: +{len(items)} → total {len(results)}")
                    if len(items) < self.rows_per_page:
                        break
                    await asyncio.sleep(self.delay)
                except Exception as e:
                    logger.error(f"DDProperty page {page}: {e}")
                    break
        return results[:max_listings]

    def _normalize(self, item: dict) -> Optional[dict]:
        try:
            prop_id = (item.get("id") or item.get("listing_id") or
                       item.get("propertyId") or "")
            if not prop_id:
                return None
            price = float(item.get("price") or item.get("asking_price") or 0)
            area  = float(item.get("floor_size") or item.get("area") or
                          item.get("usable_area") or item.get("areaSqm") or 0)
            ptype_raw = str(item.get("property_type") or item.get("propertyType") or "").upper()
            ptype = TYPE_MAP.get(ptype_raw, "other")
            cond_raw = str(item.get("condition") or "GOOD").upper()
            cond = COND_MAP.get(cond_raw, "good")
            province = item.get("province") or item.get("region") or ""
            district = item.get("district") or item.get("area_name") or ""
            location = ", ".join(filter(None, [district, province]))
            title = item.get("title") or item.get("name") or item.get("project_name") or ""
            slug = str(item.get("slug") or title.lower().replace(" ", "-") or "property")
            url = item.get("url") or item.get("detail_url") or DETAIL_URL.format(slug=slug, prop_id=prop_id)
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
                "source_domain": "ddproperty.com",
                "is_benchmark":  True,  # ราคาตลาด — ไม่ใช่ NPA
            }
        except Exception as e:
            logger.debug(f"DDProperty normalize: {e}")
            return None

    async def fetch_urls_only(self, max_pages: int = 20) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
