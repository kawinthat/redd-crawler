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
from urllib.parse import urljoin, urlencode

import httpx
from bs4 import BeautifulSoup
from loguru import logger

BASE_URL    = "https://sam.or.th"
LIST_URL    = "https://sam.or.th/site/npa/page_list.php"
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
    "บ้านเดี่ยว": "house",   "บ้านแฝด": "house",  "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse", "ทาวน์โฮม": "townhouse",
    "ทาวน์เฮาส์": "townhouse", "ทาวน์เฮ้าส์": "townhouse",
    "คอนโด": "condo",       "ห้องชุด": "condo",  "อาคารชุด": "condo",
    "ที่ดิน": "land",        "ที่ดินเปล่า": "land",
    "อาคารพาณิชย์": "commercial", "อาคาร": "commercial",
    "ตึกแถว": "commercial",  "โรงงาน": "commercial",
}

# จังหวัด keyword สำหรับ extract location
PROVINCE_KEYWORDS = [
    "กรุงเทพ", "นนทบุรี", "ปทุมธานี", "สมุทรปราการ", "นครปฐม",
    "เชียงใหม่", "ขอนแก่น", "ชลบุรี", "ระยอง", "ภูเก็ต",
    "สุราษฎร์", "นครราชสีมา", "อุดร", "สงขลา", "พระนครศรีอยุธยา",
    "สระบุรี", "ลพบุรี", "อ่างทอง", "สิงห์บุรี", "ชัยนาท",
]


def _map_type(txt: str) -> str:
    for kw, ptype in TYPE_MAP.items():
        if kw in (txt or ""):
            return ptype
    return "other"


def _parse_price(txt: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d.]", "", (txt or "").replace(",", ""))
    try:
        v = float(cleaned) if cleaned else None
        return int(v) if v and v >= 100_000 else None
    except Exception:
        return None


def _parse_area(txt: str) -> Optional[float]:
    """แปลงพื้นที่ เช่น '45.50 ตร.ม.' หรือ '1-2-50 ไร่' → sqm"""
    txt = (txt or "").strip()
    m = re.search(r"([\d,]+\.?\d*)\s*ตร\.?ม", txt)
    if m:
        return round(float(m.group(1).replace(",", "")), 2)
    m2 = re.search(r"(\d+)-(\d+)-(\d+)", txt)
    if m2:
        rai, ngan, wa = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return round(rai * 1600 + ngan * 400 + wa * 4, 2)
    m3 = re.search(r"([\d,]+\.?\d*)\s*(?:ไร่|ตร\.?ว)", txt)
    if m3:
        v = float(m3.group(1).replace(",", ""))
        return round(v * 1600, 2) if "ไร่" in txt else round(v * 4, 2)
    return None


def _extract_location(text: str) -> str:
    """ดึงชื่อจังหวัด/เขตจาก free text"""
    for kw in PROVINCE_KEYWORDS:
        if kw in text:
            m = re.search(rf"([ก-๙a-zA-Z]*{kw}[ก-๙a-zA-Z]*)", text)
            if m:
                return m.group(1)
    m_prov = re.search(r"(?:จ\.|จังหวัด|จ\.)\s*([ก-๙a-zA-Z]+)", text)
    if m_prov:
        return m_prov.group(1)
    return ""


