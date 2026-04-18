"""
perplexity_analyzer.py — RE:DD AI Market Intelligence Engine
ใช้ Perplexity Sonar ผ่าน OpenRouter API วิเคราะห์แต่ละทรัพย์:
  - ราคาตลาดก่อน/หลังรีโนเวท (ข้อมูลจริงจากเว็บ)
  - ค่าประมาณรีโนเวท (flat 5,000 ฿/ตร.ม.)
  - ROI คาดการณ์

Token Efficiency Strategy:
  1. ตรวจ MarketCache ก่อน — ถ้า cache hit ไม่เรียก AI เลย (ประหยัด 100% token)
  2. ขอ JSON output เท่านั้น — ประหยัด ~60% tokens
  3. Always sonar-pro — real-time web search + แม่นกว่า
  4. บันทึก cache หลัง sonar-pro analysis — deals หมู่บ้านเดียวกันไม่ต้องวิเคราะห์ใหม่
  5. Rate limit: 8 req/min (sonar-pro limit)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from loguru import logger

# Import MarketCache (lazy to avoid circular imports)
_market_cache: "Any" = None

def _get_market_cache():
    global _market_cache
    if _market_cache is None:
        try:
            from crawler.market_cache import MarketCache
            _market_cache = MarketCache()
        except Exception as e:
            logger.warning(f"MarketCache init failed: {e}")
            _market_cache = False   # sentinel: don't retry
    return _market_cache if _market_cache else None

# ── OpenRouter endpoint (รองรับ Perplexity Sonar + Claude + Llama ฯลฯ) ──────
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# ── Models via OpenRouter ─────────────────────────────────
# ใช้ sonar-pro ทุก deal — real-time web search + แม่นกว่า sonar ทั่วไป
MODEL_PRO = "perplexity/sonar-pro"

# ── Prompt Template ───────────────────────────────────────
# ขอข้อมูลครบ 4 ส่วน: โครงการ / ราคา 3 ระดับ / กลุ่มลูกค้า / ทำเล
# แหล่งข้อมูล: เฉพาะเว็บฝากขายเท่านั้น ห้ามประมาณ
PROMPT_TEMPLATE = """\
วิเคราะห์ทรัพย์อสังหาริมทรัพย์นี้โดยค้นหาข้อมูลจากเว็บฝากขายบ้านต่อไปนี้เท่านั้น:
• ddproperty.com  • fazwaz.co.th  • baania.com  • livinginsider.com
• dotproperty.co.th  • propertyonlineplus.com  • kaidee.com
• ghbhomecenter.com (ธอส.)  • facebook.com/marketplace (ประกาศขายบ้าน)

ทรัพย์เป้าหมาย:
ประเภท: {type_th}
โครงการ/หมู่บ้าน: {project_name}
พื้นที่/ขนาด: {area}
จังหวัด: {province}  |  เขต/อำเภอ: {district}
ราคา ประมูล (ราคาซื้อ): {buy_price_fmt}

ต้องการข้อมูล 4 ส่วน:

[ส่วนที่ 1 — ข้อมูลโครงการ]
ค้นหา: ชื่อทางการของโครงการ, ผู้พัฒนา/เจ้าของโครงการ, ที่อยู่จริง (ตำบล อำเภอ จังหวัด รหัสไปรษณีย์),
จำนวนห้องนอน/ห้องน้ำ, พื้นที่ใช้สอย (ตร.ม.), สิ่งอำนวยความสะดวกในโครงการ

[ส่วนที่ 2 — ราคาขาย 3 ระดับ (สำคัญมาก)]
ค้นหาประกาศขายจริงจากเว็บที่ระบุเท่านั้น แบ่งเป็น:
  ระดับ A — สภาพเดิม ไม่รีโนเวท (ราคาต่ำสุด-สูงสุด พร้อม URL ประกาศ)
  ระดับ B — สภาพดี / ต่อเติมบ้าง / มีผู้เช่า (ราคาต่ำสุด-สูงสุด พร้อม URL ประกาศ)
  ระดับ C — รีโนเวทใหม่ ปรับปรุงครบ พร้อมอยู่ (ราคาต่ำสุด-สูงสุด พร้อม URL ประกาศ)
  ค่าเช่า — ราคาเช่าต่อเดือนโดยประมาณ (ถ้าหาเจอ)

