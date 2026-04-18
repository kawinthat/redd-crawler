"""
app.py — RE:DD FastAPI Server
Endpoints:
  GET  /health            — health check
  POST /scan              — trigger full crawl (background)
  GET  /scan/status       — current scan progress
  GET  /deals             — list all deals (paginated)
  GET  /deals/hot         — HOT deals (ROI > 30%)
  GET  /deals/stats       — summary stats
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

load_dotenv()

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────

app = FastAPI(
    title="RE:DD Autonomous Real Estate Scanner",
    description="Crawls Thai NPA + enforcement sites, calculates ROI, surfaces HOT DEALS",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # ล็อค origin ตอน production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

_scan_state: dict = {
    "status":      "idle",    # idle | running | done | failed
    "started_at":  None,
    "finished_at": None,
    "stats":       {},
    "error":       None,
}

_scan_lock = asyncio.Lock()


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────

NPA_SITES = [
    # ── ธนาคารกรุงไทย (Krungthai) — REST API ✅
    "https://npa.krungthai.com",
    # ── ธ.อาคารสงเคราะห์ (GH Bank) — HTML scraper ✅
    "https://www.ghbhomecenter.com",
    # ── BAM บริหารสินทรัพย์กรุงเทพ — REST API ✅ (~16k assets)
    "https://www.bam.co.th/npa",
    # ── ธนาคารกรุงศรี (Krungsri) — HTML scraper ✅ (~1.7k assets)
    "https://www.krungsriproperty.com/search-result",
    # ── ธนาคารออมสิน (GSB) — SSR scraper ✅ (~3k+ assets)
    "https://npa-assets.gsb.or.th",
    # ── SCB Asset (ไทยพาณิชย์) — REST API ✅ (~4k assets)
    "https://asset.home.scb/project",
    # ── กรมบังคับคดี — Playwright form (เลือก จ.กทม/นนทบุรี/ปทุมธานี + CAPTCHA อัตโนมัติ)
    "https://asset.led.go.th/newbidreg/default.asp",
    # ── SAM (บสก.) — HTML scraper
    "https://sam.or.th/site/npa/page_list.php?s_product_type=&s_province=&s_district=&s_status_id=&key_search=",
    # ── KKP Propify — HTML scraper
    "https://kkppropify.kkpfg.com/th/npa",
    # ── Krungsri Home (ทรัพย์บ้าน)
    "https://www.krungsriproperty.com/home",
]

# โปรโมชั่น / ราคาพิเศษ — ราคาลด ลดพิเศษจากธนาคาร
PROMO_SITES = [
    "https://www.krungsriproperty.com/investment_th",
    "https://www.kasikornbank.com/th/propertyforsale/search/pages/index.aspx?tabname=PromotionPropertie",
    "https://property.pamco.co.th/assets",
    "https://www.ghbhomecenter.com/promotions",
]

# ราคาตลาดอ้างอิง — ใช้ compare ROI กับ NPA
MARKET_SITES = [
    "https://www.ddproperty.com",
    "https://www.hipflat.co.th",
    "https://www.baania.com",
]

UNLIMITED = 999_999  # ไม่จำกัด — หยุดเมื่อเว็บไม่มีหน้าต่อไปเอง


class ScanRequest(BaseModel):
    url: str          = os.getenv("TARGET_URL", "")
    urls: list[str]   = []
    max_pages: int    = UNLIMITED   # ไม่จำกัดหน้า
    max_listings: int = UNLIMITED   # ไม่จำกัดจำนวน listing
    concurrency: int  = int(os.getenv("CONCURRENCY", "3"))
    dry_run: bool     = False


# ─────────────────────────────────────────────
# BACKGROUND SCAN
# ─────────────────────────────────────────────

async def _run_scan(req: ScanRequest):
    from crawler.orchestrator import AutonomousCrawler
    from crawler.spider import CrawlConfig

    global _scan_state
    _scan_state["status"]     = "running"
    _scan_state["started_at"] = datetime.now(timezone.utc).isoformat()
    _scan_state["stats"]      = {}
    _scan_state["error"]      = None

    # Determine which URLs to scan
    if req.urls:
        target_urls = req.urls
    elif req.url:
        target_urls = [req.url]
    else:
        # Full scan: NPA + Promotion sites
        target_urls = NPA_SITES + PROMO_SITES

    try:
        # สร้าง combined_stats ก่อน แล้วส่งเป็น live_stats ให้ crawler
        # crawler จะ update dict นี้ real-time ทุก deal ที่ save
        combined_stats = {"pages": 0, "scraped": 0, "saved": 0, "hot": 0,
                          "dedup_skipped": 0, "sites": [],
                          "total_sites": len(target_urls), "done_sites": 0}
        _scan_state["stats"] = combined_stats  # frontend อ่าน reference นี้ตลอดเวลา

        crawler = AutonomousCrawler(
            dry_run    = req.dry_run,
            line_token = os.getenv("LINE_NOTIFY_TOKEN"),
            live_stats = combined_stats,   # crawler update real-time ผ่าน reference เดียวกัน
        )

        for site_url in target_urls:
            logger.info(f"Scanning (unlimited): {site_url}")
            combined_stats["current_site"] = site_url

            config = CrawlConfig(
                base_url     = site_url,
                max_pages    = req.max_pages,
                max_listings = req.max_listings,
                concurrency  = req.concurrency,
                delay_min    = 0.5,
                delay_max    = 1.5,
            )
            try:
                stats = await crawler.run(site_url, config)
                # saved/hot/scraped ถูก update real-time แล้ว ไม่ต้อง += อีก
                # แต่ dedup_skipped ต้อง += เพราะ orchestrator reset ต่อ site
                combined_stats["dedup_skipped"] += stats.get("dedup_skipped", 0)
                combined_stats["done_sites"]    += 1
                combined_stats["sites"].append({"url": site_url, "status": "ok", **stats})
            except Exception as site_err:
                logger.error(f"Site {site_url} failed: {site_err}")
                combined_stats["done_sites"] += 1
                combined_stats["sites"].append({"url": site_url, "status": "error", "error": str(site_err)})

        _scan_state["stats"]       = combined_stats
        _scan_state["status"]      = "done"
        _scan_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        logger.error(f"Scan failed: {e}")
        _scan_state["status"] = "failed"
        _scan_state["error"]  = str(e)
        _scan_state["finished_at"] = datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# SUPABASE CLIENT
# ─────────────────────────────────────────────

def _get_supabase():
    from supabase import create_client
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise HTTPException(503, "Supabase not configured")
    return create_client(url, key)


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "redd-crawler", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/test_db")
async def test_db():
    """ทดสอบ Supabase write โดยตรง — ใช้สำหรับ debug เท่านั้น"""
    import traceback
    result = {"supabase_url_set": bool(os.getenv("SUPABASE_URL")),
              "supabase_key_set": bool(os.getenv("SUPABASE_KEY"))}
    try:
        db = _get_supabase()
        # ทดสอบ read
        read_r = db.table("deals").select("id").limit(1).execute()
        result["read_ok"] = True
        result["existing_rows"] = len(read_r.data) if read_r.data else 0

        # ทดสอบ write
        test_deal = {
            "listing_url": "__test_deal_delete_me__",
            "source_domain": "test",
            "source_type": "bank_npa",
            "property_type": "condo",
            "location": "กรุงเทพ",
            "price": 1000000,
            "area_sqm": 30.0,
            "condition": "good",
            "roi_valid": False,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        write_r = db.table("deals").upsert(test_deal, on_conflict="listing_url").execute()
        result["write_ok"] = True
        result["write_data"] = str(write_r.data)[:200] if write_r.data else "no data returned"

        # ลบ test record
        db.table("deals").delete().eq("listing_url", "__test_deal_delete_me__").execute()
        result["cleanup_ok"] = True

    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-500:]
    return result


@app.post("/scan")
async def trigger_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    """Trigger a full crawl in the background. Returns immediately."""
    if _scan_state["status"] == "running":
        raise HTTPException(409, "Scan already running")

    target_urls = req.urls or ([req.url] if req.url else NPA_SITES + PROMO_SITES)
    background_tasks.add_task(_run_scan, req)
    return {
        "message":      "Scan started",
        "status":       "started",
        "urls":         target_urls,
        "url":          target_urls[0] if target_urls else "",
        "dry_run":      req.dry_run,
        "max_listings": req.max_listings,
        "sites_count":  len(target_urls),
    }


@app.get("/scan/status")
def scan_status():
    """Return current scan state + progress stats."""
    return _scan_state


@app.get("/deals")
def list_deals(
    page: int       = Query(1, ge=1),
    per_page: int   = Query(200, ge=1, le=500),
    priority: Optional[str] = Query(None, description="HIGH | MEDIUM | LOW"),
    source: Optional[str]   = Query(None),
):
    """List deals with optional filters, paginated."""
    db = _get_supabase()
    offset = (page - 1) * per_page

    q = db.table("deals").select("*")
    if priority:
        q = q.eq("priority", priority.upper())
    if source:
        q = q.eq("source_domain", source)

    result = (
        q.order("scraped_at", desc=True)
         .range(offset, offset + per_page - 1)
         .execute()
    )

    count_q = db.table("deals").select("id", count="exact")
    if priority:
        count_q = count_q.eq("priority", priority.upper())
    if source:
        count_q = count_q.eq("source_domain", source)
    total = count_q.execute().count or 0

    return {
        "data":      result.data,
        "total":     total,
        "page":      page,
        "per_page":  per_page,
        "pages":     (total + per_page - 1) // per_page,
    }


@app.get("/deals/hot")
def hot_deals(limit: int = Query(200, ge=1, le=5000)):
    """Return HOT deals (ROI > 30%) sorted by ROI descending."""
    db = _get_supabase()
    result = (
        db.table("hot_deals")
          .select("*")
          .order("roi_percent", desc=True)
          .limit(limit)
          .execute()
    )
    return {"data": result.data, "count": len(result.data)}


@app.post("/analyze")
async def trigger_analysis(
    background_tasks: BackgroundTasks,
    limit: int = Query(50, ge=1, le=500, description="จำนวน deals สูงสุดที่ analyze ต่อ batch"),
    hot_only: bool = Query(False, description="วิเคราะห์เฉพาะ HOT deals (ROI ≥ 30%)"),
    provinces: str = Query(None, description="จังหวัดที่ต้องการวิเคราะห์ คั่นด้วยคอมม่า เช่น กรุงเทพมหานคร,นนทบุรี"),
):
    """
    เรียก Perplexity Sonar Pro วิเคราะห์ deals ที่ยังไม่ analyze (ai_analyzed_at IS NULL)

    Token Efficiency:
    - ตรวจ market pattern cache ก่อน — cache hit ไม่เสีย token เลย
    - ใช้ sonar-pro สำหรับทุก deal ที่ไม่มีใน cache
    - Skip deals ที่ analyze แล้ว อัตโนมัติ
    - ระบุ provinces เพื่อจำกัดขอบเขต ประหยัด token
    """
    perplexity_key = os.getenv("OPENROUTER_API_KEY", "")
    if not perplexity_key or not perplexity_key.startswith("sk-or-"):
        raise HTTPException(503, "OPENROUTER_API_KEY ยังไม่ได้ตั้งค่า หรือไม่ถูกต้อง")

    prov_list = [p.strip() for p in provinces.split(",")] if provinces else []
    background_tasks.add_task(_run_analysis, limit, hot_only, prov_list)
    return {
        "message": "Analysis started",
        "limit": limit,
        "hot_only": hot_only,
        "provinces": prov_list or "ทุกจังหวัด",
    }


async def _run_analysis(limit: int, hot_only: bool, provinces: list[str] | None = None):
    """Background task: fetch pending deals → market cache / Perplexity analyze → save back."""
    from crawler.perplexity_analyzer import PerplexityAnalyzer

    db = _get_supabase()
    analyzer = PerplexityAnalyzer()

    try:
        # ดึง deals ที่ยังไม่ analyze
        q = db.table("deals").select(
            "id,listing_url,source_domain,property_type,project_name,"
            "location,price,area_sqm,land_area_sqm,roi_percent,priority,condition,ai_analyzed_at"
        ).is_("ai_analyzed_at", "null")

        if hot_only:
            q = q.gte("roi_percent", 30)

        # ดึงมามากกว่า limit เพื่อ post-filter จังหวัด
        fetch_limit = limit * 5 if provinces else limit
        all_pending = q.order("scraped_at", desc=True).limit(fetch_limit).execute().data or []

        # กรองจังหวัด (post-filter เพราะ location เป็น free-text)
        if provinces:
            def _match_province(loc: str) -> bool:
                loc = loc or ""
                return any(p in loc for p in provinces)
            pending = [d for d in all_pending if _match_province(d.get("location", ""))][:limit]
            logger.info(f"Province filter {provinces}: {len(all_pending)} → {len(pending)} deals")
        else:
            pending = all_pending[:limit]

        logger.info(f"Analyze batch: {len(pending)} pending deals | provinces={provinces or 'all'}")

        enriched = 0
        for deal in pending:
            result = await analyzer.analyze_deal(deal)
            if result:
                deal_id = deal["id"]
                update_data = {k: v for k, v in result.items()
                               if k not in ("ai_analysis",)}
                # Save ai_analysis as JSONB
                update_data["ai_analysis"] = result.get("ai_analysis")
                update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
                db.table("deals").update(update_data).eq("id", deal_id).execute()
                enriched += 1

        logger.success(f"Analysis done: {enriched}/{len(pending)} deals enriched")

    except Exception as e:
        logger.error(f"Analysis batch failed: {e}")


@app.get("/deals/stats")
def deal_stats():
    """Summary stats: total deals, HOT count, avg ROI, by source."""
    db = _get_supabase()

    total  = db.table("deals").select("id", count="exact").execute().count or 0
    hot    = db.table("deals").select("id", count="exact").eq("priority", "HIGH").execute().count or 0
    medium = db.table("deals").select("id", count="exact").eq("priority", "MEDIUM").execute().count or 0
    by_src = db.table("deals_by_source").select("*").execute()

    return {
        "total_deals":   total,
        "hot_deals":     hot,
        "medium_deals":  medium,
        "by_source":     by_src.data,
        "last_scan":     _scan_state.get("finished_at"),
        "scan_status":   _scan_state.get("status"),
    }
