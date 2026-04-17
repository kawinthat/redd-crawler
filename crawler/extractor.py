"""
extractor.py — Detail Page Extractor + ROI Engine
ดึงข้อมูลจากหน้า detail อัตโนมัติ
"""

import json
import re
from typing import Optional

import anthropic
from bs4 import BeautifulSoup


# ─────────────────────────────────────────────
# HTML CLEANER — ลด token 94%
# ─────────────────────────────────────────────

class HtmlCleaner:

    REMOVE_TAGS = [
        "script", "style", "nav", "footer", "header",
        "iframe", "svg", "img", "button", "form",
        "meta", "link", "noscript", "aside",
    ]

    PRICE_HINTS = [
        "ราคา", "price", "บาท", "฿", "ล้าน", "million",
        "ต่อรอง", "negotiate",
    ]

    AREA_HINTS = [
        "ตร.ม", "ตรม", "ตารางเมตร", "sq.m", "sqm",
        "พื้นที่", "area", "ขนาด",
    ]

    def clean(self, raw_html: str, max_chars: int = 4000) -> str:
        soup = BeautifulSoup(raw_html, "html.parser")

        # ลบ tags ที่ไม่มีข้อมูล
        for tag in soup(self.REMOVE_TAGS):
            tag.decompose()

        # เน้น section ที่มีราคา/พื้นที่ (ให้น้ำหนักมากกว่า)
        priority_text = []
        normal_text = []

        for el in soup.find_all(["div", "section", "article", "li", "p", "td", "span", "h1", "h2", "h3"]):
            text = el.get_text(separator=" ", strip=True)
            if len(text) < 3:
                continue

            has_price = any(h in text for h in self.PRICE_HINTS)
            has_area  = any(h in text for h in self.AREA_HINTS)

            if has_price or has_area:
                priority_text.append(text)
            else:
                normal_text.append(text)

        # รวม priority ก่อน
        combined = "\n".join(priority_text) + "\n---\n" + "\n".join(normal_text)

        # Clean whitespace
        combined = re.sub(r'\n{3,}', '\n\n', combined)
        combined = re.sub(r' {3,}', ' ', combined)

        return combined[:max_chars]


# ─────────────────────────────────────────────
# AI EXTRACTOR — Batch mode
# ─────────────────────────────────────────────

PROPERTY_SCHEMA = """{
  "price": <number หรือ null — ราคา บาท>,
  "area_sqm": <number หรือ null — พื้นที่ ตร.ม>,
  "usable_area_sqm": <number หรือ null — พื้นที่ใช้สอย>,
  "land_area_sqm": <number หรือ null — เนื้อที่ดิน>,
  "location": <string — จังหวัด/เขต/ทำเล>,
  "address": <string หรือ null — ที่อยู่เต็ม>,
  "property_type": <"condo"|"house"|"townhouse"|"land"|"commercial"|"other">,
  "bedrooms": <number หรือ null>,
  "bathrooms": <number หรือ null>,
  "floors": <number หรือ null>,
  "condition": <"new"|"good"|"fair"|"poor">,
  "project_name": <string หรือ null>,
  "title_deed": <string หรือ null — โฉนด/น.ส.3ก/ฯลฯ>,
  "features": <string หรือ null — สิ่งอำนวยความสะดวก>,
  "source_type": <"bank_npa"|"enforcement"|"private"|"developer"|"agent">,
  "auction_date": <string หรือ null — วันขายทอดตลาด>,
  "contact": <string หรือ null>
}"""