def _parse_page(html: str, source_url: str) -> list[dict]:
    """
    Universal parser สำหรับ SAM listing page
    ลองหลายกลยุทธ์: card selectors → table rows → regex fallback
    """
    if not html:
        return []

    deals: list[dict] = []
    soup = BeautifulSoup(html, "html.parser")

    # ─── กลยุทธ์ 1: CSS card selectors ──────────────────────────────────────
    CARD_SELECTORS = [
        ".npa-item", ".property-item", ".asset-card", ".list-item",
        "[class*='npa-item']", "[class*='property-item']", "[class*='asset-card']",
        "[class*='property_item']", "[class*='asset_card']", "[class*='item-property']",
        "article", ".card", ".col-property", ".property",
        "[class*='product']", "[class*='listing']",
    ]
    for selector in CARD_SELECTORS:
        try:
            cards = soup.select(selector)
            if cards:
                parsed = _parse_cards(cards, source_url)
                if parsed:
                    logger.debug(f"SAM: card selector '{selector}' → {len(parsed)} deals")
                    return parsed
        except Exception:
            continue

    # ─── กลยุทธ์ 2: Table rows ──────────────────────────────────────────────
    table_deals = _parse_tables(soup, source_url)
    if table_deals:
        logger.debug(f"SAM: table parser → {len(table_deals)} deals")
        return table_deals

    # ─── กลยุทธ์ 3: div rows (common in PHP sites) ──────────────────────────
    ROW_SELECTORS = [
        ".row[class*='property']", "div[class*='row-property']",
        "li[class*='property']", "li[class*='asset']", "li[class*='item']",
        ".list-group-item", "tr",
    ]
    for selector in ROW_SELECTORS:
        try:
            rows = soup.select(selector)
            if len(rows) > 2:
                parsed = _parse_cards(rows, source_url)
                if parsed:
                    logger.debug(f"SAM: row selector '{selector}' → {len(parsed)} deals")
                    return parsed
        except Exception:
            continue

    # ─── กลยุทธ์ 4: Regex scan — หาราคาและ link ทั้งหน้า ───────────────────
    regex_deals = _regex_parse(html, source_url)
    if regex_deals:
        logger.debug(f"SAM: regex fallback → {len(regex_deals)} deals")
        return regex_deals

    logger.warning(f"SAM: ไม่พบ deals ใน {source_url[:60]} (HTML len={len(html)})")
    # Debug: print first 500 chars to help diagnose
    logger.debug(f"SAM HTML preview: {html[:500]}")
    return deals


def _parse_cards(cards, source_url: str) -> list[dict]:
    """Parse card/row elements → list of deal dicts"""
    deals: list[dict] = []
    for card in cards:
        text = card.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue

        # ราคา — ลอง element selector ก่อน แล้ว regex
        price = None
        for sel in [".price", "[class*='price']", "[class*='Price']", "strong", "b"]:
            el = card.select_one(sel)
            if el:
                price = _parse_price(el.get_text())
                if price:
                    break
        if not price:
            # ค้นหาราคา ≥ 100,000 บาท ใน text
            for m in re.finditer(r"([\d,]+(?:\.\d+)?)\s*(?:บาท|฿|bath)", text, re.I):
                p = _parse_price(m.group(1))
                if p and p >= 100_000:
                    price = p
                    break
        if not price:
            # ตัวเลข 6+ หลัก (≥ 100,000)
            for m in re.finditer(r"\b([\d,]{6,})\b", text):
                p = _parse_price(m.group(1))
                if p and p >= 100_000:
                    price = p
                    break
        if not price:
            continue

        # Link
        link = card.find("a", href=True)
        if link:
            href = link["href"]
            detail_url = urljoin(BASE_URL, href) if not href.startswith("http") else href
        else:
            detail_url = source_url

        # Skip links that are clearly not detail pages (navigation, filters)
        if any(kw in detail_url for kw in ["page=", "province=", "search=", "#"]):
            if source_url not in detail_url:
                # might be a filter link, use source as fallback
                detail_url = source_url

        # ประเภท
        type_el = card.select_one(
            "[class*='type'],[class*='category'],[class*='Type'],[class*='Category']"
        )
        type_txt = type_el.get_text() if type_el else text
        ptype = _map_type(type_txt)

        # ทำเล
        loc_el = card.select_one(
            "[class*='location'],[class*='province'],[class*='area'],"
            "[class*='Location'],[class*='Province'],[class*='address']"
        )
        location = loc_el.get_text(strip=True) if loc_el else _extract_location(text)

        # พื้นที่
        area = None
        area_el = card.select_one("[class*='area'],[class*='size'],[class*='sqm']")
        if area_el:
            area = _parse_area(area_el.get_text())
        if not area:
            m_area = re.search(r"([\d,]+\.?\d*)\s*ตร\.?ม", text)
            if m_area:
                area = _parse_area(m_area.group(0))

        deal: dict = {
            "listing_url":   detail_url,
            "source_domain": "sam.or.th",
            "source_type":   "bank_npa",
            "property_type": ptype,
            "price":         price,
            "condition":     "fair",
            "location":      location or None,
        }
        if area and area > 0:
            if ptype == "land":
                deal["land_area_sqm"] = area
            else:
                deal["area_sqm"] = area
        deals.append(deal)
    return deals


