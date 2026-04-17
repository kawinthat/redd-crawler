"""Tests for RE:DD AI Extractor + ROI Engine."""

import pytest


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_extractor_import():
    """Extractor module should import without errors."""
    from crawler import extractor  # noqa: F401
    assert extractor is not None


def test_roi_calculation():
    """ROI formula smoke test.

    Known case: price=1,650,000 THB, area=120 sqm, condition=poor, location=ปทุมธานี
    Expected ROI ≈ 41% (from project spec)
    """
    import crawler.extractor as e

    # Find the ROI calculation function — accept common names
    calc_fn = None
    for name in ["calculate_roi", "compute_roi", "roi_engine", "get_roi"]:
        if hasattr(e, name):
            calc_fn = getattr(e, name)
            break

    if calc_fn is None:
        # Try to find it inside a class
        for name in dir(e):
            obj = getattr(e, name)
            if isinstance(obj, type) and hasattr(obj, "calculate_roi"):
                calc_fn = obj().calculate_roi
                break

    assert calc_fn is not None, "extractor.py must expose a calculate_roi function or method"

    result = calc_fn(
        price=1_650_000,
        area_sqm=120,
        condition="poor",
        property_type="condo",
        location="ปทุมธานี",
    )

    # Accept dict or numeric result
    if isinstance(result, dict):
        roi = result.get("roi_percent", result.get("roi", 0))
    else:
        roi = float(result)

    assert 35 <= roi <= 50, f"Expected ROI ~41%, got {roi}%"


# ---------------------------------------------------------------------------
# Integration test (requires ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("os").getenv("ANTHROPIC_API_KEY"),
    reason="Set ANTHROPIC_API_KEY to run AI extraction tests",
)
@pytest.mark.asyncio
async def test_batch_extraction_sample():
    """Test AI extraction against a minimal HTML snippet."""
    import crawler.extractor as e

    sample_html = """
    <div class="property-card">
      <h3>คอนโด ลาดพร้าว 2 ชั้น</h3>
      <p>ราคา 1,200,000 บาท</p>
      <p>ขนาด 45 ตร.ม.</p>
      <p>ทำเลดี ใกล้ BTS</p>
    </div>
    """

    ExtractorClass = getattr(e, "BatchExtractor", None) or getattr(e, "Extractor", None)
    assert ExtractorClass is not None, "extractor.py must expose BatchExtractor or Extractor class"

    extractor_obj = ExtractorClass()
    results = await extractor_obj.extract([sample_html])

    assert isinstance(results, list)
    assert len(results) >= 1
    record = results[0]
    assert "price" in record or "asking_price" in record, f"Missing price in {record}"
