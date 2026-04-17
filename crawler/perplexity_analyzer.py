"""
perplexity_analyzer.py — RE:DD AI Market Intelligence Engine
ใช้ Perplexity Sonar ผ่าน OpenRouter API วิเคราะห์แต่ละทรัพย์:
  - ราคาตลาดก่อน/หลังรีโนเวท
  - ค่าประมาณรีโนเวท
  - กลุ่มลูกค้าเป้าหมาย (อาชีพ, รายได้, ทำเล)
  - ROI คาดการณ์

ใช้ OpenRouter (OPENROUTER_API_KEY) เพื่อเข้าถึง Perplexity Sonar
ที่มี real-time web search — ไม่ต้องสมัคร Perplexity account แยก

Token Efficiency Strategy:
  1. ขอ JSON output เท่านั้น — ประหยัด ~60% tokens
  2. perplexity/sonar สำหรับ batch (ถูก), sonar-pro เฉพาะ HOT deals
  3. Skip deals ที่ analyze แล้ว
  4. Rate limit: 10 req/min
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from loguru import logger

# ── OpenRouter endpoint (รองรับ Perplexity Sonar + Claude + Llama ฯลฯ) ──────
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# ── Models via OpenRouter ────────────────────────────────
MODEL_STANDARD = "perplexity/sonar"      # มี web search, ราคาถูก
MODEL_PRO       = "perplexity/sonar-pro" # แม่นกว่า, เฉพาะ HOT deals

# ── Prompt Template (token-efficient JSON-only) ──────────
PROMPT_TEMPLATE = """\
วิเคราะห์ทรัพย์อสังหาริมทรัพย์ไทยต่อไปนี้ ตอบ JSON เท่านั้น ห้ามมีข้อความอื่น:

ประเภท: {type_th}
โครงการ/หมู่บ้าน: {project_name}
พื้นที่: {area}
จังหวัด: {province}
เขต/อำเภอ: {district}

JSON format (ตัวเลขหน่วยบาท):
{{
  "price_before_reno": {{"min": 0, "max": 0}},
  "price_after_reno": {{"min": 0, "max": 0}},
  "reno_cost": {{"min": 0, "max": 0}},
  "profit_potential": {{"min": 0, "max": 0}},
  "roi_percent": 0,
  "target_income_monthly": {{"min": 0, "max": 0}},
  "target_occupations": [],
  "work_areas": [],
  "summary_th": ""
}}"""

TYPE_TH_MAP = {
    "house": "บ้านเดี่ยว", "townhouse": "ทาวน์เฮ้าส์",
    "condo": "คอนโด",      "land": "ที่ดินเปล่า",
    "commercial": "อาคารพาณิชย์", "other": "ทรัพย์",
}


def _build_prompt(deal: dict) -> str:
    """Build a token-efficient Perplexity prompt from a deal dict."""
    loc = deal.get("location") or ""
    # "สงขลา อำเภอสะเดา" → province="สงขลา", district="อำเภอสะเดา"
    import re
    m = re.search(r"^([ก-๙a-zA-Z]+)\s+((?:อำเภอ|เขต)[ก-๙a-zA-Z ]+)", loc)
    province = m.group(1) if m else loc
    district = m.group(2) if m else "-"

    area_sqm = deal.get("area_sqm") or deal.get("land_area_sqm")
    area_str = f"{area_sqm:.1f} ตร.ม." if area_sqm else "ไม่ระบุ"

    type_th = TYPE_TH_MAP.get(deal.get("property_type", "other"), "ทรัพย์")
    project  = deal.get("project_name") or "-"

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
        rate_limit_per_min: int = 10,
        use_pro_for_hot: bool = True,
    ):
        # ใช้ OPENROUTER_API_KEY (มีอยู่แล้ว) — ไม่ต้องสมัคร Perplexity แยก
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        self._min_interval = 60.0 / rate_limit_per_min
        self._last_call_ts = 0.0
        self.use_pro_for_hot = use_pro_for_hot

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

        # Rate limiting
        await self._rate_limit()

        prompt = _build_prompt(deal)
        is_hot = (deal.get("roi_percent") or 0) >= 30
        model  = MODEL_PRO if (is_hot and self.use_pro_for_hot) else MODEL_STANDARD

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    OPENROUTER_API_URL,
                    headers={
                        "Authorization":  f"Bearer {self.api_key}",
                        "Content-Type":   "application/json",
                        "HTTP-Referer":   "https://redd-crawler.onrender.com",
                        "X-Title":        "RE:DD Real Estate Analyzer",
                    },
                    json={
                        "model":    model,
                        "messages": [
                            {"role": "system", "content":
                             "You are a Thai real estate expert. Respond with valid JSON only, no other text."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 450,
                        "temperature": 0.2,
                    },
                )
                resp.raise_for_status()
                raw_content = resp.json()["choices"][0]["message"]["content"].strip()

        except Exception as e:
            logger.error(f"Perplexity API error for deal {deal.get('id','?')}: {e}")
            return None

        # Parse JSON response
        try:
            # Strip markdown code fences if present
            if raw_content.startswith("```"):
                raw_content = raw_content.split("```")[1]
                if raw_content.startswith("json"):
                    raw_content = raw_content[4:]
            analysis: dict[str, Any] = json.loads(raw_content)
        except json.JSONDecodeError as e:
            logger.warning(f"Perplexity JSON parse error deal {deal.get('id','?')}: {e}")
            logger.debug(f"Raw content: {raw_content[:200]}")
            return None

        # Map analysis fields → Supabase deal columns
        enrichment: dict[str, Any] = {
            "ai_analysis":    analysis,
            "ai_analyzed_at": datetime.now(timezone.utc).isoformat(),
        }

        # market_value → ใช้ price_after_reno (mid-point)
        par = analysis.get("price_after_reno", {})
        if par.get("min") and par.get("max"):
            enrichment["market_value"] = int((par["min"] + par["max"]) / 2)

        # reno_cost_total → mid-point of reno_cost
        rc = analysis.get("reno_cost", {})
        if rc.get("min") and rc.get("max"):
            enrichment["reno_cost_total"] = int((rc["min"] + rc["max"]) / 2)

        # estimated_profit
        pp = analysis.get("profit_potential", {})
        if pp.get("min") and pp.get("max"):
            enrichment["estimated_profit"] = int((pp["min"] + pp["max"]) / 2)

        logger.info(
            f"✅ Analyzed deal {deal.get('id','?')} — "
            f"ROI {analysis.get('roi_percent',0):.1f}% | "
            f"profit ฿{enrichment.get('estimated_profit',0):,.0f}"
        )
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
