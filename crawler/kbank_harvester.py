"""
kbank_harvester.py — KBank Property For Sale
URL: https://www.kasikornbank.com/th/propertyforsale/search/pages/index.aspx

⚠️ SETUP ที่ต้องทำก่อน:
1. เปิด Chrome → F12 → Network → XHR/Fetch
2. เปิด kasikornbank.com/th/propertyforsale/... แล้ว scroll หน้า
3. หา request ที่ return JSON ของรายการทรัพย์
4. Copy URL + payload มาแทนที่ KBANK_API_ENDPOINT + _payload()
5. Copy headers ที่สำคัญ เช่น Cookie, __RequestVerificationToken มาใส่ใน EXTRA_HEADERS

เหตุที่ต้องทำแบบนี้: Incapsula WAF + ASP.NET ViewState token
"""
from __future__ import annotations
import asyncio, re
from typing import Optional
import httpx
from loguru import logger

# ─── แก้ค่าเหล่านี้หลัง inspect DevTools ──────────────────────────────────
KBANK_API_ENDPOINT = (
    "https://www.kasikornbank.com/th/propertyforsale/search/pages"
    "/GetPropertyList.aspx"          # ← endpoint เดา ต้องตรวจสอบจาก DevTools
)
KBANK_PROMO_ENDPOINT = (
    "https://www.kasikornbank.com/th/propertyforsale/search/pages"
    "/GetPropertyList.aspx?tabname=PromotionPropertie"
)
EXTRA_HEADERS: dict = {
    # ใส่ cookie/token จาก DevTools ถ้า API ต้องการ session
    # "__RequestVerificationToken": "xxx",
    # "Cookie": "ASP.NET_SessionId=xxx; VisitorId=xxx",
}
# ─────────────────────────────────────────────────────────────────────────

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Referer": "https://www.kasikornbank.com/th/propertyforsale/",
    "X-Requested-With": "XMLHttpRequest",
    **EXTRA_HEADERS,
}

TYPE_MAP = {
    "บ้าน": "house", "บ้านเดี่ยว": "house",
    "ทาวน์": "townhouse",
    "คอนโด": "condo", "ห้องชุด": "condo",
    "ที่ดิน": "land",
    "อาคาร": "commercial",
    "house": "house", "condo": "condo", "townhouse": "townhouse",
    "land": "land",   "commercial": "commercial",
}


def _parse_type(txt: str) -> str:
    for kw, pt in TYPE_MAP.items():
        if kw.lower() in txt.lower():
            return pt
    return "other"