def _parse_tables(soup: BeautifulSoup, source_url: str) -> list[dict]:
    """Parse HTML tables — ดึงข้อมูลจาก table ที่มีราคาทรัพย์"""
    deals: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # ตรวจ header row
        header_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True) for c in header_cells]
        header_text = " ".join(headers)

        # ต้องมี keyword ทรัพย์
        has_property_kw = any(kw in header_text for kw in
                              ["ทรัพย์", "ราคา", "คดี", "ประเภท", "จังหวัด",
                               "ขาย", "NPA", "npa", "asset", "property"])
        if not has_property_kw:
            # ลอง detect จาก data rows
            row_texts = " ".join(r.get_text() for r in rows[1:4])
            has_property_kw = bool(re.search(r"[\d,]{6,}", row_texts))

        if not has_property_kw:
            continue

        # Map headers → column indexes
        col: dict[str, int] = {}
        for i, h in enumerate(headers):
            h_lower = h.lower()
            if not col.get("price") and any(k in h for k in ["ราคา", "มูลค่า", "price"]):
                col["price"] = i
            if not col.get("type") and any(k in h for k in ["ประเภท", "ทรัพย์", "type"]):
                col["type"] = i
            if not col.get("province") and any(k in h for k in ["จังหวัด", "province"]):
                col["province"] = i
            if not col.get("district") and any(k in h for k in ["อำเภอ", "เขต", "แขวง", "district"]):
                col["district"] = i
            if not col.get("area") and any(k in h for k in ["ขนาด", "พื้นที่", "เนื้อที่", "area", "ตร.ม"]):
                col["area"] = i

        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells:
                continue

            full_text = row.get_text(" ", strip=True)

            # ราคา
            if "price" in col and col["price"] < len(cells):
                price = _parse_price(cells[col["price"]].get_text())
            else:
                price = None
                for cell in cells:
                    p = _parse_price(re.sub(r"[^\d]", "", cell.get_text()))
                    if p and p >= 100_000:
                        price = p
                        break
            if not price:
                continue

            # Link
            link_el = row.find("a", href=True)
            if link_el:
                href = link_el["href"]
                detail_url = urljoin(BASE_URL, href) if not href.startswith("http") else href
            else:
                detail_url = source_url

            # ประเภท
            type_txt = (cells[col["type"]].get_text()
                        if "type" in col and col["type"] < len(cells)
                        else full_text)
            ptype = _map_type(type_txt)

            # Location
            parts = []
            if "district" in col and col["district"] < len(cells):
                parts.append(cells[col["district"]].get_text(strip=True))
            if "province" in col and col["province"] < len(cells):
                parts.append(cells[col["province"]].get_text(strip=True))
            location = " ".join(filter(None, parts)) or _extract_location(full_text)

            # Area
            area = None
            if "area" in col and col["area"] < len(cells):
                area = _parse_area(cells[col["area"]].get_text())
            if not area:
                area = _parse_area(full_text)

            deal: dict = {
                "listing_url":   detail_url,
                "source_domain": "sam.or.th",
                "source_type":   "bank_npa",
                "property_type": ptype,
                "price":         price,
                "condition":     "fair",
                "location":      location or None,
            }
            if area and area > 0:
                if ptype == "land":
                    deal["land_area_sqm"] = area
                else:
                    deal["area_sqm"] = area
            deals.append(deal)

        if deals:
            break  # พบ table ที่ดีแล้ว

    return deals


def _regex_parse(html: str, source_url: str) -> list[dict]:
    """
    Last-resort: ค้นหา pattern ราคา + link ทั่วทั้ง HTML
    ใช้เมื่อ HTML structure ไม่ตรงกับ selectors ใดเลย
    """
    deals: list[dict] = []
    soup = BeautifulSoup(html, "html.parser")

    # ค้นหาทุก <a> ที่ชี้ไปยัง detail page
    detail_links: list[tuple[str, str]] = []  # (url, surrounding_text)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_url = urljoin(BASE_URL, href)
        # Filter: ต้องเป็น same domain + มี path ที่น่าจะเป็น detail page
        if "sam.or.th" in abs_url and any(kw in abs_url for kw in
                                            ["detail", "view", "product", "npa_id", "id=", ".php?", "page_detail"]):
            # ดึง text รอบๆ link (parent element)
            parent = a.parent or a
            surrounding = parent.get_text(" ", strip=True)[:500]
            detail_links.append((abs_url, surrounding))

    for url, text in detail_links:
        # ค้นหาราคาใน surrounding text
        price = None
        for m in re.finditer(r"([\d,]{6,}(?:\.\d+)?)", text):
            p = _parse_price(m.group(1))
            if p and p >= 100_000:
                price = p
                break
        if not price:
            continue

        ptype = _map_type(text)
        location = _extract_location(text)
        area = _parse_area(text)

        deals.append({
            "listing_url":   url,
            "source_domain": "sam.or.th",
            "source_type":   "bank_npa",
            "property_type": ptype,
            "price":         price,
            "condition":     "fair",
            "location":      location or None,
        })

    return deals