[ส่วนที่ 3 — กลุ่มลูกค้าเป้าหมาย]
วิเคราะห์: กลุ่มหลัก, ช่วงรายได้, อาชีพหลัก (2-3 อาชีพ), กลุ่มรอง

[ส่วนที่ 4 — ทำเลและการเดินทาง]
ระยะทาง/เวลาไปยัง: BTS/MRT ใกล้ที่สุด, ห้างสรรพสินค้า, ทางพิเศษ/วงแหวน, นิคมอุตสาหกรรม/โรงงาน

กฎ:
1. พยายามหาประกาศขายจริงจากเว็บที่ระบุก่อนเป็นอันดับแรก
2. listing_urls ต้องเป็น URL ประกาศขายจริง ห้ามใส่ URL หน้าค้นหาหรือหน้าหลัก
3. ถ้าหาประกาศขายตรงๆ ไม่เจอ — ให้ประมาณราคาตลาดจากทรัพย์คล้ายกันในพื้นที่/จังหวัดเดียวกัน
   โดยใช้ข้อมูลราคาทรัพย์ประเภทเดียวกัน ขนาดใกล้เคียง ในเขต/อำเภอเดียวกันหรือใกล้เคียง
   ในกรณีนี้ listing_urls = [] แต่ต้องใส่ราคาประมาณการ (อย่าใส่ null ทุก field)
4. ตอบ JSON เท่านั้น ห้ามมีข้อความอื่น ห้าม backtick ห้าม markdown

JSON (ราคาหน่วย: บาท):
{{
  "project": {{
    "official_name": null,
    "developer": null,
    "exact_address": null,
    "bedrooms": null,
    "bathrooms": null,
    "usable_sqm_low": null,
    "usable_sqm_high": null,
    "amenities": []
  }},
  "pricing": {{
    "original_low": null,
    "original_high": null,
    "original_urls": [],
    "good_condition_low": null,
    "good_condition_high": null,
    "good_condition_urls": [],
    "after_reno_low": null,
    "after_reno_high": null,
    "after_reno_urls": [],
    "rental_monthly": null
  }},
  "target_buyers": {{
    "primary_group": null,
    "income_range": null,
    "main_occupations": [],
    "secondary_group": null
  }},
  "location_access": [],
  "summary_th": null
}}