class DetailExtractor:

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.cleaner = HtmlCleaner()

    async def extract_batch(
        self, listings: list[dict], batch_size: int = 10
    ) -> list[dict]:
        """
        ส่ง N listings ต่อ 1 API call
        listings = [{"url": str, "html": str}, ...]
        """
        results = []

        for i in range(0, len(listings), batch_size):
            batch = listings[i:i + batch_size]
            batch_results = await self._extract_one_batch(batch)
            results.extend(batch_results)

            print(f"  🤖 Extracted batch {i//batch_size + 1}: "
                  f"{len(batch_results)} listings")

        return results

    async def _extract_one_batch(self, batch: list[dict]) -> list[dict]:
        # Clean HTML ทุกชิ้น
        cleaned = []
        for item in batch:
            text = self.cleaner.clean(item["html"])
            cleaned.append(f"[IDX_{batch.index(item)}]\nURL: {item['url']}\n\n{text}")

        combined = "\n\n===NEXT_LISTING===\n\n".join(cleaned)

        prompt = f"""คุณคือ AI ที่เชี่ยวชาญดึงข้อมูลอสังหาริมทรัพย์จากหน้าเว็บไทย

ดึงข้อมูลจาก {len(batch)} listings ด้านล่าง แต่ละ listing คั่นด้วย ===NEXT_LISTING===
แต่ละ listing เริ่มด้วย [IDX_N]

ตอบเป็น JSON array เท่านั้น ไม่มีข้อความอื่น ไม่มี markdown
Array มี {len(batch)} objects ตาม index 0 ถึง {len(batch)-1}

Schema ต่อ object:
{PROPERTY_SCHEMA}

ถ้าหาข้อมูลไม่เจอให้ใส่ null อย่าเดา

Listings:
{combined}"""

        try:
            msg = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()

            # Clean JSON fences
            raw = re.sub(r'^```json\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)

            extracted = json.loads(raw)

            # Merge back URL
            for j, item in enumerate(batch):
                if j < len(extracted):
                    extracted[j]["listing_url"] = item["url"]
                    extracted[j]["source_domain"] = item.get("source_domain", "")

            return extracted

        except Exception as e:
            print(f"  ❌ Batch extraction error: {e}")
            # Fallback: return empty records with URLs
            return [{"listing_url": item["url"], "error": str(e)} for item in batch]


# ─────────────────────────────────────────────
# ROI ENGINE
# ─────────────────────────────────────────────

RENO_COST_MATRIX = {
    # (property_type, condition): บาท/ตร.ม
    ("condo",     "poor"):  8000,
    ("condo",     "fair"):  4500,
    ("condo",     "good"):  1500,
    ("condo",     "new"):   0,
    ("house",     "poor"):  7000,
    ("house",     "fair"):  4000,
    ("house",     "good"):  1200,
    ("house",     "new"):   0,
    ("townhouse", "poor"):  6500,
    ("townhouse", "fair"):  3500,
    ("townhouse", "good"):  1000,
    ("townhouse", "new"):   0,
    ("land",      "poor"):  0,
    ("land",      "fair"):  0,
    ("land",      "good"):  0,
    ("land",      "new"):   0,
}

DEFAULT_MARKET_PRICE_SQM = {
    # ราคาตลาดอ้างอิง บาท/ตร.ม (fallback ถ้าไม่มี comparable)
    "กรุงเทพมหานคร":  95000,
    "กรุงเทพ":        95000,
    "นนทบุรี":        45000,
    "ปทุมธานี":       35000,
    "สมุทรปราการ":    40000,
    "เชียงใหม่":      45000,
    "ภูเก็ต":         80000,
    "ชลบุรี":         55000,
    "default":        40000,
}


class ROIEngine:

    def calculate(
        self,
        data: dict,
        market_price_sqm: Optional[float] = None,
        reno_cost_sqm: Optional[float] = None,
    ) -> dict:

        price = data.get("price")
        area  = data.get("area_sqm") or data.get("usable_area_sqm")

        if not price or not area or price <= 0 or area <= 0:
            return {"roi_valid": False, "roi_skip_reason": "ข้อมูลราคา/พื้นที่ไม่ครบ"}

        prop_type = data.get("property_type", "condo")
        condition = data.get("condition", "fair")
        location  = data.get("location", "")

        # ─ ต้นทุนรีโนเวท ─
        if reno_cost_sqm is None:
            reno_cost_sqm = RENO_COST_MATRIX.get(
                (prop_type, condition),
                RENO_COST_MATRIX.get(("condo", condition), 4500)
            )

        # ─ ราคาตลาด ─
        if market_price_sqm is None:
            for key, val in DEFAULT_MARKET_PRICE_SQM.items():
                if key in location:
                    market_price_sqm = val
                    break
            else:
                market_price_sqm = DEFAULT_MARKET_PRICE_SQM["default"]

        # ─ คำนวณ ─
        reno_total    = area * reno_cost_sqm
        transfer_fee  = price * 0.04           # ค่าโอน + จดจำนอง
        total_cost    = price + reno_total + transfer_fee
        market_value  = area * market_price_sqm
        profit        = market_value - total_cost
        roi           = (profit / total_cost) * 100

        # ─ Flag ─
        if roi >= 30:
            flag     = "🟢 ควรซื้อ"
            priority = "HIGH"
        elif roi >= 15:
            flag     = "🟡 พิจารณา"
            priority = "MEDIUM"
        else:
            flag     = "🔴 ข้ามไป"
            priority = "LOW"

        return {
            "roi_valid":        True,
            "buy_price":        price,
            "area_sqm":         area,
            "reno_cost_total":  round(reno_total),
            "reno_cost_sqm":    reno_cost_sqm,
            "transfer_fee":     round(transfer_fee),
            "total_cost":       round(total_cost),
            "market_price_sqm": market_price_sqm,
            "market_value":     round(market_value),
            "estimated_profit": round(profit),
            "roi_percent":      round(roi, 2),
            "roi_flag":         flag,
            "priority":         priority,
        }