def _find_next_page(soup: BeautifulSoup, current_url: str, current_page: int) -> Optional[str]:
    """หา URL หน้าถัดไป"""
    # ลอง rel="next"
    nxt = soup.find("a", rel="next")
    if nxt and nxt.get("href"):
        return urljoin(BASE_URL, nxt["href"])

    # ลอง page N+1 link
    nxt2 = soup.find("a", string=str(current_page + 1))
    if nxt2 and nxt2.get("href"):
        return urljoin(BASE_URL, nxt2["href"])

    # ลอง "ถัดไป" / "Next"
    nxt3 = soup.find("a", string=re.compile(r"ถัดไป|next|>", re.I))
    if nxt3 and nxt3.get("href"):
        return urljoin(BASE_URL, nxt3["href"])

    # ลอง pagination link ที่มีเลขหน้า
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"[?&]page=(\d+)", href)
        if m and int(m.group(1)) == current_page + 1:
            return urljoin(BASE_URL, href)

    # Try appending page param to current URL
    if "page=" in current_url:
        return re.sub(r"page=\d+", f"page={current_page + 1}", current_url)
    else:
        sep = "&" if "?" in current_url else "?"
        return f"{current_url}{sep}page={current_page + 1}"


class SAMHarvester:
    """
    บสก. (SAM) HTML Scraper
    ดึงจาก sam.or.th/site/npa/page_list.php ด้วย httpx + BeautifulSoup
    Universal parser: รองรับ card/table/regex ทุกรูปแบบ HTML
    """

    def __init__(self, delay: float = 2.0):
        self.delay = delay

    async def fetch_all(
        self, max_pages: int = 999_999, max_listings: int = 999_999
    ) -> list[dict]:
        results: list[dict] = []
        seen_urls: set[str] = set()

        params = {
            "s_product_type": "",
            "s_province":     "",
            "s_district":     "",
            "s_status_id":    "",
            "key_search":     "",
        }

        async with httpx.AsyncClient(
            headers=HEADERS, timeout=30, follow_redirects=True
        ) as client:
            page = 1
            current_url = LIST_URL
            consecutive_empty = 0

            while page <= max_pages and len(results) < max_listings:
                try:
                    if page == 1:
                        resp = await client.get(LIST_URL, params=params)
                    else:
                        resp = await client.get(current_url)

                    if resp.status_code != 200:
                        logger.warning(f"SAM page {page}: HTTP {resp.status_code}")
                        break

                    items = _parse_page(resp.text, str(resp.url))

                    # Dedup by listing_url
                    new_items = [x for x in items
                                 if x.get("listing_url") and x["listing_url"] not in seen_urls]
                    for x in new_items:
                        seen_urls.add(x["listing_url"])

                    if not new_items:
                        consecutive_empty += 1
                        logger.info(f"SAM page {page}: ไม่มีผลใหม่ (empty={consecutive_empty})")
                        if consecutive_empty >= 3:
                            logger.info("SAM: หยุดเพราะ 3 หน้าติดต่อกันไม่มีข้อมูล")
                            break
                        # ยังลองหน้าถัดไปก่อน
                    else:
                        consecutive_empty = 0
                        results.extend(new_items)
                        logger.info(f"SAM page {page}: +{len(new_items)} (total {len(results)})")

                    # หน้าถัดไป
                    soup = BeautifulSoup(resp.text, "html.parser")
                    next_url = _find_next_page(soup, current_url, page)

                    if not next_url or next_url == current_url:
                        logger.info("SAM: ไม่มีหน้าถัดไป")
                        break

                    current_url = next_url
                    page += 1
                    await asyncio.sleep(self.delay)

                except Exception as e:
                    logger.error(f"SAM page {page} error: {e}")
                    break

        logger.info(f"SAM: harvested {len(results)} total")
        return results[:max_listings]
