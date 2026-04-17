"""
Quick live test — Spider vs npa.krungthai.com (Priority 1 site)
รัน: python test_spider_live.py
"""
import asyncio
import sys
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from crawler.spider import RealEstateCrawler, CrawlConfig, LinkHarvester, PageFetcher

TARGET = "https://npa.krungthai.com"


async def main():
    print(f"\n{'='*60}")
    print(f"  RE:DD Spider — Live Test v3")
    print(f"  Target: {TARGET}")
    print(f"{'='*60}\n")

    # ── Step 1: ดึง HTML ด้วย PageFetcher (stealth) ──
    print("🌐 ดึง HTML...", flush=True)
    fetcher = PageFetcher()
    await fetcher.start()
    html = await fetcher.fetch(TARGET)
    await fetcher.stop()

    print(f"   HTML length: {len(html):,} chars")
    if len(html) < 1000:
        print("   ⚠️  HTML สั้นมาก — อาจถูกบล็อก:")
        print(html[:500])
        return 0

    # ── Step 2: extract links ──
    print("\n🔍 Extract listing links...")
    harvester = LinkHarvester()
    links = harvester.extract_listing_links(html, TARGET)
    print(f"   พบ {len(links)} links (score ≥ 0.4)\n")

    # Debug: show all hrefs that contain 'propertyDetail'
    soup = BeautifulSoup(html, "html.parser")
    detail_links = [
        urljoin(TARGET, a["href"])
        for a in soup.find_all("a", href=True)
        if "propertyDetail" in a.get("href", "")
    ]
    print(f"   href ที่มี 'propertyDetail': {len(detail_links)}")
    for u in detail_links[:5]:
        print(f"     {u}")

    # ── Step 3: full harvest ──
    print("\n🕷️  Full harvest (3 pages, max 50 listings)...")
    config = CrawlConfig(
        base_url=TARGET,
        max_pages=3,
        max_listings=50,
        delay_min=1.5,
        delay_max=2.5,
    )
    crawler = RealEstateCrawler(config=config)
    urls = await crawler.harvest(TARGET, max_pages=3, max_listings=50)

    print(f"\n{'='*60}")
    print(f"  ผลลัพธ์: พบ {len(urls)} listing URLs")
    print(f"{'='*60}")

    if urls:
        print("\nตัวอย่าง URLs แรก 10 รายการ:")
        for i, u in enumerate(urls[:10], 1):
            print(f"  {i}. {u}")
        passed = len(urls) >= 10
        result = f"✅ PASS ({len(urls)} URLs)" if passed else f"⚠️  {len(urls)} URLs (ต้องการ ≥10)"
    else:
        result = "❌ ไม่พบ URLs เลย"

    print(f"\n{result}\n")
    return len(urls)


if __name__ == "__main__":
    count = asyncio.run(main())
    sys.exit(0 if count >= 1 else 1)
