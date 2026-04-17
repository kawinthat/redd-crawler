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

class ScanRequest(BaseModel):
    url: str              = os.getenv("TARGET_URL", "https://npa.krungthai.com")
    max_pages: int        = int(os.getenv("MAX_PAGES", "56"))
    max_listings: int     = int(os.getenv("MAX_LISTINGS", "2000"))
    concurrency: int      = int(os.getenv("CONCURRENCY", "3"))
    dry_run: bool         = False


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

    try:
        config = CrawlConfig(
            base_url      = req.url,
            max_pages     = req.max_pages,
            max_listings  = req.max_listings,
            concurrency   = req.concurrency,
            delay_min     = 0.5,
            delay_max     = 1.5,
        )

        crawler = AutonomousCrawler(
            dry_run    = req.dry_run,
            line_token = os.getenv("LINE_NOTIFY_TOKEN"),
        )

        stats = await crawler.run(req.url, config)
        _scan_state["stats"]       = stats
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


@app.post("/scan")
async def trigger_scan(req: ScanRequest, background_tasks: BackgroundTasks):
    """Trigger a full crawl in the background. Returns immediately."""
    if _scan_state["status"] == "running":
        raise HTTPException(409, "Scan already running")

    background_tasks.add_task(_run_scan, req)
    return {
        "message":  "Scan started",
        "url":      req.url,
        "dry_run":  req.dry_run,
        "max_listings": req.max_listings,
    }


@app.get("/scan/status")
def scan_status():
    """Return current scan state + progress stats."""
    return _scan_state


@app.get("/deals")
def list_deals(
    page: int       = Query(1, ge=1),
    per_page: int   = Query(50, ge=1, le=200),
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
def hot_deals(limit: int = Query(50, ge=1, le=200)):
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
