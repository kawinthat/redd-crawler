"""
led_harvester_v2.py — กรมบังคับคดี NPA Scraper
URL: https://asset.led.go.th/newbidreg/default.asp

ใช้ Playwright เพราะ:
  - เป็น ASP Classic form (ต้องการ session/ViewState)
  - มี CAPTCHA ก่อน submit

CAPTCHA strategy (ลำดับ):
  1. pytesseract OCR (ฟรี, ต้องติดตั้ง tesseract-ocr บน server)
  2. 2captcha API (ถ้ามี TWOCAPTCHA_KEY ใน .env)
  3. ลอง submit ว่างๆ (บางทีไม่มี CAPTCHA จริง)

จังหวัดที่ crawl: กรุงเทพมหานคร, นนทบุรี, ปทุมธานี

SETUP บน Render:
  เพิ่มใน Build Command:
    apt-get install -y tesseract-ocr tesseract-ocr-tha
  เพิ่มใน requirements.txt:
    pytesseract
    Pillow
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import re
from typing import Optional

from loguru import logger

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

LED_URL = "https://asset.led.go.th/newbidreg/default.asp"

# จังหวัดที่ต้องการ crawl (ต้องตรงกับ text ใน <option> ของเว็บ)
TARGET_PROVINCES = [
    "กรุงเทพมหานคร",
    "นนทบุรี",
    "ปทุมธานี",
]

# ถ้ามี TWOCAPTCHA_KEY ใน .env → ใช้ 2captcha solve (แม่นกว่า OCR)
TWOCAPTCHA_KEY = os.getenv("TWOCAPTCHA_KEY", "")

# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────

TYPE_MAP = {
    "บ้านเดี่ยว": "house",  "บ้าน": "house",
    "ทาวน์เฮ้าส์": "townhouse", "ทาวน์เฮาส์": "townhouse",
    "ทาวน์โฮม": "townhouse",   "ทาวน์": "townhouse",
    "คอนโดมิเนียม": "condo",   "คอนโด": "condo",
    "ห้องชุด": "condo",
    "ที่ดิน": "land",           "ที่ดินเปล่า": "land",
    "อาคารพาณิชย์": "commercial", "อาคาร": "commercial",
    "ตึกแถว": "commercial",    "โรงงาน": "commercial",
    "สำนักงาน": "commercial",
}


def _parse_type(txt: str) -> str:
    for kw, pt in TYPE_MAP.items():
        if kw in (txt or ""):
            return pt
    return "other"


def _parse_price(txt: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", (txt or "").replace(",", ""))
    try:
        return float(cleaned) if cleaned else None
    except Exception:
        return None


# ─────────────────────────────────────────────
# CAPTCHA SOLVERS
# ─────────────────────────────────────────────

async def _ocr_captcha(img_bytes: bytes) -> str:
    """ลอง pytesseract OCR — ต้องติดตั้ง tesseract-ocr ก่อน."""
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter
        img = Image.open(io.BytesIO(img_bytes)).convert("L")  # grayscale
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = img.filter(ImageFilter.SHARPEN)
        # whitelist ตัวเลข+อักษรภาษาอังกฤษ (LED มักใช้ alphanumeric)
        text = pytesseract.image_to_string(
            img,
            config=(
                "--psm 7 "
                "-c tessedit_char_whitelist="
                "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            )
        ).strip().replace(" ", "")
        logger.debug(f"OCR result: '{text}'")
        return text
    except ImportError:
        logger.debug("pytesseract ไม่ได้ติดตั้ง → ลอง 2captcha")
        return ""
    except Exception as e:
        logger.warning(f"OCR error: {e}")
        return ""


async def _solve_2captcha(img_bytes: bytes) -> str:
    """ส่ง captcha image ไปให้ 2captcha solve."""
    if not TWOCAPTCHA_KEY:
        return ""
    try:
        import httpx
        b64 = base64.b64encode(img_bytes).decode()
        async with httpx.AsyncClient(timeout=60) as client:
            # 1. Submit image
            resp = await client.post(
                "http://2captcha.com/in.php",
                data={"key": TWOCAPTCHA_KEY, "method": "base64", "body": b64},
            )
            if "OK|" not in resp.text:
                logger.warning(f"2captcha submit error: {resp.text[:100]}")
                return ""
            captcha_id = resp.text.split("|")[1]
            logger.debug(f"2captcha submitted: id={captcha_id}")
            # 2. Poll result (รอ max 45 วิ)
            for _ in range(15):
                await asyncio.sleep(3)
                res = await client.get(
                    "http://2captcha.com/res.php",
                    params={"key": TWOCAPTCHA_KEY, "action": "get", "id": captcha_id},
                )
                if "OK|" in res.text:
                    answer = res.text.split("|")[1]
                    logger.debug(f"2captcha answer: '{answer}'")
                    return answer
                if "CAPCHA_NOT_READY" not in res.text:
                    logger.warning(f"2captcha unexpected: {res.text[:100]}")
                    break
    except Exception as e:
        logger.error(f"2captcha error: {e}")
    return ""


# ─────────────────────────────────────────────
# MAIN HARVESTER
# ─────────────────────────────────────────────

class LEDHarvesterV2:
    """
    กรมบังคับคดี Playwright scraper
    - เลือกจังหวัดอัตโนมัติ (กรุงเทพ / นนทบุรี / ปทุมธานี)
    - แก้ CAPTCHA ด้วย OCR หรือ 2captcha
    - paginate จนครบทุกหน้า
    """

    def __init__(self, delay: float = 2.0, max_captcha_retries: int = 5):
        self.delay = delay
        self.max_captcha_retries = max_captcha_retries

    async def fetch_all(
        self, max_pages: int = 999_999, max_listings: int = 999_999
    ) -> list[dict]:
        results: list[dict] = []
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("Playwright ไม่ได้ติดตั้ง")
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
            # ซ่อน automation flag
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = await context.new_page()

            for province in TARGET_PROVINCES:
                if len(results) >= max_listings:
                    break
                logger.info(f"LED: เริ่ม scrape จังหวัด {province}")
                prov_results = await self._scrape_province(
                    page, province, max_pages, max_listings - len(results)
                )
                results.extend(prov_results)
                logger.info(
                    f"LED: {province} → {len(prov_results)} deals "
                    f"(รวม {len(results)})"
                )
                await asyncio.sleep(self.delay)

            await browser.close()

        logger.info(f"LED v2: harvested {len(results)} total")
        return results[:max_listings]

    # ──────────────────────────────────────────
    # Per-province scrape loop
    # ──────────────────────────────────────────

    async def _scrape_province(
        self, page, province: str, max_pages: int, remaining: int
    ) -> list[dict]:
        results: list[dict] = []
        page_num = 1

        while page_num <= max_pages and len(results) < remaining:
            # โหลดหน้าใหม่ (reset ViewState/session ทุก loop)
            try:
                await page.goto(LED_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1500)
            except Exception as e:
                logger.error(f"LED: goto failed: {e}")
                break

            # 1. เลือกจังหวัด
            if not await self._select_province(page, province):
                logger.warning(f"LED: ไม่พบจังหวัด '{province}' ใน dropdown — ข้าม")
                break

            # 2. กรอก CAPTCHA
            captcha_ok = await self._handle_captcha(page)
            if not captcha_ok:
                logger.warning(f"LED: CAPTCHA แก้ไม่ได้ — ข้าม {province} หน้า {page_num}")
                break

            # 3. ถ้าไม่ใช่หน้าแรก → navigate ไปหน้าที่ต้องการ
            if page_num > 1:
                jumped = await self._jump_to_page(page, page_num)
                if not jumped:
                    logger.info(f"LED: {province} ไม่มีหน้า {page_num} — จบ")
                    break

            # 4. Submit (หน้าแรกเท่านั้น)
            if page_num == 1:
                submitted = await self._submit_form(page)
                if not submitted:
                    break

            # 5. Parse ผลลัพธ์
            html = await page.content()
            items = self._parse_table(html, province)
            if not items:
                logger.info(f"LED: {province} หน้า {page_num} ว่าง — จบ")
                break

            results.extend(items)
            logger.info(f"LED: {province} หน้า {page_num}: +{len(items)}")

            # 6. คลิกหน้าถัดไป (ถ้าอยู่ใน result page)
            has_next = await self._click_next(page)
            if not has_next:
                logger.info(f"LED: {province} ไม่มีหน้าถัดไป — จบ")
                break

            page_num += 1
            await asyncio.sleep(self.delay)

        return results

    # ──────────────────────────────────────────
    # Province selector
    # ──────────────────────────────────────────

    async def _select_province(self, page, province: str) -> bool:
        """หา <select> จังหวัด แล้วเลือก province ที่ต้องการ"""
        # selector candidates — เรียงตามความน่าจะเป็น
        selectors = [
            "select[name*='province' i]",
            "select[name*='changwat' i]",
            "select[id*='province' i]",
            "select[id*='ddlProvince' i]",
            "select[id*='changwat' i]",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.count() == 0:
                    continue
                options = await el.locator("option").all_text_contents()
                # หา option ที่ match
                matched = next(
                    (opt for opt in options
                     if province in opt or province[:4] in opt),
                    None
                )
                if matched:
                    await el.select_option(label=matched)
                    await page.wait_for_timeout(600)
                    logger.debug(f"LED: เลือก '{matched}' (selector={sel})")
                    return True
            except Exception:
                continue

        # fallback: ลอง select ทุกตัวในหน้า
        try:
            all_selects = page.locator("select")
            count = await all_selects.count()
            for i in range(count):
                el = all_selects.nth(i)
                options = await el.locator("option").all_text_contents()
                matched = next(
                    (opt for opt in options
                     if province in opt or province[:4] in opt),
                    None
                )
                if matched:
                    await el.select_option(label=matched)
                    await page.wait_for_timeout(600)
                    logger.debug(f"LED: เลือก '{matched}' (select #{i})")
                    return True
        except Exception:
            pass

        return False

    # ──────────────────────────────────────────
    # CAPTCHA handler
    # ──────────────────────────────────────────

    async def _handle_captcha(self, page) -> bool:
        """
        1. หา CAPTCHA image
        2. Solve ด้วย OCR / 2captcha
        3. กรอก input
        คืน True ถ้าสำเร็จหรือไม่มี CAPTCHA

        Fallback: ถ้าทุกวิธีล้มเหลว → ลอง submit โดยไม่กรอก CAPTCHA
        (บางหน้าของ LED ไม่บังคับ CAPTCHA จริง)
        """
        CAPTCHA_IMG_SELECTORS = [
            "img[src*='captcha' i]",
            "img[src*='vcode' i]",
            "img[src*='verify' i]",
            "img[src*='code' i]",
            "img[id*='captcha' i]",
            "#imgCaptcha",
            ".captcha img",
            "img[alt*='captcha' i]",
            "img[alt*='ยืนยัน']",
        ]
        CAPTCHA_INPUT_SELECTORS = [
            "input[name*='captcha' i]",
            "input[id*='captcha' i]",
            "input[name*='vcode' i]",
            "input[name*='code' i]",
            "input[id*='code' i]",
            "input[placeholder*='ยืนยัน']",
            "input[placeholder*='กรอก']",
            "input[maxlength='6']",
            "input[maxlength='5']",
            "input[maxlength='4']",
        ]

        # ── ตรวจว่ามี CAPTCHA จริงไหม ──────────────────────────────────
        has_captcha = False
        for sel in CAPTCHA_IMG_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    has_captcha = True
                    break
            except Exception:
                continue

        if not has_captcha:
            logger.info("LED: ไม่พบ CAPTCHA image — ดำเนินการต่อ")
            return True

        for attempt in range(self.max_captcha_retries):
            # ── ค้นหา CAPTCHA image ─────────────────────────────
            img_bytes: Optional[bytes] = None
            for sel in CAPTCHA_IMG_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        img_bytes = await el.screenshot()
                        logger.debug(f"LED: CAPTCHA image พบที่ {sel}")
                        break
                except Exception:
                    continue

            if img_bytes is None:
                logger.info("LED: CAPTCHA image หายไประหว่าง attempt — ดำเนินการต่อ")
                return True

            # ── Solve ───────────────────────────────────────────
            answer = await _ocr_captcha(img_bytes)
            if not answer and TWOCAPTCHA_KEY:
                answer = await _solve_2captcha(img_bytes)

            if not answer:
                logger.warning(
                    f"LED: Solve CAPTCHA ล้มเหลว attempt {attempt + 1}/{self.max_captcha_retries}"
                )
                await self._refresh_captcha(page)
                await asyncio.sleep(1)
                continue

            # ── กรอก input ──────────────────────────────────────
            filled = False
            for inp_sel in CAPTCHA_INPUT_SELECTORS:
                try:
                    inp = page.locator(inp_sel).first
                    if await inp.count() > 0 and await inp.is_visible():
                        await inp.fill("")
                        await inp.type(answer, delay=80)  # พิมพ์ทีละตัวให้เป็นธรรมชาติ
                        filled = True
                        logger.debug(f"LED: กรอก CAPTCHA '{answer}' → {inp_sel}")
                        break
                except Exception:
                    continue

            if filled:
                return True

            logger.warning(f"LED: หา CAPTCHA input ไม่เจอ attempt {attempt + 1}")

        # ── Last resort: ลอง submit โดยไม่กรอก CAPTCHA ─────────────────
        logger.warning("LED: ลอง submit โดยไม่กรอก CAPTCHA (last resort)")
        return True  # ให้ _submit_form() ลองต่อ — ถ้าแก้ CAPTCHA ไม่ได้ ให้ continue ไม่ให้ crash

    async def _refresh_captcha(self, page) -> None:
        """คลิกปุ่ม/ลิ้งค์ refresh CAPTCHA"""
        refresh_selectors = [
            "a[href*='captcha' i]", "a[id*='refresh' i]",
            "img[title*='refresh' i]", "img[onclick*='captcha' i]",
            "span[onclick*='captcha' i]", "a[onclick*='captcha' i]",
        ]
        for sel in refresh_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_timeout(800)
                    return
            except Exception:
                continue
        # fallback: reload หน้า
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

    # ──────────────────────────────────────────
    # Form submit
    # ──────────────────────────────────────────

    async def _submit_form(self, page) -> bool:
        """กดปุ่มค้นหา / submit"""
        submit_selectors = [
            "input[type='submit'][value*='ค้นหา']",
            "input[type='submit'][value*='Search']",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('ค้นหา')",
            "a:has-text('ค้นหา')",
        ]
        for sel in submit_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=25_000)
                    await page.wait_for_timeout(1500)
                    logger.debug(f"LED: submit สำเร็จ ({sel})")
                    return True
            except Exception:
                continue
        logger.error("LED: ไม่พบปุ่ม submit")
        return False

    async def _jump_to_page(self, page, page_num: int) -> bool:
        """ถ้าอยู่ใน result page แล้ว → คลิกหมายเลขหน้า"""
        try:
            el = page.locator(f"a:has-text('{page_num}')").first
            if await el.count() > 0:
                await el.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            pass
        return False

    async def _click_next(self, page) -> bool:
        """คลิกปุ่มหน้าถัดไป — คืน True ถ้าเจอ"""
        next_selectors = [
            "a:has-text('ถัดไป')",
            "a:has-text('หน้าถัดไป')",
            "a:has-text('Next')",
            "a:has-text('>')",
            "[aria-label='Next page']",
            ".pagination-next a",
            "a[rel='next']",
        ]
        for sel in next_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click()
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                    await page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False

    # ──────────────────────────────────────────
    # HTML table parser
    # ──────────────────────────────────────────

    def _parse_table(self, html: str, province: str) -> list[dict]:
        """
        Parse HTML table ผลลัพธ์จาก กรมบังคับคดี
        รองรับ table หลายรูปแบบ (ปรับอัตโนมัติจาก header)
        """
        results: list[dict] = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if len(rows) < 2:
                    continue

                # หา header row
                header_cells = rows[0].find_all(["th", "td"])
                headers = [c.get_text(strip=True) for c in header_cells]

                # ตรวจว่า table นี้มี column ที่เกี่ยวกับทรัพย์
                header_text = " ".join(headers)
                if not any(kw in header_text for kw in
                           ["ทรัพย์", "ราคา", "คดี", "ประเภท", "จังหวัด"]):
                    continue

                # Map header → column index
                col: dict[str, int] = {}
                for i, h in enumerate(headers):
                    if any(k in h for k in ["ราคา", "มูลค่า"]):
                        col.setdefault("price", i)
                    if any(k in h for k in ["ประเภท", "ทรัพย์"]):
                        col.setdefault("type", i)
                    if any(k in h for k in ["จังหวัด"]):
                        col.setdefault("province", i)
                    if any(k in h for k in ["อำเภอ", "เขต", "แขวง"]):
                        col.setdefault("district", i)
                    if any(k in h for k in ["คดี", "เลขที่", "ที่"]):
                        col.setdefault("case_no", i)
                    if any(k in h for k in ["วันที่", "นัด", "ขาย"]):
                        col.setdefault("date", i)

                # Parse data rows
                for row in rows[1:]:
                    cells = row.find_all("td")
                    if not cells:
                        continue

                    full_text = row.get_text(" ", strip=True)

                    # ราคา
                    price_txt = cells[col["price"]].get_text() if "price" in col else ""
                    price = _parse_price(price_txt)
                    if not price or price <= 0:
                        # fallback: หาราคาจากทุก cell
                        for cell in cells:
                            ct = cell.get_text()
                            if "บาท" in ct or re.search(r"\d{5,}", ct):
                                price = _parse_price(re.sub(r"[^\d.]", "", ct.replace(",", "")))
                                if price and price > 0:
                                    break
                    if not price or price <= 0:
                        continue

                    # ประเภททรัพย์
                    type_txt = (cells[col["type"]].get_text()
                                if "type" in col else full_text)

                    # อำเภอ/เขต
                    district = (cells[col["district"]].get_text(strip=True)
                                if "district" in col else "")

                    # เลขคดี
                    case_no = (cells[col["case_no"]].get_text(strip=True)
                               if "case_no" in col else "")

                    # วันประมูล
                    auction_date = (cells[col["date"]].get_text(strip=True)
                                    if "date" in col else "")

                    # Detail link
                    link_el = row.find("a", href=True)
                    if link_el:
                        href = link_el["href"]
                        detail_url = (href if href.startswith("http")
                                      else f"https://asset.led.go.th/newbidreg/{href.lstrip('/')}")
                    else:
                        detail_url = (
                            f"https://asset.led.go.th/newbidreg/default.asp"
                            f"?case={case_no}" if case_no else LED_URL
                        )

                    results.append({
                        "listing_url":   detail_url,
                        "source_domain": "asset.led.go.th",
                        "source_type":   "enforcement",
                        "property_type": _parse_type(type_txt),
                        "price":         int(price),
                        "condition":     "fair",
                        "location":      f"{district} {province}".strip(),
                        "project_name":  full_text[:100],
                        "auction_date":  auction_date or None,
                        "raw_data": {
                            "case_no":      case_no,
                            "province":     province,
                            "district":     district,
                            "auction_date": auction_date,
                        },
                    })

                if results:
                    break  # พบ table ที่ถูกต้องแล้ว

        except ImportError:
            logger.warning("LED: ต้องการ beautifulsoup4 — pip install beautifulsoup4")
        except Exception as e:
            logger.error(f"LED parse error: {e}")
        return results
