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
# เข้มงวด: ค้นเฉพาะเว็บฝากขายบ้านเท่านั้น + บังคับ URL จริง + ราคาเป็นช่วง low-high
PROMPT_TEMPLATE = """\
ค้นหาประกาศขายจริงของอสังหาริมทรัพย์ใกล้เคียง จากเว็บฝากขายบ้านต่อไปนี้เท่านั้น:
• ddproperty.com
• fazwaz.co.th
• propertyonlineplus.com
• baania.com
• livinginsider.com
• dotproperty.co.th
• kaidee.com

ทรัพย์เป้าหมาย (ต้องการเปรียบเทียบราคาขาย):
ประเภท: {type_th}
โครงการ/หมู่บ้าน: {project_name}
พื้นที่: {area}
จังหวัด: {province}
เขต/อำเภอ: {district}
สภาพเป้าหมาย: สภาพดี พร้อมอยู่ (เทียบเท่าหลังรีโนเวทแล้ว)

ขั้นตอน:
1. ค้นหาประกาศขายที่ใกล้เคียงที่สุด (ประเภท + ทำเล + ขนาด) จากเว็บในรายการข้างต้นเท่านั้น
2. คำนวณ ฿/ตร.ม. ของแต่ละประกาศที่เจอ
3. รายงานช่วงราคา: ต่ำสุด (low) และ สูงสุด (high) ฿/ตร.ม. จากประกาศจริงที่ค้นเจอ

กฎเหล็ก (ห้ามฝ่าฝืนเด็ดขาด):
1. ห้ามใช้แหล่งอื่นนอกจากเว็บฝากขายบ้านในรายการข้างต้น — ห้ามใช้ข่าว บทความ ราคาประเมิน ธปท. DDinsight หรือแหล่งอื่นใด
2. ห้ามประมาณหรือคาดเดาราคา — ถ้าค้นไม่เจอประกาศจริง ให้ใส่ null ทุก field
3. listing_urls ต้องเป็น URL ประกาศขายจริง ห้ามใส่ URL หน้าหลักหรือหน้าค้นหา
4. ตอบ JSON เท่านั้น ห้ามมีข้อความอื่น ห้าม markdown ห้าม backtick

JSON (หน่วย: บาท/ตร.ม.):
{{
  "listing_urls": [],
  "market_price_sqm_low": null,
  "market_price_sqm_high": null,
  "comparable_count": null,
  "comparable_projects": [],
  "target_buyers": [],
  "summary_th": null
}}

คำอธิบาย fields:
- listing_urls: URL ประกาศขายจริงที่ค้นเจอ (จากเว็บฝากขายเท่านั้น)
- market_price_sqm_low: ราคาต่ำสุด ฿/ตร.ม. จากประกาศที่ค้นเจอ (สภาพดี)
- market_price_sqm_high: ราคาสูงสุด ฿/ตร.ม. จากประกาศที่ค้นเจอ (สภาพดี)
- comparable_count: จำนวนประกาศที่ค้นเจอและใช้อ้างอิง
- comparable_projects: โครงการ/หมู่บ้านจากประกาศที่ค้นเจอ
- target_buyers: กลุ่มผู้ซื้อเป้าหมายที่เหมาะสม
- summary_th: สรุป 2-3 ประโยค: (1) ช่วงราคาที่ค้นเจอจากเว็บใด (2) ศักยภาพการลงทุน (3) ข้อควรระวัง"""

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
    """Build a Perplexity Sonar Pro prompt from a deal dict."""
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

    return PROMPT_TEMPLATE.format(
        type_th=type_th,
        project_name=project,
        area=area_str,
        province=province,
        district=district,
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
                                    "คุณคือผู้ค้นหาราคาอสังหาริมทรัพย์จากเว็บประกาศขายบ้านในไทย "
                                    "ค้นหาจาก ddproperty.com fazwaz.co.th baania.com livinginsider.com "
                                    "dotproperty.co.th propertyonlineplus.com kaidee.com เท่านั้น "
                                    "ห้ามใช้แหล่งข้อมูลอื่น ห้ามประมาณราคาเอง "
                                    "ตอบด้วย JSON ที่ถูกต้องเท่านั้น ห้ามมีข้อความอื่น ห้าม backtick "
                                    "ถ้าค้นไม่เจอประกาศจริง ให้ใส่ null ทุก field"
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens":  800,    # เพิ่มจาก 600 เพราะ listing_urls ใช้ token
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
        # Extract real listing URLs from response
        listing_urls = [
            u for u in (analysis.get("listing_urls") or [])
            if isinstance(u, str) and u.startswith("http")
        ]

        enrichment: dict[str, Any] = {
            "ai_analysis":     analysis,
            "ai_analyzed_at":  datetime.now(timezone.utc).isoformat(),
            "roi_data_source": "sonar_pro",
        }
        if listing_urls:
            enrichment["source_urls"] = listing_urls

        area_sqm  = deal.get("area_sqm") or deal.get("land_area_sqm") or 0
        buy_price = deal.get("buy_price") or deal.get("price") or 0

        # ── ราคาตลาดหลังรีโนเวท (สภาพดี) — เป็นช่วง low/high ──────────
        sqm_low  = analysis.get("market_price_sqm_low")
        sqm_high = analysis.get("market_price_sqm_high")

        # Validate: ต้องเป็นตัวเลข > 0 และสมเหตุสมผล (ไม่น้อยกว่า 5,000 หรือเกิน 500,000 ฿/ตร.ม.)
        def _valid_sqm(v) -> bool:
            try:
                f = float(v)
                return 5_000 <= f <= 500_000
            except (TypeError, ValueError):
                return False

        has_low  = sqm_low  is not None and _valid_sqm(sqm_low)
        has_high = sqm_high is not None and _valid_sqm(sqm_high)

        if has_low and has_high and area_sqm > 0:
            sqm_low_f  = float(sqm_low)
            sqm_high_f = float(sqm_high)
            # Normalize: low ต้องไม่สูงกว่า high
            if sqm_low_f > sqm_high_f:
                sqm_low_f, sqm_high_f = sqm_high_f, sqm_low_f

            val_min = int(sqm_low_f  * area_sqm)
            val_max = int(sqm_high_f * area_sqm)
            val_mid = int((val_min + val_max) / 2)   # midpoint ใช้เป็น market_value หลัก

            enrichment["market_price_sqm_min"] = int(sqm_low_f)
            enrichment["market_price_sqm_max"] = int(sqm_high_f)
            enrichment["market_price_sqm"]     = int((sqm_low_f + sqm_high_f) / 2)
            enrichment["market_value_min"]     = val_min
            enrichment["market_value_max"]     = val_max
            enrichment["market_value"]         = val_mid   # backward compat
            market_value_min = val_min
            market_value_max = val_max
        elif has_low and area_sqm > 0:
            # มีแค่ low — ใช้ low เป็นทั้ง min และ max
            sqm_low_f = float(sqm_low)
            val_min   = int(sqm_low_f * area_sqm)
            enrichment["market_price_sqm_min"] = int(sqm_low_f)
            enrichment["market_price_sqm_max"] = int(sqm_low_f)
            enrichment["market_price_sqm"]     = int(sqm_low_f)
            enrichment["market_value_min"]     = val_min
            enrichment["market_value_max"]     = val_min
            enrichment["market_value"]         = val_min
            market_value_min = market_value_max = val_min
        elif has_high and area_sqm > 0:
            # มีแค่ high — ใช้ high เป็นทั้ง min และ max
            sqm_high_f = float(sqm_high)
            val_max    = int(sqm_high_f * area_sqm)
            enrichment["market_price_sqm_min"] = int(sqm_high_f)
            enrichment["market_price_sqm_max"] = int(sqm_high_f)
            enrichment["market_price_sqm"]     = int(sqm_high_f)
            enrichment["market_value_min"]     = val_max
            enrichment["market_value_max"]     = val_max
            enrichment["market_value"]         = val_max
            market_value_min = market_value_max = val_max
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

            # 2. ROI สูงผิดปกติ (>200%) — Sonar อาจส่ง ฿/ตร.ม. มาเป็นราคารวม
            elif roi_max > 200:
                logger.warning(
                    f"Sanity fail deal {deal.get('id','?')}: "
                    f"roi_max {roi_max:.1f}% > 200% — likely unit confusion — roi_valid=False"
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
            f"฿/ตร.ม. {enrichment.get('market_price_sqm_min',0):,.0f}"
            f"~{enrichment.get('market_price_sqm_max',0):,.0f} | "
            f"value ฿{enrichment.get('market_value_min',0):,.0f}"
            f"~฿{enrichment.get('market_value_max',0):,.0f} | "
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
