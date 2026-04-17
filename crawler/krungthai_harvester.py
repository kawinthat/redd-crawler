"""
krungthai_harvester.py — Direct API Harvester for npa.krungthai.com
ใช้ REST API โดยตรง ไม่ต้อง Playwright → เร็วกว่า 10x
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
from loguru import logger


API_BASE = "https://npa.krungthai.com/api/v1/product"
DETAIL_URL = "https://npa.krungthai.com/propertyDetail/{collGrpId}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://npa.krungthai.com/",
    "Origin": "https://npa.krungthai.com",
}


class KrungthaiHarvester:
    """
    ดึง listing ทั้งหมดจาก npa.krungthai.com ผ่าน REST API
    ไม่ต้องใช้ Playwright — httpx เพียงพอ

    Usage:
        harvester = KrungthaiHarvester()
        listings = await harvester.fetch_all(max_pages=10)
        # listings = list[dict] พร้อม price, area, location, type, url
    """

    ROWS_PER_PAGE = 50  # max ที่ API รองรับได้ดี

    def __init__(self, rows_per_page: int = 50, delay: float = 0.8):
        self.rows_per_page = rows_per_page
        self.delay = delay  # วินาที ระหว่างหน้า

    async def _post_page(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        page: int,
        type_prop: list | None = None,
    ) -> tuple[list[dict], dict]:
        """POST ไปยัง API และคืน (dataResponse, paging)"""
        body = {
            "typeProp": type_prop or [],
            "paging": {
                "totalRows": 0,
                "rowsPerPage": self.rows_per_page,
                "currentPage": page,
            },
        }
        r = await client.post(f"{API_BASE}{endpoint}", json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("dataResponse", []), data.get("paging", {})

    def _normalize(self, raw: dict) -> dict:
        """แปลง raw API record → normalized listing dict"""
        coll_grp_id = raw.get("collGrpId", "")
        return {
            "source_url": DETAIL_URL.format(collGrpId=coll_grp_id),
            "source_site": "npa.krungthai.com",
            "external_id": str(coll_grp_id),
            "property_type": raw.get("collCateName", ""),
            "location": (
                f"{raw.get('shrProvinceName', '')} "
                f"{raw.get('shrAmphurName', '')}".strip()
            ),
            "province": raw.get("shrProvinceName", ""),
            "district": raw.get("shrAmphurName", ""),
            "area_rai": raw.get("calSumAreaCollGrpId", ""),
            "asking_price": raw.get("nmlPrice") or raw.get("appraisalPrice"),
            "appraisal_price": raw.get("appraisalPrice"),
            "image_url": (
                f"https://npa.krungthai.com/api/v1/product/getImage/"
                f"{raw.get('fileName', '')}"
                if raw.get("fileName")
                else None
            ),
            "is_flash_sale": False,
            "raw": raw,
        }

    async def fetch_all(
        self,
        max_pages: int = 140,
        max_listings: int = 2000,
        endpoint: str = "/homePage",
    ) -> list[dict]:
        """
        ดึง listing ทุกหน้าจาก API

        Args:
            max_pages:    จำนวนหน้าสูงสุด (default=140 = ทั้งหมด)
            max_listings: จำนวน listing สูงสุด (default=2000)
            endpoint:     API endpoint ("/homePage" หรือ "/flashSale")

        Returns:
            list[dict]: normalized listing records พร้อม source_url
        """
        results: list[dict] = []

        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            # Page 1 — get total
            dr, paging = await self._post_page(client, endpoint, page=1)
            total_pages = min(paging.get("pageCount", 1), max_pages)
            total_rows = paging.get("totalRows", 0)

            logger.info(
                f"KrungthaiHarvester: totalRows={total_rows}, "
                f"pageCount={paging.get('pageCount')}, "
                f"fetching up to {total_pages} pages (max_listings={max_listings})"
            )

            results.extend(self._normalize(r) for r in dr)
            logger.info(f"  Page 1/{total_pages}: +{len(dr)} items")

            for page in range(2, total_pages + 1):
                if len(results) >= max_listings:
                    logger.info(f"  ✅ ถึง max_listings ({max_listings}) แล้ว — หยุด")
                    break
                await asyncio.sleep(self.delay + random.uniform(0, 0.4))
                try:
                    dr, _ = await self._post_page(client, endpoint, page=page)
                    results.extend(self._normalize(r) for r in dr)
                    logger.info(f"  Page {page}/{total_pages}: +{len(dr)} items (total={len(results)})")
                except Exception as exc:
                    logger.warning(f"  Page {page} failed: {exc} — skipping")

        logger.success(f"KrungthaiHarvester done: {len(results)} listings")
        return results[:max_listings]

    async def fetch_urls_only(self, max_pages: int = 140) -> list[str]:
        """คืนแค่ list[str] ของ detail URLs"""
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
