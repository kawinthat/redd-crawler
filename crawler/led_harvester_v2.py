"""
led_harvester_v2.py — กรมบังคับคดี NPA Scraper (v5 — confirmed correct via Chrome inspection)

สิ่งที่ค้นพบจาก live inspection โดยตรง:
  1. CAPTCHA อยู่ใน input#opass[name="oseckey"] — มีค่าอยู่แล้ว ไม่ต้อง OCR
  2. ต้อง submit ผ่าน JS document.forms['webForm'].submit() ไม่ใช่ click button
  3. ข้อมูลครบทุกอย่างอยู่ใน forms web1-web30 (hidden fields) บนหน้า results แล้ว
     → ไม่ต้องคลิกเข้า detail page เพิ่ม!
  4. Fields สำคัญ: assetprice1-9, biddate1-8, ReserveFund, remark, auc_asset_gen,
     assettypedesc, deedtumbol, deedampur, deedcity, ownername, law_suit_no, person1/2
  5. Pagination: webForm1-5 = หน้า 1-5, webFormn2 = กลุ่มหน้าถัดไป
  6. "ปทุมธานี  สาขาธัญบุรี" มี double space ใน dropdown value
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from loguru import logger

LED_URL = "https://asset.led.go.th/newbidreg/default.asp"

# Region values ที่ verified จาก live browser inspection
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
    "ปทุมธานี  สาขาธัญบุรี",   # double space — ตรงกับ dropdown value จริง
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
    "ที่ดิน": "land",
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


def _parse_type(txt: str) -> str:
    txt = (txt or "").strip()
    for kw, pt in TYPE_MAP.items():
        if kw in txt:
            return pt
    return "other"


def _thai_date_to_ce(thai_date: str) -> str:
    """แปลง '25690109' → '2026-01-09'"""
    if not thai_date or len(thai_date) < 8:
        return ""
    try:
        be_year = int(thai_date[:4])
        month   = thai_date[4:6]
        day     = thai_date[6:8]
        ce_year = be_year - 543
        return f"{ce_year}-{month}-{day}"
    except Exception:
        return ""


def _first_nonzero_price(fields: dict) -> Optional[int]:
    """หาราคาจาก assetprice1-9 — เอา non-zero ตัวแรก"""
    for i in range(1, 10):
        v = fields.get(f"assetprice{i}", "0") or "0"
        p = _parse_price(v)
        if p and p > 0:
            return int(p)
    return None


def _area_sqm(fields: dict) -> Optional[float]:
    """
    แปลงพื้นที่จาก LED form fields:
    - ห้องชุด/คอนโด: wa field = ตร.ม. โดยตรง (confirmed จาก live inspection:
      remark "41.36+3.87=45.23 ตร.ม." ตรงกับ wa=45.23)
    - ทุกประเภทอื่น (บ้าน, ที่ดิน, ทาวน์เฮ้าส์, อาคาร):
      wa = ตร.วา → แปลง: rai*1600 + quater*400 + wa*4
    """
    ptype_raw = (fields.get("assettypedesc") or "").strip()
    is_condo  = any(kw in ptype_raw for kw in ["ห้องชุด", "คอนโด", "กรรมสิทธิ์ห้องชุด"])

    try:
        rai    = float(fields.get("rai", "0") or 0)
        quater = float(fields.get("quaterrai", "0") or 0)
        wa     = float(fields.get("wa", "0") or 0)

        if is_condo:
            sqm = wa                               # ตร.ม. โดยตรง
        else:
            sqm = rai * 1600 + quater * 400 + wa * 4  # ตร.วา → ตร.ม.

        return round(sqm, 2) if sqm > 0 else None
    except Exception:
        return None


class LEDHarvesterV2:
    """
    กรมบังคับคดี Playwright scraper (v5 — correct form submission + webN form parsing)

    Architecture:
    1. JS form submit (ไม่ใช้ Playwright click — ปุ่มมีหลายตัว สับสน)
    2. Extract ข้อมูลจาก web1-web30 hidden form fields (ไม่ต้องคลิก detail page!)
    3. Paginate ผ่าน webFormN forms
    """

    def __init__(self, delay: float = 1.0, max_captcha_retries: int = 3):
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

        logger.info("LED: launching Playwright Chromium...")
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
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="th-TH",
                )
                page = await context.new_page()

                for region in TARGET_REGIONS:
                    if len(results) >= max_listings:
                        break
                    logger.info(f"LED: ▶ scrape region '{region}'")
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
            logger.error(f"LED: Playwright fatal error: {e}")
            return []

        logger.info(f"LED v5: harvested {len(results)} total")
        return results[:max_listings]

    # ──────────────────────────────────────────────────────────────────
    # Per-region scrape loop
    # ──────────────────────────────────────────────────────────────────

    async def _scrape_region(
        self, page, region: str, max_pages: int, remaining: int
    ) -> list[dict]:
        results: list[dict] = []

        # 1. โหลดหน้า search ใหม่ — wait networkidle ให้ JS set CAPTCHA ครบก่อน
        try:
            await page.goto(LED_URL, wait_until="networkidle", timeout=45_000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"LED: goto networkidle timeout '{region}', fallback domcontentloaded: {e}")
            try:
                await page.goto(LED_URL, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(3000)  # extra wait for JS to populate CAPTCHA
            except Exception as e2:
                logger.error(f"LED: goto failed '{region}': {e2}")
                return []

        # 2. อ่าน CAPTCHA จาก #opass (id ที่ confirmed จาก live inspection)
        # ถ้า field ยังไม่มีค่า ให้ wait poll สูงสุด 5 วิ
        captcha_val: str = ""
        for _attempt in range(10):
            captcha_val = await page.evaluate(
                "document.getElementById('opass')?.value || ''"
            )
            if not captcha_val:
                captcha_val = await page.evaluate(
                    "document.querySelector('input[name=\"oseckey\"]')?.value || ''"
                )
            if captcha_val:
                break
            await page.wait_for_timeout(500)

        if not captcha_val:
            # Log page state เพื่อ debug ว่าหน้าโหลดมาถูกไหม
            page_html = await page.content()
            has_form = "webForm" in page_html
            has_opass = "opass" in page_html
            logger.warning(
                f"LED: '{region}' — ไม่พบ CAPTCHA value "
                f"(has_webForm={has_form}, has_opass={has_opass}, url={page.url}, "
                f"html_snippet={page_html[200:500]})"
            )
            return []
        logger.info(f"LED: '{region}' CAPTCHA = '{captcha_val}'")

        # 3. Submit ผ่าน JS — set seckey + region_name แล้ว submit webForm
        submitted = await page.evaluate(f"""
            (function() {{
                var form = document.forms['webForm'];
                if (!form) {{ return 'no-form'; }}

                // กรอก CAPTCHA
                var secField = document.getElementById('pass') || form.seckey;
                if (secField) secField.value = '{captcha_val}';

                // เลือก region
                var sel = form.region_name;
                if (!sel) {{ return 'no-region-select'; }}
                var found = false;
                for (var i = 0; i < sel.options.length; i++) {{
                    if (sel.options[i].value === '{region}' ||
                        sel.options[i].text.trim() === '{region}'.trim()) {{
                        sel.selectedIndex = i;
                        found = true;
                        break;
                    }}
                }}
                if (!found) {{ return 'region-not-found:' + sel.options.length + '-options'; }}

                form.submit();
                return 'submitted';
            }})()
        """)
        logger.info(f"LED: '{region}' submit result = '{submitted}'")

        if "submitted" not in str(submitted):
            logger.error(f"LED: '{region}' form submit failed: {submitted}")
            return []

        # 4. รอหน้าผลลัพธ์โหลด
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1000)
        except Exception as e:
            logger.error(f"LED: '{region}' wait for results failed: {e}")
            return []

        # 5. ตรวจว่า search สำเร็จ — ต้องมี web1 form (listing detail form)
        has_web1 = await page.evaluate("!!document.forms['web1']")
        if not has_web1:
            # Log page info เพื่อ debug
            page_text = await page.inner_text("body")
            logger.warning(
                f"LED: '{region}' ไม่พบ web1 form — "
                f"URL={page.url} "
                f"page snippet: {page_text[:300]}"
            )
            return []

        # 6. หา total pages
        total_pages = await self._get_total_pages(page)
        logger.info(f"LED: '{region}' → {total_pages} หน้า")

        # 7. Parse หน้าแรก
        page_results = await self._parse_web_forms(page, region)
        results.extend(page_results)
        logger.info(f"LED: '{region}' หน้า 1/{total_pages} → {len(page_results)} รายการ")

        # 8. Paginate — ใช้ webFormN forms
        for page_num in range(2, min(total_pages + 1, max_pages + 1)):
            if len(results) >= remaining:
                break
            ok = await self._go_to_page(page, page_num, total_pages)
            if not ok:
                logger.warning(f"LED: '{region}' ไปหน้า {page_num} ไม่ได้ — หยุด")
                break
            page_results = await self._parse_web_forms(page, region)
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
    # Parse web1-webN forms on current results page
    # ──────────────────────────────────────────────────────────────────

    async def _parse_web_forms(self, page, region: str) -> list[dict]:
        """
        ดึงข้อมูลจาก web1-webN hidden form fields บนหน้า results
        แต่ละ form มี action=asset_open.asp และมี fields:
          auc_lot, assettypedesc, rai, quaterrai, wa,
          biddate1-8, assetprice1-9, ReserveFund, ReserveFund1,
          remark, deedno, deedtumbol, deedampur, deedcity, landtype,
          ownername, law_suit_no, law_suit_year, law_court_name,
          person1, person2, province_name, auc_asset_gen, addrno, etc.
        """
        forms_data = await page.evaluate("""
            (() => {
                const result = [];
                const forms = document.forms;
                for (let i = 0; i < forms.length; i++) {
                    const form = forms[i];
                    if (!form || !form.action || !form.action.includes('asset_open.asp')) continue;
                    const fields = {};
                    for (let j = 0; j < form.elements.length; j++) {
                        const el = form.elements[j];
                        if (el.name) fields[el.name] = el.value;
                    }
                    if (fields.auc_asset_gen) result.push(fields);
                }
                return result;
            })()
        """)

        results = []
        for fields in (forms_data or []):
            try:
                record = self._fields_to_record(fields, region)
                if record:
                    results.append(record)
            except Exception as e:
                logger.warning(f"LED: parse form error: {e} | fields keys={list(fields.keys())[:10]}")
        return results

    def _fields_to_record(self, fields: dict, region: str) -> Optional[dict]:
        """แปลง form hidden fields → normalized record สำหรับ RE:DD"""
        asset_gen = fields.get("auc_asset_gen", "")
        if not asset_gen:
            return None

        # ราคา — หา non-zero แรก จาก assetprice1-9
        price = _first_nonzero_price(fields)
        if not price or price <= 0:
            return None

        # ประเภท
        ptype_raw = (fields.get("assettypedesc") or "").strip()
        ptype = _parse_type(ptype_raw)

        # พื้นที่
        area_sqm = _area_sqm(fields)

        # ที่อยู่ — ใช้ deed location (ครบกว่า)
        deed_tumbol  = (fields.get("deedtumbol") or "").strip()
        deed_ampur   = (fields.get("deedampur") or "").strip()
        deed_city    = (fields.get("deedcity") or "").strip()
        addr_no      = (fields.get("addrno") or "").strip()

        # fallback: tumbol/ampur/city จาก form (อาจเป็น "-")
        tumbol = (fields.get("tumbol") or "").replace("-","").strip()
        ampur  = (fields.get("ampur") or "").replace("-","").strip()
        city   = (fields.get("city") or "").replace("-","").strip()

        location_parts = [deed_tumbol or tumbol, deed_ampur or ampur, deed_city or city]
        location = " ".join(p for p in location_parts if p).strip()
        if not location:
            location = region

        # วันประมูล
        bid_dates = [
            _thai_date_to_ce(fields.get(f"biddate{i}", ""))
            for i in range(1, 9)
        ]
        bid_dates = [d for d in bid_dates if d]

        # ราคาประมูลแต่ละนัด
        asset_prices = {
            f"round_{i}": int(float(fields.get(f"assetprice{i}", 0) or 0))
            for i in range(1, 10)
            if int(float(fields.get(f"assetprice{i}", 0) or 0)) > 0
        }

        # รายละเอียด
        remark = (fields.get("remark") or "").strip()

        # source_url ใช้ auc_asset_gen เป็น unique ID
        source_url = (
            f"https://asset.led.go.th/newbidreg/asset_open.asp"
            f"?auc_asset_gen={asset_gen}"
        )

        # title
        lot_no = (fields.get("auc_lot") or fields.get("str_bid_num") or "").strip()
        title_parts = [ptype_raw, lot_no, deed_tumbol or tumbol, deed_ampur or ampur]
        title = " ".join(p for p in title_parts if p).strip()

        # โฉนด
        deed_no   = (fields.get("deedno") or "").strip()
        land_type = (fields.get("landtype") or "").strip()

        return {
            "source_url":    source_url,
            "source_domain": "asset.led.go.th",
            "property_type": ptype,
            "price":         price,
            "area_sqm":      area_sqm,
            "condition":     "fair",
            "location":      location,
            "title":         title,
            "auction_date":  bid_dates[0] if bid_dates else None,  # earliest bid date → DB column
            "is_benchmark":  False,
            "raw_data": {
                "auc_asset_gen":  asset_gen,
                "auc_lot":        lot_no,
                "ptype_raw":      ptype_raw,
                "asset_prices":   asset_prices,
                "bid_dates":      bid_dates,
                "reserve_fund":   fields.get("ReserveFund", ""),
                "reserve_fund1":  fields.get("ReserveFund1", ""),
                "remark":         remark,
                "addr_no":        addr_no,
                "deed_no":        deed_no,
                "land_type":      land_type,
                "deed_tumbol":    deed_tumbol,
                "deed_ampur":     deed_ampur,
                "deed_city":      deed_city,
                "owner_name":     fields.get("ownername", ""),
                "sale_type":      fields.get("saletypename", ""),
                "sale_location":  fields.get("sale_location1", ""),
                "law_court":      fields.get("law_court_name", ""),
                "law_suit":       f"{fields.get('law_suit_no','')} / {fields.get('law_suit_year','')}",
                "creditor":       fields.get("person1", ""),
                "debtor":         fields.get("person2", ""),
                "region":         region,
                "occupant":       fields.get("occupant", ""),
            },
        }

    # ──────────────────────────────────────────────────────────────────
    # Pagination helpers
    # ──────────────────────────────────────────────────────────────────

    async def _get_total_pages(self, page) -> int:
        """อ่าน total pages — หลายรูปแบบ"""
        try:
            # รูปแบบ: หน้าที่ 1/88
            text = await page.inner_text("body")
            match = re.search(r"หน้าที่\s+\d+/(\d+)", text)
            if match:
                return int(match.group(1))
            # รูปแบบ: webFormnn{N} — form name encodes last page
            last_form = await page.evaluate("""
                (() => {
                    const forms = document.forms;
                    for (let i = 0; i < forms.length; i++) {
                        const name = forms[i].name || '';
                        const m = name.match(/^webFormnn(\d+)$/);
                        if (m) return parseInt(m[1]);
                    }
                    return 0;
                })()
            """)
            if last_form and last_form > 0:
                return last_form
        except Exception:
            pass
        return 1

    async def _go_to_page(self, page, page_num: int, total_pages: int) -> bool:
        """
        Navigate ไปหน้าที่ต้องการ:
        - ลอง webFormN โดยตรง (N = page number ที่ต้องการ ถ้ามีใน visible range)
        - ถ้าไม่พบ → advance page group ก่อน (webFormn{group_num})
        - Fallback: webFormnn (last page), webFormp (prev page)
        """
        try:
            # ลอง submit webForm{page_num} โดยตรง
            result = await page.evaluate(f"""
                (() => {{
                    const form = document.forms['webForm{page_num}'];
                    if (form) {{ form.submit(); return 'ok'; }}
                    return 'not-found';
                }})()
            """)
            if result == "ok":
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(500)
                return True

            # ไม่พบ webForm{page_num} — ต้อง advance page group
            # หา "next group" forms ทั้งหมด: webFormn{N}
            advance_result = await page.evaluate(f"""
                (() => {{
                    const forms = document.forms;
                    // ลอง webFormn+{page_num} ทุกรูปแบบ
                    for (let i = 0; i < forms.length; i++) {{
                        const name = (forms[i].name || '');
                        if (name.startsWith('webFormn') && !name.startsWith('webFormnn')) {{
                            forms[i].submit();
                            return 'advanced:' + name;
                        }}
                    }}
                    return 'no-advance';
                }})()
            """)
            logger.debug(f"LED: advance page group result: {advance_result}")

            if advance_result.startswith("advanced"):
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(500)
                # ตอนนี้ page group ใหม่โหลดแล้ว — ลอง submit webForm{page_num} อีกครั้ง
                result2 = await page.evaluate(f"""
                    (() => {{
                        const form = document.forms['webForm{page_num}'];
                        if (form) {{ form.submit(); return 'ok'; }}
                        // ลอง webForm1 สำหรับหน้าแรกของ group ใหม่
                        const form1 = document.forms['webForm1'];
                        if (form1) {{ form1.submit(); return 'group-first'; }}
                        return 'not-found';
                    }})()
                """)
                if result2 in ("ok", "group-first"):
                    await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(500)
                    return True

        except Exception as e:
            logger.warning(f"LED: go_to_page({page_num}) error: {e}")
        return False

    async def fetch_urls_only(self, max_pages: int = 20) -> list[str]:
        listings = await self.fetch_all(max_pages=max_pages)
        return [x["source_url"] for x in listings]
