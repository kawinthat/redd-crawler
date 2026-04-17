"""
LED Harvester — กรมบังคับคดี (led.go.th)
ทรัพย์บังคับคดีขายทอดตลาด — ราคาต่ำกว่าตลาดมาก (NPA ประเภทหนึ่ง)
"""
import asyncio, httpx, logging
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL   = "https://www.led.go.th"
# LED มี API สำหรับค้นหาทรัพย์บังคับคดี
SEARCH_API = f"{BASE_URL}/ewt/led_web2/asset"
ALT_API    = f"{BASE_URL}/api/asset/search"

TYPE_MAP = {
    "condo":        "condo",
    "house":        "house",
    "townhouse":    "townhouse",
    "land":         "land",
    "บ้าน":         "house",
    "คอนโด":        "condo",
    "ทาวน์เฮ้าส์":  "townhouse",
    "ที่ดิน":        "land",
    "อาคาร":        "other",
    "ห้องชุด":      "condo",
}

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/ewt/led_web2/asset",
    "X-Requested-With": "XMLHttpRequest",
}

# LED อาจใช้ Incapsula WAF — ลอง fallback URLs หลายแบบ
SEARCH_ENDPOINTS = [
    f"{BASE_URL}/ewt/led_web2/asset",
    f"{BASE_URL}/api/asset/list",
    f"{BASE_URL}/api/v1/properties",
    "https://led-asset.led.go.th/api/search",
    "https://asset.led.go.th/api/properties",
]


class LEDHarvester:
    """
    กรมบังคับคดี Harvester.
    ทรัพย์บังคับคดีมักถูกกว่าราคาตลาด 20-50% → ROI สูงมาก
    """

    def __init__(self, rows_per_page: int = 20, delay: float = 2.0):
        self.rows_per_page = rows_per_page
        self.delay = delay

    async def fetch_all(
        self,
        max_pages: int = 20,
        max_listings: int = 500,
    ) -> list[dict]:
        results: list[dict] = []
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=25, follow_redirects=True
        ) as client:
            for page in range(1, max_pages + 1):
                if len(results) >= max_listings:
                    break

                items = await self._try_fetch_page(client, page)
                if items is None:
                    # ลอง endpoint ถัดไปไม่ได้ผล → หยุด
                    logger.warning(f"LED: ไม่สามารถดึงหน้า {page} ได้ — หยุด")
                    break
                if not items:
                    logger.info(f"LED: หน้า {page} ว่างเปล่า — สิ้นสุด")
                    break

                for item in items:
                    normalized = self._normalize(item)
                    if normalized:
                        results.append(normalized)

                logger.info(f"LED page {page}: +{len(items)} → total {len(results)}")

                if len(items) < self.rows_per_page:
                    break

                await asyncio.sleep(self.delay)

        return results[:max_listings]

    async def _try_fetch_page(self, client: httpx.AsyncClient, page: int) -> Optional[list]:
        """ลอง endpoints หลายอันจนกว่าจะสำเร็จ"""
        # Method 1: POST JSON (รูปแบบ REST API ทั่วไป)
        payloads_and_endpoints = [
            (
                f"{BASE_URL}/ewt/led_web2/asset",
                {"page": page, "pageSize": self.rows_per_page, "assetType": "all"}
            ),
            (
                f"{BASE_URL}/api/asset/search",
                {"pageNo": page, "pageSize": self.rows_per_page}
            ),
            (
                "https://asset.led.go.th/api/properties",
                {"page": page, "limit": self.rows_per_page, "type": "all"}
            ),
        ]

        for endpoint, payload in payloads_and_endpoints:
            try:
                resp = await client.post(endpoint, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    items = (data.get("data") or data.get("results") or
                             data.get("assets") or data.get("items") or
                             data.get("properties") or [])
                    if items is not None:
                        return items
            except Exception as e:
                logger.debug(f"LED POST {endpoint}: {e}")

        # Method 2: GET params
        get_endpoints = [
            (f"{BASE_URL}/ewt/led_web2/asset", {"page": page, "limit": self.rows_per_page}),
            (f"{BASE_URL}/api/v1/properties", {"page": page, "per_page": self.rows_per_page}),
            ("https://led-asset.led.go.th/api/search", {"p": page, "n": self.rows_per_page}),
        ]

        for endpoint, params in get_endpoints:
            try:
                resp = await client.get(endpoint, params=params)
                if resp.status_code == 200:
                    # ลองแปลง JSON
                    try:
                        data = resp.json()
                        items = (data.get("data") or data.get("results") or
                                 data.get("assets") or data.get("properties") or [])
                        if items is not None:
                            return items
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"LED GET {endpoint}: {e}")

        # ถ้าทุก endpoint ล้มเหลว คืน None (จะหยุด loop)
        return None

    def _normalize(self, item: dict) -> Optional[dict]:
        try:
            prop_id = (item.get("id") or item.get("assetId") or
                       item.get("asset_id") or item.get("propertyId") or
                       item.get("caseNo") or "")
            if not prop_id:
                return None

            price = float(
                item.get("price") or item.get("appraisalPrice") or
                item.get("appraised_price") or item.get("startPrice") or
                item.get("start_price") or item.get("minPrice") or 0
            )
            area = float(
                item.get("usableArea") or item.get("usable_area") or
                item.get("area") or item.get("areaSqm") or
                item.get("size") or 0
            )

            ptype_raw = str(
                item.get("assetType") or item.get("asset_type") or
                item.get("propertyType") or item.get("type") or ""
            ).lower()
            ptype = TYPE_MAP.get(ptype_raw, "other")

            province = item.get("province") or item.get("changwat") or ""
            district = item.get("district") or item.get("amphoe") or ""
            location = ", ".join(filter(None, [district, province]))

            title = (item.get("title") or item.get("assetName") or
                     item.get("asset_name") or item.get("name") or "")

            # URL ของทรัพย์
            url = (item.get("url") or item.get("detail_url") or
                   item.get("permalink") or "")
            if not url:
                url = f"{BASE_URL}/ewt/led_web2/asset-detail?id={prop_id}"
            elif not url.startswith("http"):
                url = BASE_URL + url

            # วันที่ขายทอดตลาด (ถ้ามี)
            sale_date = item.get("auctionDate") or item.get("auction_date") or ""

            return {
                "source_url":    url,
                "price":         price,
                "area_sqm":      area,
                "property_type": ptype,
                "condition":     "fair",  # ทรัพย์บังคับคดีมักสภาพปานกลาง
                "location":      location,
                "title":         title,
                "source_domain": "led.go.th",
                "is_benchmark":  False,  # เป็น NPA จริง ไม่ใช่ราคาตลาด
                "auction_date":  sale_date,
            }
        except Exception as e:
            logger.debug(f"LED normalize: {e}")
            return None

    async def fetch_urls_only(self, max_pages: int = 20) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
