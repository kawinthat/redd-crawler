"""
Phase 3 — AI Extractor + ROI Engine Test
รัน: python test_extractor_live.py
  - ROI tests รันได้ทันที (ไม่ต้อง API key)
  - AI extraction ต้องการ ANTHROPIC_API_KEY ใน .env
"""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from crawler.extractor import HtmlCleaner, DetailExtractor, ROIEngine


# ─────────────────────────────────────────────────────────
# TEST 1: ROI Engine
# ─────────────────────────────────────────────────────────

def test_roi_engine():
    print("\n" + "="*60)
    print("  TEST 1: ROI Engine")
    print("="*60)

    engine = ROIEngine()
    passed = 0
    total = 0

    tests = [
        # (label, input, expected_flag)
        ("ปทุมธานี condo poor 1.65M 120sqm",
         dict(price=1_650_000, area_sqm=120, condition="poor",
              property_type="condo", location="ปทุมธานี"),
         "HIGH"),  # ROI ~35%

        ("กรุงเทพ condo fair 2M 60sqm",
         dict(price=2_000_000, area_sqm=60, condition="fair",
              property_type="condo", location="กรุงเทพ"),
         "HIGH"),  # 60*95000=5.7M vs 2M+270k+80k=2.35M → ROI 142%

        ("overpriced กรุงเทพ condo 10M 40sqm",
         dict(price=10_000_000, area_sqm=40, condition="good",
              property_type="condo", location="กรุงเทพ"),
         "LOW"),   # 40*95000=3.8M vs 10M+ = negative ROI

        ("missing data",
         dict(price=None, area_sqm=50, condition="fair",
              property_type="condo", location="กรุงเทพ"),
         None),  # should return roi_valid=False
    ]

    for label, inp, expected_priority in tests:
        total += 1
        result = engine.calculate(inp)

        if expected_priority is None:
            ok = not result.get("roi_valid", True)
            status = "✅" if ok else "❌"
            print(f"  {status} [{label}]: roi_valid={result.get('roi_valid')}")
        else:
            ok = result.get("priority") == expected_priority
            status = "✅" if ok else "❌"
            roi = result.get("roi_percent", "?")
            flag = result.get("roi_flag", "?")
            print(f"  {status} [{label}]")
            print(f"       ROI={roi}%  flag={flag}  priority={result.get('priority')} (expected {expected_priority})")

        if ok:
            passed += 1

    print(f"\n  Result: {passed}/{total} passed {'✅' if passed==total else '⚠️'}")
    return passed == total


# ─────────────────────────────────────────────────────────
# TEST 2: HtmlCleaner on real fixture
# ─────────────────────────────────────────────────────────

def test_html_cleaner():
    print("\n" + "="*60)
    print("  TEST 2: HtmlCleaner on real fixture")
    print("="*60)

    fixture = "fixtures/krungthai_1.html"
    if not os.path.exists(fixture):
        print(f"  ⚠️  {fixture} not found — run spider first")
        return False

    with open(fixture, encoding="utf-8") as f:
        html = f.read()

    cleaner = HtmlCleaner()
    cleaned = cleaner.clean(html, max_chars=4000)

    ratio = len(cleaned) / len(html) * 100
    print(f"  Raw: {len(html):,} chars → Cleaned: {len(cleaned):,} chars ({ratio:.1f}%)")
    print(f"\n  Cleaned preview (first 600 chars):")
    print("  " + cleaned[:600].replace("\n", "\n  "))

    ok = len(cleaned) > 100 and len(cleaned) <= 4000
    print(f"\n  {'✅ PASS' if ok else '❌ FAIL'}")
    return ok


# ─────────────────────────────────────────────────────────
# TEST 3: AI Extraction (requires ANTHROPIC_API_KEY)
# ─────────────────────────────────────────────────────────

async def test_ai_extraction():
    print("\n" + "="*60)
    print("  TEST 3: AI Batch Extraction")
    print("="*60)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "sk-ant-your-key-here":
        print("  ⚠️  ANTHROPIC_API_KEY ไม่ได้ตั้งค่า — ข้าม test นี้")
        print("  📌 ใส่ key ใน .env แล้วรันใหม่เพื่อ test AI extraction")
        return None  # not failed, just skipped

    fixtures = [f"fixtures/krungthai_{i}.html" for i in range(1, 4)]
    available = [f for f in fixtures if os.path.exists(f)]

    if not available:
        print("  ⚠️  ไม่มี fixtures — run spider ก่อน")
        return False

    extractor = DetailExtractor(api_key=api_key)
    listings = []

    for fname in available[:2]:  # test แค่ 2 ไฟล์ประหยัด cost
        with open(fname, encoding="utf-8") as f:
            html = f.read()
        listings.append({
            "url": f"https://npa.krungthai.com/propertyDetail/test",
            "html": html,
            "source_domain": "npa.krungthai.com",
        })

    print(f"  Extracting {len(listings)} listings (batch_size=2)...")
    results = await extractor.extract_batch(listings, batch_size=2)

    print(f"\n  Results:")
    roi_engine = ROIEngine()
    passed = 0
    for i, r in enumerate(results):
        if "error" in r:
            print(f"  ❌ [{i+1}] Error: {r['error']}")
            continue
        price = r.get("price")
        area = r.get("area_sqm")
        location = r.get("location", "")
        prop_type = r.get("property_type", "")
        condition = r.get("condition", "")
        print(f"  [{i+1}] {prop_type} | {location}")
        print(f"       price={price:,}" if price else f"       price=null")
        print(f"       area={area} sqm | condition={condition}")

        if price and area:
            roi = roi_engine.calculate(r)
            if roi.get("roi_valid"):
                print(f"       ROI={roi['roi_percent']}% {roi['roi_flag']}")
            passed += 1

    ok = passed >= 1
    print(f"\n  {'✅ PASS' if ok else '❌ FAIL'} ({passed}/{len(results)} extracted successfully)")
    return ok


# ─────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────

async def main():
    print("\n" + "="*60)
    print("  RE:DD Phase 3 — Extractor + ROI Tests")
    print("="*60)

    r1 = test_roi_engine()
    r2 = test_html_cleaner()
    r3 = await test_ai_extraction()

    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    print(f"  ROI Engine:     {'✅ PASS' if r1 else '❌ FAIL'}")
    print(f"  HtmlCleaner:    {'✅ PASS' if r2 else '❌ FAIL'}")
    print(f"  AI Extraction:  {'✅ PASS' if r3 else ('⏭️  SKIPPED (no API key)' if r3 is None else '❌ FAIL')}")
    print()

    critical_pass = r1 and r2
    print(f"  Overall: {'✅ PASS' if critical_pass else '❌ FAIL'} (AI extraction optional until API key set)")
    return 0 if critical_pass else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)
