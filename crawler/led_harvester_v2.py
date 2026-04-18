"""
led_harvester_v2.py — กรมบังคับคดี NPA Scraper (REWRITTEN v3)
URL: https://asset.led.go.th/newbidreg/default.asp

สิ่งที่ค้นพบจากการ reverse-engineer เว็บโดยตรง:
  1. CAPTCHA ไม่ใช่ image CAPTCHA — คำตอบเก็บใน hidden input[name=oseckey]
     แค่ copy ค่านั้นไปใส่ input[name=seckey] → ผ่านทันที (ไม่ต้อง OCR หรือ 2captcha!)
  2. Pagination: submit form webFormX (X = page number) ไม่ต้อง CAPTCHA ใหม่
  3. หน่วยงาน: select[name=region_name] ไม่ใช่ text field
  4. Result table: 11 col (lot, seq, case_no, type, ไร่, งาน, ตร.วา, ราคา, ตำบล, อำเภอ, จังหวัด)
  5. หน่วยงานแพ่งกรุงเทพ 1 มี 2,630+ รายการ / 88 หน้า
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from loguru import logger

LED_URL = "https://asset.led.go.th/newbidreg/default.asp"

# หน่วยงานที่ต้องการ scrape — กรุงเทพมหานครและปริมณฑล
TARGET_REGIONS = [
    "แพ่งกรุงเทพมหานคร 1",
    "แพ่งกรุงเทพมหานคร 2",
    "แพ่งกรุงเทพมหานคร 3",
    "แพ่งกรุงเทพมหานคร 4",
    "แพ่งกรุงเทพมหานคร 5",
    "แพ่งกรุงเทพมหานคร 6",
    "แพ่งกรุงเทพมหานคร 7",
    "นนทบุรี",
    "ปทุมธานี",
    "ปทุมธานี  สาขาธัญบุรี",
    "สมุทรปราการ",
]

TYPE_MAP = {
    "ห้องชุด": "condo",
    "คอนโดมิเนียม": "condo",
    "กรรมสิทธิ์ห้องชุด": "condo",
    "ที่ดินพร้อมสิ่งปลูกสร้าง": "house",
    "บ้านเดี่ยว": "house",
    "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse",
    "ทาวน์เฮาส์": "townhouse",
    "ทาวน์โฮม": "townhouse",
    "ที่ดินว่างเปล่า": "land",
    "อาคารพาณิชย์": "commercial",
    "อาคาร": "commercial",
    "ตึกแถว": "commercial",
    "โรงงาน": "commercial",
}


def _parse_price(txt: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", (txt or "").replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except Exception:
        return None


def _parse_area_sqm(rai: str, ngan: str, sqwa: str) -> float:
    """แปลง ไร่/งาน/ตร.วา เป็น ตร.ม. (1 ไร่=1600, 1 งาน=400, 1 ตร.วา=4 ตร.ม.)"""
    try:
        r = float(re.sub(r"[^\d.]", "", rai or "0") or 0)
        n = float(re.sub(r"[^\d.]", "", ngan or "0") or 0)
        w = float(re.sub(r"[^\d.]", "", (sqwa or "0").replace(",", "")) or 0)
        return (r * 1600) + (n * 400) + (w * 4)
    except Exception:
        return 0.0


def _parse_type(txt: str) -> str:
    for kw, pt in TYPE_MAP.items():
        if kw in (txt or ""):
            return pt
    return "other"


class LEDHarvesterV2:
    """
    กรมบังคับคดี Playwright scraper (rewritten v3)
    - อ่าน CAPTCHA จาก hidden field input[name=oseckey] (ไม่ต้อง OCR!)
    - Pagination ด้วย form webFormX โดยตรง ไม่ต้อง CAPTCHA ใหม่
    - scrape 11 หน่วยงานกรุงเทพ+ปริมณฑล
    """

    def __init__(self, delay: float = 1.5, max_captcha_retries: int = 3):
        self.delay = delay
        self.max_captcha_retries = max_captcha_retries

    async def fetch_all(
        self, max_pages: int = 999_999, max_listings: int = 999_999
    ) -> list[dict]:
        results: list[dict] = []
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("LED: Playwright ไม่ได้ติดตั้ง")
            return []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="th-TH",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = await context.new_page()

            for region in TARGET_REGIONS:
                if len(results) >= max_listings:
                    break
                logger.info(f"LED: เริ่ม scrape '{region}'")
                region_results = await self._scrape_region(
                    page, region, max_pages, max_listings - len(results)
                )
                results.extend(region_results)
                logger.info(
                    f"LED: '{region}' → {len(region_results)} deals "
                    f"(รวม {len(results)})"
                )
                await asyncio.sleep(self.delay)

            await browser.close()

        logger.info(f"LED v2: harvested {len(results)} total")
        return results[:max_listings]

    # ──────────────────────────────────────────────────────────────────
    # Per-region scrape loop
    # ──────────────────────────────────────────────────────────────────

    async def _scrape_region(
        self, page, region: str, max_pages: int, remaining: int
    ) -> list[dict]:
        results: list[dict] = []

        # 1. โหลดหน้า search ใหม่ทุก region
        try:
            await page.goto(LED_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(1000)
        except Exception as e:
            logger.error(f"LED: goto failed: {e}")
            return []

        # 2. อ่าน CAPTCHA จาก hidden field — คำตอบอยู่ใน input[name=oseckey]!
        captcha_val = await page.input_value('input[name="oseckey"]')
        if not captcha_val:
            logger.warning(f"LED: ไม่พบ CAPTCHA value สำหรับ '{region}'")
            return []
        logger.debug(f"LED: CAPTCHA = '{captcha_val}'")

        # 3. กรอก CAPTCHA ลงใน visible field input[name=seckey]
        await page.fill('input[name="seckey"]', captcha_val)

        # 4. เลือก region จาก dropdown select[name=region_name]
        try:
            await page.select_option('select[name="region_name"]', label=region)
        except Exception:
            try:
                await page.select_option('select[name="region_name"]', value=region)
            except Exception as e:
                logger.warning(f"LED: เลือก region '{region}' ไม่ได้: {e}")
                return []
        await page.wait_for_timeout(300)

        # 5. Submit — click ปุ่ม ค้นหา
        try:
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(800)
        except Exception as e:
            logger.error(f"LED: submit failed for '{region}': {e}")
            return []

        # 6. ตรวจว่า search สำเร็จ
        page_html = await page.content()
        if "ผลการค้นหา" not in page_html and "หมายเลขคดี" not in page_html:
            logger.warning(f"LED: '{region}' ไม่พบผลลัพธ์")
            return []

        # 7. หา total pages จาก "หน้าที่ X/Y"
        total_pages = await self._get_total_pages(page)
        logger.info(f"LED: '{region}' → {total_pages} หน้า")

        # 8. Parse หน้าแรก
        page_results = await self._parse_current_page(page, region)
        results.extend(page_results)
        logger.info(f"LED: '{region}' หน้า 1/{total_pages} → {len(page_results)} รายการ")

        # 9. Paginate — webFormX ไม่ต้อง CAPTCHA ใหม่
        for page_num in range(2, min(total_pages + 1, max_pages + 1)):
            if len(results) >= remaining:
                break
            ok = await self._go_to_page(page, page_num)
            if not ok:
                logger.warning(f"LED: '{region}' ไปหน้า {page_num} ไม่ได้ — หยุด")
                break
            page_results = await self._parse_current_page(page, region)
            if not page_results:
                logger.info(f"LED: '{region}' หน้า {page_num} ว่าง — จบ")
                break
            results.extend(page_results)
            logger.info(
                f"LED: '{region}' หน้า {page_num}/{total_pages} "
                f"→ {len(page_results)} (รวม {len(results)})"
            )
            await asyncio.sleep(self.delay)

        return results

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    async def _get_total_pages(self, page) -> int:
        """อ่าน total pages จาก 'หน้าที่ X/Y'"""
        try:
            text = await page.inner_text("body")
            match = re.search(r"หน้าที่\s+\d+/(\d+)", text)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return 1

    async def _go_to_page(self, page, page_num: int) -> bool:
        """
        Navigate ไปหน้าที่ต้องการ:
          1. Click link ที่มี onclick="webFormX.submit()"
          2. ถ้าไม่อยู่ใน visible range → click next-group ก่อน
          3. Fallback: submit ผ่าน JS evaluate
        """
        try:
            # Strategy 1: click link ตรง
            link = page.locator(f'a[onclick*="webForm{page_num}.submit"]').first
            if await link.count() > 0:
                await link.click()
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(500)
                return True

            # Strategy 2: click "next group" แล้วลอง click อีกรอบ
            next_group = page.locator('a[onclick*="webFormn"]').first
            if await next_group.count() > 0:
                await next_group.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(500)
                link2 = page.locator(
                    f'a[onclick*="webForm{page_num}.submit"]'
                ).first
                if await link2.count() > 0:
                    await link2.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(500)
                    return True

            # Strategy 3: Fallback JS submit
            result = await page.evaluate(f"""
                (() => {{
                    const f = document.forms['webForm{page_num}'];
                    if (f) {{ f.submit(); return true; }}
                    return false;
                }})()
            """)
            if result:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(500)
                return True

        except Exception as e:
            logger.warning(f"LED: go_to_page({page_num}) error: {e}")
        return False

    async def _parse_current_page(self, page, region: str) -> list[dict]:
        """
        Parse ตาราง result จากหน้าปัจจุบัน
        Table structure (confirmed from live scraping):
          row[0]: headers (ล๊อตที่, ลำดับที่, หมายเลขคดี, ประเภท, ขนาด, ราคา, ตำบล, อำเภอ, จังหวัด)
          row[1]: size sub-header (ไร่, งาน, ตร.วา)
          row[2+]: data — 11 cells each
        """
        results = []
        try:
            tables = await page.query_selector_all("table")
            result_table = None
            for t in tables:
                text = await t.inner_text()
                if "ราคาประเมิน" in text and "หมายเลขคดี" in text:
                    result_table = t
                    break

            if not result_table:
                logger.debug(f"LED: ไม่พบ result table สำหรับ '{region}'")
                return []

            rows = await result_table.query_selector_all("tr")
            for row in rows[2:]:  # skip 2 header rows
                cells = await row.query_selector_all("td")
                if len(cells) < 11:
                    continue

                texts = [((await c.inner_text()) or "").strip() for c in cells]

                # Column mapping (confirmed):
                # 0=ล๊อตที่  1=ลำดับที่  2=หมายเลขคดี  3=ประเภท
                # 4=ไร่  5=งาน  6=ตร.วา  7=ราคาประเมิน
                # 8=ตำบล  9=อำเภอ  10=จังหวัด
                lot       = texts[0]
                seq       = texts[1]
                case_no   = texts[2]
                ptype_raw = texts[3]
                rai       = texts[4]
                ngan      = texts[5]
                sqwa      = texts[6]
                price_raw = texts[7]
                tambon    = texts[8]
                ampur     = texts[9]
                changwat  = texts[10]

                price = _parse_price(price_raw)
                if not price or price <= 0:
                    continue

                area_sqm = _parse_area_sqm(rai, ngan, sqwa)
                ptype    = _parse_type(ptype_raw)
                location = f"{tambon} {ampur} {changwat}".strip()

                # unique source_url จาก case_no + lot
                safe_case = re.sub(r"[^a-zA-Z0-9ก-๙/]", "_", case_no)
                safe_lot  = re.sub(r"[^a-zA-Z0-9/]", "_", lot)
                source_url = (
                    f"https://asset.led.go.th/newbidreg/default.asp"
                    f"?case={safe_case}&lot={safe_lot}"
                )

                results.append({
                    "source_url":    source_url,
                    "source_domain": "asset.led.go.th",
                    "property_type": ptype,
                    "price":         int(price),
                    "area_sqm":      area_sqm if area_sqm > 0 else None,
                    "condition":     "fair",
                    "location":      location,
                    "title":         f"{ptype_raw} {tambon} {ampur}".strip(),
                    "is_benchmark":  False,
                    "raw_data": {
                        "lot":      lot,
                        "seq":      seq,
                        "case_no":  case_no,
                        "tambon":   tambon,
                        "ampur":    ampur,
                        "changwat": changwat,
                        "rai":      rai,
                        "ngan":     ngan,
                        "sqwa":     sqwa,
                        "region":   region,
                    },
                })

        except Exception as e:
            logger.error(f"LED: parse error for '{region}': {e}")
        return results

    async def fetch_urls_only(self, max_pages: int = 20) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
