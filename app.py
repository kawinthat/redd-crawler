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

# ── Analyze progress state ─────────────────────
_analyze_state: dict = {
    "status":      "idle",   # idle | running | done | failed
    "started_at":  None,
    "finished_at": None,
    "total":       0,        # deals ทั้งหมดในชุดนี้
    "done":        0,        # ประมวลผลแล้ว (รวม success + error)
    "enriched":    0,        # Sonar Pro สำเร็จ
    "cached":      0,        # ได้ข้อมูลจาก market cache
    "failed_count":0,        # error รายตัว
    "current":     None,     # {"title":..., "location":...}
    "error":       None,
    "elapsed":     0,        # วินาที
    "eta":         None,     # วินาที
}

_analyze_lock = asyncio.Lock()


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
                          "total_sites": len(target_urls), "done_sites": 0,
                          "current_site": "", "progress": ""}
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


class AnalyzeUrlsBody(BaseModel):
    urls: list[str] = []
    force_reanalyze: bool = False


@app.get("/analyze/status")
def analyze_status_endpoint():
    """Return current analyze progress."""
    return _analyze_state


@app.post("/analyze")
async def trigger_analysis(
    background_tasks: BackgroundTasks,
    body: AnalyzeUrlsBody = None,
    limit: int     = Query(50,    ge=1,  le=500),
    hot_only: bool = Query(False),
    provinces: str = Query(None),
    types: str     = Query(None,  description="house,condo,townhouse,land,commercial"),
    price_min: int = Query(None),
    price_max: int = Query(None),
    ai_status: str = Query("all", description="all | unanalyzed | analyzed"),
    force: bool    = Query(False,  description="force re-analyze even if already done"),
):
    """
    เรียก Perplexity Sonar Pro วิเคราะห์ deals
    - body.urls: ถ้าส่งมา → analyze เฉพาะ URL เหล่านั้น (Selection mode)
    - force: True → analyze ซ้ำแม้มี ai_analyzed_at แล้ว
    """
    perplexity_key = os.getenv("OPENROUTER_API_KEY", "")
    if not perplexity_key or not perplexity_key.startswith("sk-or-"):
        raise HTTPException(503, "OPENROUTER_API_KEY ยังไม่ได้ตั้งค่า หรือไม่ถูกต้อง")

    if _analyze_state.get("status") == "running":
        return {
            "message": "Analysis already running",
            "progress": _analyze_state,
        }

    prov_list  = [p.strip() for p in provinces.split(",")] if provinces else []
    types_list = [t.strip() for t in types.split(",")]     if types     else []
    url_list   = (body.urls if body and body.urls else [])
    force_flag = force or (body.force_reanalyze if body else False)

    background_tasks.add_task(
        _run_analysis,
        limit, hot_only, prov_list, types_list,
        price_min, price_max, ai_status, force_flag, url_list,
    )
    return {
        "message":   "Analysis started",
        "limit":     limit,
        "hot_only":  hot_only,
        "provinces": prov_list or "ทุกจังหวัด",
        "urls_mode": len(url_list) > 0,
    }


