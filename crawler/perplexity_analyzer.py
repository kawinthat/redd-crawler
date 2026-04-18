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
# sonar-pro มี real-time web search → ค้นหาราคาตลาดจริงจาก DDProperty, Hipflat, Baania
PROMPT_TEMPLATE = """\
ค้นหาราคาตลาดอสังหาริมทรัพย์จริงจากเว็บไซต์ DDProperty, Hipflat, Baania \
หรือแหล่งข้อมูลอสังหาริมทรัพย์ที่น่าเชื่อถือในไทย ณ ปี 2025-2026

ทรัพย์ที่ต้องการข้อมูล:
ประเภท: {type_th}
โครงการ/หมู่บ้าน: {project_name}
พื้นที่: {area}
จังหวัด: {province}
เขต/อำเภอ: {district}
สภาพ: {condition_th}

กฎเหล็ก:
1. ห้ามประมาณเอง — ต้องใช้ข้อมูลราคาตลาดจริงที่ค้นเจอเท่านั้น
2. ถ้าค้นไม่เจอราคาจริงสำหรับทำเล/ประเภทนี้ → ใส่ null
3. ตอบ JSON เท่านั้น ห้ามมีข้อความอื่น ห้าม markdown

JSON format (หน่วย: บาท และ บาท/ตร.ม.):
{{
  "market_price_sqm_before_reno": null,
  "market_price_sqm_after_reno": null,
  "market_value_before_reno": null,
  "market_value_after_reno": null,
  "data_sources": [],
  "comparable_projects": [],
  "target_buyers": [],
  "summary_th": null
}}

คำอธิบาย fields (สำคัญมาก — ต้องกรอกทุก field):
- market_price_sqm_before_reno: ราคาตลาด ฿/ตร.ม. สภาพเดิม ไม่รีโนเวท (ประเภทและทำเลเดียวกัน)
- market_price_sqm_after_reno: ราคาตลาด ฿/ตร.ม. สภาพดี หลังรีโนเวทแล้ว
- market_value_before_reno: มูลค่ารวมสภาพเดิม = market_price_sqm_before_reno × {area_sqm:.0f} ตร.ม.
- market_value_after_reno: มูลค่ารวมหลังรีโนเวท = market_price_sqm_after_reno × {area_sqm:.0f} ตร.ม.
- data_sources: รายการ URL หรือชื่อเว็บที่ใช้อ้างอิงราคาตลาดจริง (เช่น ["https://www.ddproperty.com/...", "hipflat.co.th"])
- comparable_projects: รายชื่อโครงการใกล้เคียงที่นำมาเปรียบเทียบราคา (เช่น ["The Trust Ratchada", "ลลิล Lumpini"])
- target_buyers: กลุ่มลูกค้าเป้าหมาย (เช่น ["นักลงทุนปล่อยเช่า", "ครอบครัว", "นักลงทุน flip"])
- summary_th: สรุปภาษาไทย 2-3 ประโยค ครอบคลุม: (1) ราคาตลาดที่ค้นเจอ (2) ศักยภาพการลงทุน (3) ข้อควรระวัง — ห้ามเว้นว่าง ต้องกรอกเสมอ"""

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
        area_sqm=area_sqm or 0,
        province=province,
        district=district,
        condition_th=condition_th,
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
                                    "คุณคือผู้เชี่ยวชาญอสังหาริมทรัพย์ไทย "
                                    "ค้นหาข้อมูลราคาตลาดจริงจากอินเทอร์เน็ต "
                                    "ตอบด้วย JSON ที่ถูกต้องเท่านั้น ห้ามมีข้อความอื่น "
                                    "ถ้าหาราคาจริงไม่เจอ ให้ใส่ null — ห้ามประมาณเอง"
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens":  600,
                        "temperature": 0.1,   # ต่ำมาก = ตอบตรง ไม่สร้างสรรค์
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
        # Extract source URLs from Sonar Pro response
        data_sources = analysis.get("data_sources") or []
        source_urls  = [s for s in data_sources if isinstance(s, str) and s.startswith("http")]

        enrichment: dict[str, Any] = {
            "ai_analysis":    analysis,
            "ai_analyzed_at": datetime.now(timezone.utc).isoformat(),
            "roi_data_source": "sonar_pro",
        }
        if source_urls:
            enrichment["source_urls"] = source_urls

        area_sqm = deal.get("area_sqm") or deal.get("land_area_sqm") or 0
        buy_price = deal.get("price") or 0

        # ── ราคาตลาดก่อนรีโนเวท (สภาพเดิม) ───────────────────────────
        mvbr = analysis.get("market_value_before_reno")
        psbr = analysis.get("market_price_sqm_before_reno")
        if mvbr and mvbr > 0:
            enrichment["market_value_before_reno"] = int(mvbr)
        elif psbr and psbr > 0 and area_sqm > 0:
            enrichment["market_value_before_reno"] = int(psbr * area_sqm)

        # ── ราคาตลาดหลังรีโนเวท (สภาพดี) ──────────────────────────────
        mvar = analysis.get("market_value_after_reno")
        psar = analysis.get("market_price_sqm_after_reno")
        if mvar and mvar > 0:
            market_value_after = int(mvar)
            enrichment["market_value"] = market_value_after
            if psar and psar > 0:
                enrichment["market_price_sqm"] = int(psar)
        elif psar and psar > 0 and area_sqm > 0:
            market_value_after = int(psar * area_sqm)
            enrichment["market_value"] = market_value_after
            enrichment["market_price_sqm"] = int(psar)
        else:
            market_value_after = None

        # ── คำนวณ ROI ใหม่ด้วยราคาจริงจาก Sonar Pro ──────────────────
        if market_value_after and buy_price > 0 and area_sqm > 0:
            reno_total   = area_sqm * 5_000      # flat 5,000 ฿/ตร.ม.
            transfer_fee = buy_price * 0.055     # 5.5% (โอน 2% + จดจำนอง 1% + อากร 0.5% + อื่นๆ)
            total_cost   = buy_price + reno_total + transfer_fee
            profit       = market_value_after - total_cost
            roi_pct      = (profit / total_cost) * 100

            # ── Sanity checks — กัน ROI หลอก ──────────────────────────
            # 1. ราคาตลาดต้องไม่ต่ำกว่าราคาซื้อ × 0.7 (ไม่มีใครซื้อสินทรัพย์ที่ขายได้แค่ 70% ของที่จ่ายไป)
            if market_value_after < buy_price * 0.7:
                logger.warning(
                    f"Sanity fail deal {deal.get('id','?')}: "
                    f"market_value_after {market_value_after:,.0f} < buy_price×0.7 {buy_price*0.7:,.0f} "
                    f"— roi_valid=False"
                )
                enrichment["roi_valid"] = False
                enrichment["roi_percent"] = round(roi_pct, 2)
                enrichment["roi_flag"] = "⚠️ ข้อมูลผิดปกติ"
                enrichment["priority"] = "SKIP"
                enrichment["total_cost"] = round(total_cost)
                enrichment["transfer_fee"] = round(transfer_fee)
                enrichment["reno_cost_total"] = round(reno_total)
                enrichment["reno_cost_sqm"] = 5_000
            # 2. ROI ที่เป็นไปไม่ได้ (>200%) — Sonar Pro อาจ confuse ราคา/ตร.ม. กับราคารวม
            elif roi_pct > 200:
                logger.warning(
                    f"Sanity fail deal {deal.get('id','?')}: "
                    f"ROI {roi_pct:.1f}% > 200%% threshold — likely Sonar Pro confused unit "
                    f"(market_value_after={market_value_after:,.0f}, area={area_sqm}) — roi_valid=False"
                )
                enrichment["roi_valid"] = False
                enrichment["roi_flag"] = "⚠️ ROI เกินจริง (ตรวจสอบหน่วย)"
                enrichment["priority"] = "SKIP"
                enrichment["total_cost"] = round(total_cost)
                enrichment["transfer_fee"] = round(transfer_fee)
                enrichment["reno_cost_total"] = round(reno_total)
                enrichment["reno_cost_sqm"] = 5_000
            else:
                enrichment["reno_cost_total"]  = round(reno_total)
                enrichment["reno_cost_sqm"]    = 5_000
                enrichment["transfer_fee"]     = round(transfer_fee)
                enrichment["total_cost"]       = round(total_cost)
                enrichment["estimated_profit"] = round(profit)
                enrichment["roi_percent"]      = round(roi_pct, 2)
                enrichment["roi_valid"]        = True

                if roi_pct >= 30:
                    enrichment["roi_flag"] = "🟢 ควรซื้อ"
                    enrichment["priority"] = "HIGH"
                elif roi_pct >= 15:
                    enrichment["roi_flag"] = "🟡 พิจารณา"
                    enrichment["priority"] = "MEDIUM"
                else:
                    enrichment["roi_flag"] = "🔴 ข้ามไป"
                    enrichment["priority"] = "LOW"

        logger.info(
            f"✅ Sonar Pro analyzed deal {deal.get('id','?')} — "
            f"before_reno ฿{enrichment.get('market_value_before_reno',0):,.0f} | "
            f"after_reno ฿{enrichment.get('market_value',0):,.0f} | "
            f"ROI {enrichment.get('roi_percent',0):.1f}% | "
            f"sources={len(enrichment.get('source_urls',[]))}"
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
