"""Tests for RE:DD LinkHarvester / Spider."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Unit tests (no network)
# ---------------------------------------------------------------------------

def test_spider_import():
    """Spider module should import without errors."""
    from crawler import spider  # noqa: F401
    assert spider is not None


def test_spider_has_required_classes():
    """Spider should expose LinkHarvester (or similar main class)."""
    import crawler.spider as s
    # Accept any of these common names
    has_class = any(
        hasattr(s, name) for name in ["LinkHarvester", "Spider", "Crawler", "RealEstateCrawler"]
    )
    assert has_class, "spider.py must expose a harvester/crawler class"


# ---------------------------------------------------------------------------
# Integration test (requires network — skip in CI)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("os").getenv("RUN_INTEGRATION_TESTS"),
    reason="Set RUN_INTEGRATION_TESTS=1 to run network tests",
)
@pytest.mark.asyncio
async def test_spider_led_live():
    """Live test: crawl led.go.th and find ≥10 listing URLs."""
    import crawler.spider as s

    HarvesterClass = getattr(s, "LinkHarvester", None) or getattr(s, "Spider", None)
    assert HarvesterClass is not None

    harvester = HarvesterClass(max_pages=2, max_listings=50)
    urls = await harvester.harvest("https://led.go.th/assets")

    assert isinstance(urls, list), "harvest() must return a list"
    assert len(urls) >= 10, f"Expected ≥10 URLs, got {len(urls)}"
    for url in urls:
        assert url.startswith("http"), f"Invalid URL: {url}"