async def _run_analysis(
    limit: int,
    hot_only: bool,
    provinces: list[str] | None = None,
    types: list[str] | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    ai_status: str = "all",
    force: bool = False,
    url_list: list[str] | None = None,
):
    """Background task: fetch pending deals → market cache / Perplexity analyze → save back."""
    global _analyze_state

    from crawler.perplexity_analyzer import PerplexityAnalyzer

    db = _get_supabase()
    analyzer = PerplexityAnalyzer()
    start_time = datetime.now(timezone.utc)

    # ── Reset progress state ───────────────────────────────────────────────
    _analyze_state.update({
        "status":      "running",
        "started_at":  start_time.isoformat(),
        "finished_at": None,
        "total":       0,
        "done":        0,
        "enriched":    0,
        "cached":      0,
        "failed_count":0,
        "current":     None,
        "error":       None,
        "elapsed":     0,
        "eta":         None,
    })

    try:
        FIELDS = (
            "id,listing_url,source_domain,property_type,project_name,"
            "location,price,area_sqm,land_area_sqm,roi_percent,priority,"
            "condition,ai_analyzed_at,reno_cost_total,transfer_fee,"
            "market_value,buy_price"
        )

        # ── URL selection mode ─────────────────────────────────────────────
        if url_list:
            all_deals = []
            CHUNK = 50
            for i in range(0, len(url_list), CHUNK):
                chunk = url_list[i:i+CHUNK]
                rows = db.table("deals").select(FIELDS).in_("listing_url", chunk).execute().data or []
                all_deals.extend(rows)
            if not force:
                pending = [d for d in all_deals if not d.get("ai_analyzed_at")]
            else:
                pending = all_deals
        else:
            # ── Filter-based mode ──────────────────────────────────────────
            q = db.table("deals").select(FIELDS)
            if not force and ai_status != "analyzed":
                q = q.is_("ai_analyzed_at", "null")
            elif ai_status == "analyzed":
                q = q.not_.is_("ai_analyzed_at", "null")

            if hot_only:
                q = q.gte("roi_percent", 30)
            if price_min is not None:
                q = q.gte("price", price_min)
            if price_max is not None:
                q = q.lte("price", price_max)

            fetch_limit = limit * 5 if (provinces or types) else limit
            all_pending = q.order("scraped_at", desc=True).limit(fetch_limit).execute().data or []

            # Post-filter จังหวัด + ประเภท (free-text → ทำ server-side ไม่ได้ตรง)
            def _match(d: dict) -> bool:
                loc = d.get("location") or ""
                if provinces and not any(p in loc for p in provinces): return False
                if types and d.get("property_type") not in types:      return False
                return True

            pending = [d for d in all_pending if _match(d)][:limit]
            logger.info(f"Analyze batch: {len(pending)} deals | prov={provinces} types={types}")

        _analyze_state["total"] = len(pending)
        if not pending:
            _analyze_state.update({"status": "done", "finished_at": datetime.now(timezone.utc).isoformat()})
            return

        # ── Process each deal ──────────────────────────────────────────────
        for i, deal in enumerate(pending):
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            eta     = (elapsed / (i + 1)) * (len(pending) - i - 1) if i > 0 else None
            _analyze_state.update({
                "done":    i,
                "elapsed": int(elapsed),
                "eta":     int(eta) if eta is not None else None,
                "current": {
                    "title":    deal.get("project_name") or TYPE_TH_PY.get(deal.get("property_type"), "ทรัพย์"),
                    "location": (deal.get("location") or "—")[:60],
                },
            })

            try:
                result = await analyzer.analyze_deal(deal)
                if result:
                    # ── whitelist: เฉพาะ column ที่มีอยู่ใน deals table ─────────
                    VALID_COLS = {
                        "ai_analysis", "ai_analyzed_at", "updated_at",
                        "roi_data_source", "roi_percent", "roi_min", "roi_max",
                        "roi_valid", "roi_flag", "priority",
                        "market_value", "market_value_min", "market_value_max",
                        "market_value_before_reno",
                        "market_price_sqm", "market_price_sqm_min", "market_price_sqm_max",
                        "price_original_low", "price_original_high",
                        "price_good_low", "price_good_high",
                        "price_reno_low", "price_reno_high",
                        "rental_monthly_est",
                        "reno_cost_total", "reno_cost_sqm", "transfer_fee",
                        "total_cost", "estimated_profit", "estimated_profit_max",
                        "project_official_name", "project_developer", "project_address",
                        "source_urls",
                    }
                    update_data = {
                        k: v for k, v in result.items()
                        if k in VALID_COLS and k != "ai_analysis"
                    }
                    update_data["ai_analysis"] = result.get("ai_analysis")
                    update_data["updated_at"]  = datetime.now(timezone.utc).isoformat()
                    try:
                        db.table("deals").update(update_data).eq("id", deal["id"]).execute()
                    except Exception as db_err:
                        logger.error(
                            f"DB update FAILED deal {deal.get('id')}: {db_err} "
                            f"| fields={list(update_data.keys())}"
                        )
                        raise
                    # Distinguish cache vs Sonar Pro
                    if result.get("roi_data_source") == "cache":
                        _analyze_state["cached"] += 1
                    else:
                        _analyze_state["enriched"] += 1
                else:
                    _analyze_state["failed_count"] += 1
            except Exception as deal_err:
                logger.error(f"Deal analyze error ({deal.get('id')}): {deal_err}")
                _analyze_state["failed_count"] += 1

        elapsed_total = (datetime.now(timezone.utc) - start_time).total_seconds()
        _analyze_state.update({
            "status":      "done",
            "done":        len(pending),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed":     int(elapsed_total),
            "eta":         0,
            "current":     None,
        })
        logger.success(
            f"Analysis done: {_analyze_state['enriched']} sonar | "
            f"{_analyze_state['cached']} cache | "
            f"{_analyze_state['failed_count']} failed"
        )

    except Exception as e:
        logger.error(f"Analysis batch failed: {e}")
        _analyze_state.update({"status": "failed", "error": str(e)})


