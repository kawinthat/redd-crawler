"""
sam_harvester.py — บริษัทบริหารสินทรัพย์ (SAM / บสก.)
URL: https://sam.or.th/site/npa/page_list.php

SAM เป็น PHP site แสดงผลเป็น HTML table
ดึงข้อมูล: ประเภท, ทำเล, ราคา, ขนาด, link รายละเอียด

สังเกต: SAM ใช้ pagination ด้วย GET param page=N
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

BASE_URL  = "https://sam.or.th"
LIST_URL  = "https://sam.or.th/site/npa/page_list.php"
DETAIL_BASE = "https://sam.or.th/site/npa/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
    "Referer":         BASE_URL,
}

TYPE_MAP = {
    "บ้านเดี่ยว": "house",  "บ้านแฝด": "house",  "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse", "ทาวน์โฮม": "townhouse",
    "ทาวน์เฮาส์": "townhouse",
    "คอนโด": "condo",      "ห้องชุด": "condo", "อาคารชุด": "condo",
    "ที่ดิน": "land",       "ที่ดินเปล่า": "land",
    "อาคารพาณิชย์": "commercial", "อาคาร": "commercial",
    "ตึกแถว": "commercial", "โรงงาน": "commercial",
}


def _map_type(txt: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw in (txt or ""):
            return ptype
    return "other"


def _parse_price(txt: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d.]", "", (txt or "").replace(",", ""))
    try:
        return int(float(cleaned)) if cleaned else None
    except Exception:
        return None


def _parse_area(txt: str) -> Optional[float]:
    """แปลงพื้นที่ เช่น '45.50 ตร.ม.' หรือ '1-2-50 ไร่' → sqm"""
    txt = (txt or "").strip()

    # ตร.ม. โดยตรง
    m = re.search(r"([\d.]+)\s*ตร\.?ม", txt)
    if m:
        return round(float(m.group(1)), 2)

    # ไร่-งาน-วา → sqm
    m2 = re.search(r"(\d+)-(\d+)-(\d+)", txt)
    if m2:
        rai, ngan, wa = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return round(rai * 1600 + ngan * 400 + wa * 4, 2)

    # ตัวเลขล้วน (sqm)
    m3 = re.search(r"([\d.]+)", txt)
    if m3:
        v = float(m3.group(1))
        return round(v, 2) if v > 0 else None
    return None


def _parse_page(html: str, source_url: str) -> list[dict]:
    """Parse HTML ของ SAM listing page → list of deal dicts"""
    deals: list[dict] = []
    try:
        soup = BeautifulSoup(html, "html.parser")

        # SAM แสดงผลเป็น card หรือ table row
        # ลอง card pattern ก่อน
        cards = soup.select(".npa-item, .property-item, .asset-card, .list-item, article")
        if not cards:
            # fallback: row-based table
            cards = soup.select("tr.property-row, tr[class*='asset'], table tr")

        for card in cards:
            text = card.get_text(" ", strip=True)
            if not text or len(text) < 10:
                continue

            # ราคา
            price_el = card.select_one(".price, [class*='price'], .npa-price, .asset-price")
            price_txt = price_el.get_text() if price_el else ""
            if not price_txt:
                # หาราคาจาก text ทั้ง card
                m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:บาท|฿)", text)
                price_txt = m.group(1) if m else ""
            price = _parse_price(price_txt)
            if not price or price < 100_000:
                continue  # ราคาต่ำเกินไป = noise

            # ประเภท
            type_el = card.select_one("[class*='type'], [class*='category']")
            type_txt = type_el.get_text() if type_el else text

            # ทำเล
            loc_el = card.select_one("[class*='location'], [class*='province'], [class*='area']")
            location = loc_el.get_text(strip=True) if loc_el else ""
            if not location:
                # หาชื่อจังหวัดจาก text
                m_loc = re.search(r"(?:จ\.|จังหวัด)\s*([ก-๙a-zA-Z]+)", text)
                location = m_loc.group(1) if m_loc else ""

            # พื้นที่
            area_el = card.select_one("[class*='area'], [class*='size'], [class*='sqm']")
            area_txt = area_el.get_text() if area_el else ""
            area = _parse_area(area_txt) if area_txt else None

            # Link
            link = card.find("a", href=True)
            if link:
                href = link["href"]
                detail_url = urljoin(BASE_URL, href) if not href.startswith("http") else href
            else:
                detail_url = source_url

            ptype = _map_type(type_txt)
            deal: dict = {
                "listing_url":   detail_url,
                "source_domain": "sam.or.th",
                "source_type":   "bank_npa",
                "property_type": ptype,
                "price":         price,
                "condition":     "fair",
                "location":      location,
            }
            if area and area > 0:
                if ptype == "land":
                    deal["land_area_sqm"] = area
                else:
                    deal["area_sqm"] = area

            deals.append(deal)

    except Exception as e:
        logger.error(f"SAM parse error: {e}")
    return deals


def _find_next_page(soup: BeautifulSoup, current_page: int) -> Optional[str]:
    """หา URL หน้าถัดไป"""
    # ลอง rel="next"
    nxt = soup.find("a", rel="next")
    if nxt and nxt.get("href"):
        return urljoin(BASE_URL, nxt["href"])
    # ลอง page N+1 link
    nxt2 = soup.find("a", string=str(current_page + 1))
    if nxt2 and nxt2.get("href"):
        return urljoin(BASE_URL, nxt2["href"])
    # ลอง "ถัดไป"
    nxt3 = soup.find("a", string=re.compile("ถัดไป|next", re.I))
    if nxt3 and nxt3.get("href"):
        return urljoin(BASE_URL, nxt3["href"])
    return None


class SAMHarvester:
    """
    บสก. (SAM) HTML Scraper
    ดึงจาก sam.or.th/site/npa/page_list.php ด้วย httpx + BeautifulSoup
    """

    def __init__(self, delay: float = 2.0):
        self.delay = delay

    async def fetch_all(
        self, max_pages: int = 999_999, max_listings: int = 999_999
    ) -> list[dict]:
        results: list[dict] = []
        params = {
            "s_product_type": "",
            "s_province":     "",
            "s_district":     "",
            "s_status_id":    "",
            "key_search":     "",
        }

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            page = 1
            current_url = LIST_URL

            while page <= max_pages and len(results) < max_listings:
                try:
                    if page == 1:
                        resp = await client.get(LIST_URL, params=params)
                    else:
                        resp = await client.get(current_url)

                    if resp.status_code != 200:
                        logger.warning(f"SAM page {page}: HTTP {resp.status_code}")
                        break

                    items = _parse_page(resp.text, current_url)
                    if not items:
                        logger.info(f"SAM page {page}: ว่าง — หยุด")
                        break

                    results.extend(items)
                    logger.info(f"SAM page {page}: +{len(items)} (total {len(results)})")

                    # หน้าถัดไป
                    soup = BeautifulSoup(resp.text, "html.parser")
                    next_url = _find_next_page(soup, page)
                    if not next_url or next_url == current_url:
                        logger.info("SAM: ไม่มีหน้าถัดไป")
                        break

                    current_url = next_url
                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"SAM page {page}: {e}")
                    break

        logger.info(f"SAM: harvested {len(results)} total")
        return results[:max_listings]