class KBankHarvester:
    """
    KBank NPA/Promotion Harvester.
    ต้องการ KBANK_API_ENDPOINT ที่ถูกต้องจาก Chrome DevTools ก่อนจึงจะทำงาน
    """
    def __init__(self, page_size: int = 12, delay: float = 2.0, promo: bool = False):
        self.page_size = page_size
        self.delay     = delay
        self.endpoint  = KBANK_PROMO_ENDPOINT if promo else KBANK_API_ENDPOINT

    async def fetch_all(self, max_pages: int = 999999, max_listings: int = 999999) -> list[dict]:
        results = []
        async with httpx.AsyncClient(headers=BASE_HEADERS, timeout=30, follow_redirects=True) as client:
            page = 1
            while page <= max_pages and len(results) < max_listings:
                items = await self._fetch_page(client, page)
                if not items:
                    logger.warning(f"KBank: หน้า {page} ว่าง — หยุด")
                    break
                results.extend(items)
                logger.info(f"KBank page {page}: +{len(items)} (total {len(results)})")
                if len(items) < self.page_size:
                    break
                page += 1
                await asyncio.sleep(self.delay)
        logger.info(f"KBank: harvested {len(results)} total")
        return results[:max_listings]

    async def _fetch_page(self, client: httpx.AsyncClient, page: int) -> list[dict]:
        # ── ลอง format ต่างๆ ─────────────────────────────────────────────
        # format เหล่านี้เป็น guess จาก pattern ASP.NET ทั่วไป
        # ต้องแก้ตาม DevTools จริง
        attempts = [
            # Format 1: JSON POST (REST API pattern)
            {"method": "POST", "url": self.endpoint,
             "json": {"pageNo": page, "pageSize": self.page_size, "propertyType": "all"}},
            # Format 2: GET params
            {"method": "GET", "url": self.endpoint,
             "params": {"page": page, "pageSize": self.page_size}},
            # Format 3: Form POST (ASP.NET WebMethod)
            {"method": "POST", "url": self.endpoint,
             "data": {"MethodCall": "GetList", "pageIndex": page - 1, "pageSize": self.page_size}},
        ]
        for attempt in attempts:
            try:
                method = attempt.pop("method")
                resp = await client.post(**attempt) if method == "POST" else await client.get(**attempt)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        # ลอง key ต่างๆ ที่ API อาจ return
                        items = (data.get("data") or data.get("properties") or
                                 data.get("result") or data.get("items") or
                                 data.get("d") or   # ASP.NET WebMethod pattern
                                 [])
                        if isinstance(items, str):  # d เป็น string JSON อีกชั้น
                            import json as _json
                            items = _json.loads(items)
                        if items:
                            return [r for r in (self._normalize(i) for i in items) if r]
                    except Exception:
                        # HTML response → parse
                        return self._parse_html(resp.text)
            except Exception as e:
                logger.debug(f"KBank attempt failed: {e}")
        logger.warning(
            "KBank: ทุก endpoint ล้มเหลว\n"
            "→ เปิด Chrome DevTools → Network → XHR\n"
            "→ เปิด kasikornbank.com/th/propertyforsale\n"
            "→ scroll/กดค้นหา → copy URL ของ request JSON\n"
            "→ ใส่ใน KBANK_API_ENDPOINT และ EXTRA_HEADERS"
        )
        return []

    def _parse_html(self, html: str) -> list[dict]:
        """HTML fallback สำหรับ server-rendered pages"""
        results = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select(".property-item, .npa-item, .asset-card, [class*='property']")
            for card in cards:
                text = card.get_text(" ", strip=True)
                m = re.search(r"([\d,]+)\s*บาท", text)
                if not m:
                    continue
                price = float(m.group(1).replace(",", ""))
                href = card.find("a", href=True)
                url = href["href"] if href else ""
                if not url.startswith("http"):
                    url = "https://www.kasikornbank.com" + url
                results.append({
                    "listing_url":   url,
                    "source_domain": "kasikornbank.com",
                    "source_type":   "bank_npa",
                    "property_type": _parse_type(text),
                    "price":         int(price),
                    "condition":     "fair",
                })
        except ImportError:
            logger.warning("KBank HTML scraper: ต้องการ beautifulsoup4")
        except Exception as e:
            logger.error(f"KBank HTML parse: {e}")
        return results

    def _normalize(self, item: dict) -> Optional[dict]:
        try:
            price = float(
                item.get("price") or item.get("Price") or
                item.get("sellPrice") or item.get("offerPrice") or 0
            )
            if price <= 0:
                return None
            pid = item.get("id") or item.get("Id") or item.get("propertyId") or ""
            # Fix: parentheses ensure url resolution before conditional
            url = (
                item.get("url") or item.get("Url") or item.get("detailUrl") or
                (f"https://www.kasikornbank.com/th/propertyforsale/detail/{pid}" if pid else "")
            )
            orig = float(item.get("originalPrice") or item.get("appraisePrice") or 0)
            raw = dict(item)
            if orig > price:
                raw["original_price"] = orig
                raw["is_promotion"] = True
            return {
                "listing_url":   url,
                "source_domain": "kasikornbank.com",
                "source_type":   "bank_npa",
                "property_type": _parse_type(
                    str(item.get("propertyType") or item.get("type") or "")
                ),
                "price":         int(price),
                "area_sqm":      float(item.get("usableArea") or item.get("area") or 0) or None,
                "condition":     "fair",
                "location":      ", ".join(filter(None, [
                    str(item.get("district") or item.get("amphoe") or ""),
                    str(item.get("province") or ""),
                ])),
                "project_name":  str(item.get("name") or item.get("title") or ""),
                "raw_data":      raw,
            }
        except Exception:
            return None