# ── ชื่อประเภทภาษาไทย (ใช้ใน _run_analysis) ────────────────────────────────
TYPE_TH_PY = {
    "land": "ที่ดิน", "condo": "คอนโด", "house": "บ้านเดี่ยว",
    "townhouse": "ทาวน์เฮาส์", "commercial": "อาคารพาณิชย์",
}


@app.post("/deals/reset-analysis")
async def reset_analysis():
    """
    ล้างข้อมูล AI Analysis ทั้งหมดออกจาก deals
    — ไม่ลบตัว deal (listing_url, price, location ฯลฯ คงอยู่)
    — ลบเฉพาะ field ที่ Sonar Pro/analyzer เขียน
    — ทำเป็น batch เพื่อกัน schema cache error ถ้า column ยังไม่ได้ migrate
    """
    db = _get_supabase()

    # แบ่งเป็น 2 กลุ่ม:
    # กลุ่ม A — columns ที่มีในทุก schema (migration เก่า)
    core_fields: dict = {
        "ai_analysis":            None,
        "ai_analyzed_at":         None,
        "roi_data_source":        None,
        "source_urls":            None,
        "market_value":           None,
        "market_value_before_reno": None,
        "market_price_sqm":       None,
        "reno_cost_total":        None,
        "reno_cost_sqm":          None,
        "transfer_fee":           None,
        "total_cost":             None,
        "estimated_profit":       None,
        "roi_percent":            None,
        "roi_valid":              None,
        "roi_flag":               None,
        "priority":               None,
    }

    # กลุ่ม B — columns ใหม่จาก migration 006 (อาจยังไม่มีในบาง deploy)
    new_fields: dict = {
        "price_original_low":     None,
        "price_original_high":    None,
        "price_good_low":         None,
        "price_good_high":        None,
        "price_reno_low":         None,
        "price_reno_high":        None,
        "rental_monthly_est":     None,
        "project_official_name":  None,
        "project_developer":      None,
        "project_address":        None,
        "market_value_min":       None,
        "market_value_max":       None,
        "market_price_sqm_min":   None,
        "market_price_sqm_max":   None,
        "roi_min":                None,
        "roi_max":                None,
        "estimated_profit_max":   None,
    }

    cleared = 0
    reset_fields: list[str] = []
    errors: list[str] = []

    # รัน core fields ก่อน — ต้องสำเร็จ
    try:
        # นับ deals ที่มี AI data ก่อน reset (Supabase v2+ ไม่ return rows จาก bulk UPDATE)
        count_before_r = db.table("deals").select("id", count="exact") \
            .not_.is_("ai_analyzed_at", "null").execute()
        count_before = count_before_r.count or 0

        db.table("deals").update(core_fields).not_.is_("id", "null").execute()

        # นับหลัง reset
        count_after_r = db.table("deals").select("id", count="exact") \
            .not_.is_("ai_analyzed_at", "null").execute()
        count_after = count_after_r.count or 0

        cleared = count_before - count_after
        if cleared == 0 and count_before > 0:
            # Supabase ไม่ return affected rows — ใช้ count_before แทน
            cleared = count_before

        reset_fields.extend(core_fields.keys())
        logger.info(f"reset-analysis core: before={count_before} after={count_after} cleared={cleared}")
    except Exception as e:
        logger.error(f"reset-analysis core error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # รัน new fields — ถ้า column ไม่มีให้ข้ามไป (ไม่ error)
    for col, val in new_fields.items():
        try:
            db.table("deals").update({col: val}).not_.is_("id", "null").execute()
            reset_fields.append(col)
        except Exception as e:
            err_msg = str(e)
            if "column" in err_msg.lower() or "schema" in err_msg.lower():
                logger.warning(f"reset-analysis: skip '{col}' — column not in schema yet (run migration 006)")
                errors.append(f"{col}: not in schema")
            else:
                logger.warning(f"reset-analysis: '{col}' error — {err_msg}")
                errors.append(f"{col}: {err_msg[:60]}")

    logger.info(f"reset-analysis done: {cleared} deals | {len(reset_fields)} fields reset | {len(errors)} skipped")
    return {
        "status":        "ok",
        "cleared":       cleared,
        "fields_reset":  reset_fields,
        "skipped_fields": errors,
        "note":          "รัน migrations/006_rich_analysis_columns.sql ใน Supabase เพื่อ reset ทุก field" if errors else None,
    }


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
