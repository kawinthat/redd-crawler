"""
led_harvester_v2.py — กรมบังคับคดี NPA Scraper (v4 — robust + full logging)
URL: https://asset.led.go.th/newbidreg/default.asp

สิ่งที่ค้นพบจากการ reverse-engineer เว็บโดยตรง:
  1. CAPTCHA ไม่ใช่ image CAPTCHA — คำตอบเก็บใน hidden input[name=oseckey]
     แค่ copy ค่านั้นไปใส่ input[name=seckey] → ผ่านทันที (ไม่ต้อง OCR หรือ 2captcha!)
  2. Pagination: submit form webFormX (X = page number) ไม่ต้อง CAPTCHA ใหม่
  3. หน่วยงาน: select[name=region_name] ไม่ใช่ text field
  4. Result table: 11 col (lot, seq, case_no, type, ไร่, งาน, ตร.วา, ราคา, ตำบล, อำเภอ, จังหวัด)
  5. หน่วยงานแพ่งกรุงเทพ 1 มี 2,630+ รายการ / 88 หน้า
  6. ASP Classic ใช้ <input type="submit"> ไม่ใช่ <button type="submit">
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
    กรมบังคับคดี Playwright scraper (v4 — robust + full logging)
    - อ่าน CAPTCHA จาก hidden field input[name=oseckey] (ไม่ต้อง OCR!)
    - Fallback: อ่าน CAPTCHA จาก hidden inputs ทั้งหมดหากไม่พบ oseckey
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

        logger.info("LED: เริ่ม launch Playwright Chromium...")
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"],
                )
                logger.info("LED: Chromium launched OK")

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
                    try:
                        region_results = await self._scrape_region(
                            page, region, max_pages, max_listings - len(results)
                        )
                        results.extend(region_results)
                        logger.info(
                            f"LED: '{region}' → {len(region_results)} deals "
                            f"(รวม {len(results)})"
                        )
                    except Exception as region_err:
                        logger.error(f"LED: region '{region}' failed: {region_err}")
                    await asyncio.sleep(self.delay)

                await browser.close()
        except Exception as e:
            logger.error(f"LED: Playwright error: {e}")
            return []

        logger.info(f"LED v4: harvested {len(results)} total")
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
            # รอ JS โหลดเสร็จ (ASP Classic อาจ inject CAPTCHA ด้วย JS)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"LED: goto failed for '{region}': {e}")
            return []

        # 2. Log page info เพื่อ debug
        try:
            page_title = await page.title()
            page_url   = page.url
            logger.info(f"LED: page loaded — title='{page_title}' url={page_url}")
        except Exception:
            pass

        # 3. อ่าน CAPTCHA จาก hidden field
        captcha_val = await self._read_captcha(page, region)
        if captcha_val is None:
            return []

        # 4. กรอก CAPTCHA ลงใน visible field
        try:
            await page.fill('input[name="seckey"]', captcha_val)
            logger.debug(f"LED: filled seckey='{captcha_val}'")
        except Exception as e:
            logger.error(f"LED: ไม่สามารถกรอก seckey: {e}")
            return []

        # 5. เลือก region จาก dropdown — ลอง 3 วิธี
        selected = await self._select_region(page, region)
        if not selected:
            return []
        await page.wait_for_timeout(300)

        # 6. Submit form — ลอง input[type=submit] ก่อน, fallback button[type=submit]
        submitted = await self._submit_form(page, region)
        if not submitted:
            return []

        # 7. ตรวจว่า search สำเร็จ + log HTML snippet
        page_html = await page.content()
        # แสดง 500 chars แรกเพื่อ debug
        logger.debug(f"LED: result page HTML (first 500): {page_html[:500]}")

        success_keywords = ["ผลการค้นหา", "หมายเลขคดี", "ล๊อตที่", "ลำดับที่", "ราคา"]
        found_kw = [kw for kw in success_keywords if kw in page_html]
        if not found_kw:
            logger.warning(
                f"LED: '{region}' ไม่พบผลลัพธ์ — "
                f"page length={len(page_html)}, "
                f"URL={page.url}"
            )
            return []
        logger.info(f"LED: '{region}' search success — found keywords: {found_kw}")

        # 8. หา total pages
        total_pages = await self._get_total_pages(page)
        logger.info(f"LED: '{region}' → {total_pages} หน้า")

        # 9. Parse หน้าแรก
        page_results = await self._parse_current_page(page, region)
        results.extend(page_results)
        logger.info(f"LED: '{region}' หน้า 1/{total_pages} → {len(page_results)} รายการ")

        # 10. Paginate
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

    async def _read_captcha(self, page, region: str) -> Optional[str]:
        """
        อ่าน CAPTCHA value — ลอง selectors หลายอัน
        คำตอบอยู่ใน input ที่ซ่อนอยู่ (type=hidden หรือ name=oseckey)
        """
        # Candidates ที่เป็นไปได้สำหรับ hidden CAPTCHA field
        captcha_selectors = [
            'input[name="oseckey"]',
            'input[name="o_seckey"]',
            'input[name="captcha_answer"]',
            'input[name="answer"]',
        ]
        for sel in captcha_selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    val = await page.locator(sel).first.input_value()
                    if val:
                        logger.info(f"LED: CAPTCHA found via '{sel}' = '{val}'")
                        return val
                    logger.debug(f"LED: '{sel}' exists but value is empty")
            except Exception as e:
                logger.debug(f"LED: selector '{sel}' error: {e}")

        # Fallback: ดู hidden inputs ทั้งหมด (อาจเป็น name อื่น)
        try:
            hidden_inputs = await page.evaluate("""
                () => {
                    const inputs = document.querySelectorAll('input[type="hidden"]');
                    return Array.from(inputs).map(i => ({name: i.name, value: i.value}));
                }
            """)
            logger.info(f"LED: hidden inputs on page: {hidden_inputs}")

            # หา hidden input ที่มี value เป็น string สั้นๆ (น่าจะเป็น CAPTCHA)
            for inp in hidden_inputs:
                v = inp.get("value", "")
                n = inp.get("name", "")
                # CAPTCHA มักเป็น alphanumeric 4-8 ตัว
                if v and 2 <= len(v) <= 20 and n.lower() not in (
                    "page", "pagenum", "region", "region_name", "action",
                    "__viewstate", "__eventvalidation", "__viewstategenerator",
                ):
                    logger.info(f"LED: CAPTCHA fallback — using hidden field name='{n}' value='{v}'")
                    return v

        except Exception as e:
            logger.error(f"LED: ไม่สามารถอ่าน hidden inputs: {e}")

        logger.warning(f"LED: '{region}' — ไม่พบ CAPTCHA value ใน hidden fields ทั้งหมด")
        return None

    async def _select_region(self, page, region: str) -> bool:
        """เลือก region จาก dropdown — ลอง label, value, index"""
        # ลอง label ก่อน
        try:
            await page.select_option('select[name="region_name"]', label=region)
            selected = await page.locator('select[name="region_name"]').input_value()
            logger.debug(f"LED: selected region by label='{region}' → value='{selected}'")
            return True
        except Exception:
            pass

        # ลอง value
        try:
            await page.select_option('select[name="region_name"]', value=region)
            return True
        except Exception:
            pass

        # ลอง label แบบ fuzzy (ตัดช่องว่างออก)
        region_stripped = " ".join(region.split())
        try:
            options = await page.evaluate("""
                () => {
                    const sel = document.querySelector('select[name="region_name"]');
                    if (!sel) return [];
                    return Array.from(sel.options).map(o => ({text: o.text.trim(), value: o.value}));
                }
            """)
            logger.info(f"LED: dropdown options (first 15): {options[:15]}")
            # หา option ที่ match แบบ fuzzy
            for opt in options:
                opt_text = " ".join((opt.get("text") or "").split())
                if opt_text == region_stripped or region_stripped in opt_text:
                    await page.select_option(
                        'select[name="region_name"]', value=opt["value"]
                    )
                    logger.info(f"LED: fuzzy-matched region '{region}' → '{opt['text']}'")
                    return True
        except Exception as e:
            logger.error(f"LED: เลือก region '{region}' ไม่ได้ทุกวิธี: {e}")

        logger.warning(f"LED: ไม่สามารถเลือก region '{region}'")
        return False

    async def _submit_form(self, page, region: str) -> bool:
        """Submit ฟอร์ม — ลอง input[type=submit] (ASP Classic) ก่อน"""
        submit_selectors = [
            'input[type="submit"]',
            'button[type="submit"]',
            'input[value="ค้นหา"]',
            'button:has-text("ค้นหา")',
            'input[type="button"][onclick*="submit"]',
        ]
        for sel in submit_selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    await page.locator(sel).first.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=25_000)
                    await page.wait_for_timeout(1000)
                    logger.info(f"LED: submitted via '{sel}' for region '{region}'")
                    return True
            except Exception as e:
                logger.debug(f"LED: submit selector '{sel}' failed: {e}")

        # Fallback: JS form submit
        try:
            await page.evaluate("document.forms[0].submit()")
            await page.wait_for_load_state("domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1000)
            logger.info(f"LED: submitted via JS forms[0].submit() for '{region}'")
            return True
        except Exception as e:
            logger.error(f"LED: ไม่สามารถ submit form สำหรับ '{region}': {e}")
            return False

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
            result_table_debug = []

            for i, t in enumerate(tables):
                text = await t.inner_text()
                result_table_debug.append(
                    f"table[{i}] keywords: "
                    + str([kw for kw in ["ราคาประเมิน","ราคาขั้นต้น","ราคาประมาณการ","หมายเลขคดี","ล๊อตที่"] if kw in text])
                )
                if "หมายเลขคดี" in text and any(
                    kw in text for kw in ["ราคาประเมิน", "ราคาขั้นต้น", "ราคาประมาณการ", "ราคา"]
                ):
                    result_table = t
                    break

            if not result_table:
                # ลอง fallback: ตาราง 2 ขึ้นไปที่มีข้อมูลมาก
                logger.debug(f"LED: table scan for '{region}': {result_table_debug}")
                for i, t in enumerate(tables):
                    rows = await t.query_selector_all("tr")
                    if len(rows) >= 5:
                        result_table = t
                        logger.info(f"LED: using table[{i}] with {len(rows)} rows (fallback)")
                        break

            if not result_table:
                logger.debug(f"LED: ไม่พบ result table สำหรับ '{region}'")
                return []

            rows = await result_table.query_selector_all("tr")
            logger.debug(f"LED: '{region}' table has {len(rows)} rows")

            # หา data rows — skip header rows (ที่ไม่ใช่ข้อมูล)
            skip_count = 0
            for row in rows[:5]:
                cells = await row.query_selector_all("td,th")
                texts = [((await c.inner_text()) or "").strip() for c in cells]
                joined = " ".join(texts)
                if any(kw in joined for kw in ["ล๊อตที่", "หมายเลขคดี", "ประเภท", "ไร่", "งาน"]):
                    skip_count += 1
                else:
                    break

            data_rows = rows[skip_count:] if skip_count > 0 else rows[2:]
            logger.debug(f"LED: '{region}' skipping {skip_count} header rows, data rows={len(data_rows)}")

            for row in data_rows:
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