คำอธิบาย:
- pricing.original_low/high: ราคาระดับ A (สภาพเดิม)
- pricing.good_condition_low/high: ราคาระดับ B (สภาพดี)
- pricing.after_reno_low/high: ราคาระดับ C (รีโนเวทใหม่) ← ใช้คำนวณ ROI
- location_access: array ของ {{"point":"BTS คูคต","detail":"10-15 นาที ขับรถ"}}
- summary_th: สรุป 3-4 ประโยค: ราคาที่ค้นเจอ, ศักยภาพ Flip/Rent, ข้อควรระวัง"""

TYPE_TH_MAP = {
    "house":      "บ้านเดี่ยว",
    "townhouse":  "ทาวน์เฮ้าส์",
    "condo":      "คอนโด",
    "land":       "ที่ดินเปล่า",
    "commercial": "อาคารพาณิชย์",
    "other":      "ทรัพย์",
}

CONDITION_TH_MAP = {
    "new":  "ใหม่/สภาพดีมาก",
    "good": "ดี",
    "fair": "พอใช้ (ต้องรีโนเวทบ้าง)",
    "poor": "ต้องซ่อมแซมมาก",
}


def _build_prompt(deal: dict) -> str:
    """Build a Perplexity Sonar Pro prompt from a deal dict.

    Note: For LED enforcement deals (source_type='bank_npa' or 'enforcement'),
    the prompt treats the auction price as the acquisition cost and seeks
    comparable market prices for flip analysis.
    """
    import re
    loc = deal.get("location") or ""
    m = re.search(r"^([ก-๙a-zA-Z]+)\s+((?:อำเภอ|เขต)[ก-๙a-zA-Z ]+)", loc)
    province = m.group(1) if m else loc
    district = m.group(2) if m else (loc or "-")

    area_sqm = deal.get("area_sqm") or deal.get("land_area_sqm") or 0
    area_str = f"{area_sqm:.1f} ตร.ม." if area_sqm else "ไม่ระบุ"

    type_th     = TYPE_TH_MAP.get(deal.get("property_type", "other"), "ทรัพย์")
    condition_th = CONDITION_TH_MAP.get(deal.get("condition", "fair"), "พอใช้")
    project     = deal.get("project_name") or "-"

    buy_price = deal.get("buy_price") or deal.get("price") or 0
    buy_price_fmt = f"฿{buy_price:,.0f}" if buy_price else "ไม่ระบุ"

    return PROMPT_TEMPLATE.format(
        type_th=type_th,
        project_name=project,
        area=area_str,
        province=province,
        district=district,
        buy_price_fmt=buy_price_fmt,
    )


class PerplexityAnalyzer:
    """
    Enriches deals with AI market intelligence from Perplexity Sonar.

    Usage:
        analyzer = PerplexityAnalyzer()
        result = await analyzer.analyze_deal(deal_dict)
        # result = {"price_before_reno": {...}, ..., "summary_th": "..."}
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        rate_limit_per_min: int = 8,   # sonar-pro มี rate limit ต่ำกว่า sonar ทั่วไป
    ):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        self._min_interval = 60.0 / rate_limit_per_min
        self._last_call_ts = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.api_key.startswith("sk-or-")

    async def analyze_deal(self, deal: dict) -> Optional[dict]:
        """
        วิเคราะห์ 1 deal → return enrichment dict (หรือ None ถ้า fail).

        Args:
            deal: deal dict จาก Supabase (ต้องมี location / property_type)

        Returns:
            dict ที่ merge กลับเข้า deal ได้เลย เช่น:
            {
              "market_value": 4500000,
              "reno_cost_total": 800000,
              "estimated_profit": 1200000,
              "ai_analysis": {...full JSON...},
              "ai_analyzed_at": "2026-04-17T..."
            }
        """
        if not self.enabled:
            logger.debug("PerplexityAnalyzer disabled — ไม่มี API key")
            return None

        # Skip deals ที่ไม่มีข้อมูลพอวิเคราะห์
        if not deal.get("location") and not deal.get("project_name"):
            logger.debug(f"Skip deal {deal.get('id','?')} — ไม่มี location/project_name")
            return None

        # ── Check market pattern cache ──────────────────────────────────
        cache = _get_market_cache()
        if cache:
            cached = await cache.get_pattern(deal)
            if cached:
                logger.info(
                    f"♻️  Cache used for deal {deal.get('id','?')} — "
                    f"ROI {cached.get('roi_percent','?')}% (no AI call)"
                )
                return cached

        # Rate limiting (only hit when cache miss)
        await self._rate_limit()

        prompt = _build_prompt(deal)

        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    OPENROUTER_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type":  "application/json",
                        "HTTP-Referer":  "https://redd-crawler.onrender.com",
                        "X-Title":       "RE:DD Real Estate Analyzer",
                    },
                    json={
                        "model":    MODEL_PRO,   # ใช้ sonar-pro ทุก deal
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "คุณคือผู้วิเคราะห์ราคาอสังหาริมทรัพย์ไทย "
                                    "ขั้นตอน: 1) ค้นหาประกาศขายจริงจาก ddproperty.com fazwaz.co.th baania.com "
                                    "livinginsider.com dotproperty.co.th propertyonlineplus.com kaidee.com ก่อน "
                                    "2) ถ้าหาประกาศขายตรงๆ ไม่เจอ ให้ประมาณราคาตลาดจากทรัพย์ใกล้เคียงในพื้นที่เดียวกัน "
                                    "(ใส่ราคาประมาณการ listing_urls=[]) ห้ามใส่ null ทุก pricing field "
                                    "ตอบด้วย JSON ที่ถูกต้องเท่านั้น ห้ามมีข้อความอื่น ห้าม backtick"
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens":  1500,   # เพิ่มเพราะ 4 ส่วนข้อมูลต้องการ token มากขึ้น
                        "temperature": 0.0,   # 0 = deterministic, ห้ามคาดเดา
                    },
                )
                resp.raise_for_status()
                raw_content = resp.json()["choices"][0]["message"]["content"].strip()

        except Exception as e:
            logger.error(f"Perplexity API error deal {deal.get('id','?')}: {e}")
            return None

        # ── Parse JSON ──────────────────────────────────────────────────
        try:
            if raw_content.startswith("```"):
                raw_content = raw_content.split("```")[1]
                if raw_content.startswith("json"):
                    raw_content = raw_content[4:]
            analysis: dict[str, Any] = json.loads(raw_content.strip())
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error deal {deal.get('id','?')}: {e} | raw={raw_content[:200]}")
            return None

        # ── Map fields → Supabase deal columns ─────────────────────────
        # รวบรวม listing URLs จากทุก tier ใน pricing
        pricing    = analysis.get("pricing") or {}
        proj_info  = analysis.get("project") or {}

        all_urls: list[str] = []
        for url_field in ("original_urls", "good_condition_urls", "after_reno_urls"):
            all_urls += [u for u in (pricing.get(url_field) or [])
                         if isinstance(u, str) and u.startswith("http")]
        # deduplicate ไม่เปลี่ยนลำดับ
        listing_urls = list(dict.fromkeys(all_urls))

        enrichment: dict[str, Any] = {
            "ai_analysis":     analysis,
            "ai_analyzed_at":  datetime.now(timezone.utc).isoformat(),
            "roi_data_source": "sonar_pro",
        }
        if listing_urls:
            enrichment["source_urls"] = listing_urls

        area_sqm  = deal.get("area_sqm") or deal.get("land_area_sqm") or 0
        buy_price = deal.get("buy_price") or deal.get("price") or 0

        # ── ราคา 3 ระดับ — ดึงจาก pricing block ────────────────────────
        def _parse_price(v) -> Optional[int]:
            """ตรวจสอบและแปลงราคา → int (ต้องอยู่ในช่วง 50,000–500,000,000 บาท)"""
            try:
                f = float(v)
                return int(f) if 50_000 <= f <= 500_000_000 else None
            except (TypeError, ValueError):
                return None

        orig_low   = _parse_price(pricing.get("original_low"))
        orig_high  = _parse_price(pricing.get("original_high"))
        good_low   = _parse_price(pricing.get("good_condition_low"))
        good_high  = _parse_price(pricing.get("good_condition_high"))
        reno_low   = _parse_price(pricing.get("after_reno_low"))
        reno_high  = _parse_price(pricing.get("after_reno_high"))
        rental     = _parse_price(pricing.get("rental_monthly"))

        # บันทึก 3 ระดับราคาลง enrichment (สำหรับแสดงผลใน dashboard)
        if orig_low:  enrichment["price_original_low"]  = orig_low
        if orig_high: enrichment["price_original_high"] = orig_high
        if good_low:  enrichment["price_good_low"]      = good_low
        if good_high: enrichment["price_good_high"]     = good_high
        if reno_low:  enrichment["price_reno_low"]      = reno_low
        if reno_high: enrichment["price_reno_high"]     = reno_high
        if rental:    enrichment["rental_monthly_est"]  = rental

        # บันทึกข้อมูลโครงการ
        if proj_info.get("official_name"):
            enrichment["project_official_name"] = proj_info["official_name"]
        if proj_info.get("developer"):
            enrichment["project_developer"] = proj_info["developer"]
        if proj_info.get("exact_address"):
            enrichment["project_address"] = proj_info["exact_address"]

        # ── ราคา after_reno ใช้คำนวณ ROI (เป้าหมายราคาขาย) ──────────
        # ถ้าไม่มี after_reno ให้ fallback ไป good_condition
        market_value_min = reno_low or good_low
        market_value_max = reno_high or good_high or market_value_min

        if market_value_min:
            # ตรวจสอบความสมเหตุสมผล: ต้องไม่ต่ำกว่าราคาซื้อ × 0.4 (ปรับจาก 0.5 — กัน edge case NPA ลดราคามาก)
            if buy_price > 0 and market_value_min < buy_price * 0.4:
                logger.warning(
                    f"Price sanity fail deal {deal.get('id','?')}: "
                    f"after_reno {market_value_min:,.0f} < buy_price*0.4 — clearing"
                )
                market_value_min = market_value_max = None
            # ลบ sanity check orig_high*0.8 ออก — aggressive เกินไป ทำให้ ROI = null บ่อยๆ
            # Sonar บางครั้ง return after_reno ต่ำกว่า orig_high เพราะ listing แต่ละตัวต่างกัน

        if market_value_min and market_value_max:
            val_mid = int((market_value_min + market_value_max) / 2)
            enrichment["market_value_min"] = market_value_min
            enrichment["market_value_max"] = market_value_max
            enrichment["market_value"]     = val_mid   # backward compat

            # คำนวณ ฿/ตร.ม. ถ้ามี usable_sqm
            usable_sqm = area_sqm
            if proj_info.get("usable_sqm_low"):
                try: usable_sqm = float(proj_info["usable_sqm_low"])
                except: pass
            if usable_sqm > 0:
                enrichment["market_price_sqm_min"] = int(market_value_min / usable_sqm)
                enrichment["market_price_sqm_max"] = int(market_value_max / usable_sqm)
                enrichment["market_price_sqm"]     = int(val_mid / usable_sqm)
        else:
            market_value_min = market_value_max = None

        # ── คำนวณ ROI ด้วยช่วงราคาจริงจาก Sonar Pro ──────────────────
        def _calc_roi(market_val: int) -> tuple[float, float, float]:
            """Return (reno_total, total_cost, roi_pct) for a given market value."""
            reno_total   = area_sqm * 5_000
            transfer_fee = buy_price * 0.055
            total_cost   = buy_price + reno_total + transfer_fee
            profit       = market_val - total_cost
            roi_pct      = (profit / total_cost) * 100 if total_cost > 0 else 0
            return reno_total, total_cost, roi_pct

        if market_value_min is not None and buy_price > 0 and area_sqm > 0:
            reno_total, total_cost, roi_min = _calc_roi(market_value_min)
            _,          _,          roi_max = _calc_roi(market_value_max)
            roi_mid = (roi_min + roi_max) / 2

            enrichment["reno_cost_total"] = round(reno_total)
            enrichment["reno_cost_sqm"]   = 5_000
            enrichment["transfer_fee"]    = round(buy_price * 0.055)
            enrichment["total_cost"]      = round(total_cost)

            # ── Sanity checks ──────────────────────────────────────────
            # 1. ราคาตลาดต่ำสุดต้องไม่น้อยกว่าราคาซื้อ × 0.7
            if market_value_min < buy_price * 0.7:
                logger.warning(
                    f"Sanity fail deal {deal.get('id','?')}: "
                    f"market_value_min {market_value_min:,.0f} < buy_price×0.7 — roi_valid=False"
                )
                enrichment["roi_valid"]   = False
                enrichment["roi_percent"] = round(roi_min, 2)
                enrichment["roi_min"]     = round(roi_min, 2)
                enrichment["roi_max"]     = round(roi_max, 2)
                enrichment["roi_flag"]    = "⚠️ ข้อมูลผิดปกติ"
                enrichment["priority"]    = "SKIP"

            # 2. ROI สูงผิดปกติ (>120%) — Sonar อาจส่ง ฿/ตร.ม. มาเป็นราคารวม
            #    NPA flip ปกติไม่เกิน 80-100% — threshold 120% ดักได้ดีกว่า 200%
            elif roi_max > 120:
                logger.warning(
                    f"Sanity fail deal {deal.get('id','?')}: "
                    f"roi_max {roi_max:.1f}% > 120% — likely unit confusion — roi_valid=False"
                )
                enrichment["roi_valid"]   = False
                enrichment["roi_percent"] = round(roi_mid, 2)
                enrichment["roi_min"]     = round(roi_min, 2)
                enrichment["roi_max"]     = round(roi_max, 2)
                enrichment["roi_flag"]    = "⚠️ ROI เกินจริง (ตรวจสอบหน่วย)"
                enrichment["priority"]    = "SKIP"

            else:
                profit_min = market_value_min - total_cost
                profit_max = market_value_max - total_cost
                enrichment["estimated_profit"]     = round(profit_min)   # conservative
                enrichment["estimated_profit_max"] = round(profit_max)
                enrichment["roi_percent"]          = round(roi_min, 2)   # conservative
                enrichment["roi_min"]              = round(roi_min, 2)
                enrichment["roi_max"]              = round(roi_max, 2)
                enrichment["roi_valid"]            = True

                # ตัดสินบน roi_min (conservative) เพื่อกันสัญญาณ false positive
                if roi_min >= 30:
                    enrichment["roi_flag"] = "🟢 ควรซื้อ"
                    enrichment["priority"] = "HIGH"
                elif roi_min >= 15:
                    enrichment["roi_flag"] = "🟡 พิจารณา"
                    enrichment["priority"] = "MEDIUM"
                else:
                    enrichment["roi_flag"] = "🔴 ข้ามไป"
                    enrichment["priority"] = "LOW"

        logger.info(
            f"✅ Sonar Pro analyzed deal {deal.get('id','?')} — "
            f"orig ฿{enrichment.get('price_original_low',0):,.0f}"
            f"~{enrichment.get('price_original_high',0):,.0f} | "
            f"reno ฿{enrichment.get('price_reno_low',0):,.0f}"
            f"~฿{enrichment.get('price_reno_high',0):,.0f} | "
            f"ROI {enrichment.get('roi_min',0):.1f}%~{enrichment.get('roi_max',0):.1f}% | "
            f"urls={len(listing_urls)}"
        )

        # ── Save to market pattern cache for future use ─────────────────
        if cache and enrichment.get("market_value"):
            try:
                await cache.save_pattern(deal, enrichment)
            except Exception as ce:
                logger.warning(f"Cache save failed (non-fatal): {ce}")

        return enrichment

    async def analyze_batch(
        self,
        deals: list[dict],
        skip_analyzed: bool = True,
    ) -> dict[Any, dict]:
        """
        วิเคราะห์หลาย deals — skip ที่ analyze แล้ว

        Returns:
            {deal_id: enrichment_dict} สำหรับ deals ที่ประมวลผลสำเร็จ
        """
        results: dict[Any, dict] = {}

        for deal in deals:
            deal_id = deal.get("id")

            # Skip analyzed deals
            if skip_analyzed and deal.get("ai_analyzed_at"):
                continue

            enrichment = await self.analyze_deal(deal)
            if enrichment and deal_id:
                results[deal_id] = enrichment

        logger.info(f"Batch analysis: {len(results)}/{len(deals)} deals enriched")
        return results

    async def _rate_limit(self):
        """ป้องกัน rate limit โดย enforce minimum interval ระหว่าง calls."""
        import time
        now = time.time()
        wait = self._min_interval - (now - self._last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_ts = time.time()
