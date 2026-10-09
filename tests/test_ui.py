"""Browser end-to-end tests of the operator dashboard (Playwright).

Locally this uses the installed Microsoft Edge (no browser download); in CI set
LOADGUARD_BROWSER=chromium after `playwright install chromium`.
"""
import os

import pytest
from playwright.async_api import async_playwright, expect

from conftest import lab
from loadguard.csms import Config

pytestmark = pytest.mark.ui


async def _browser(p):
    which = os.environ.get("LOADGUARD_BROWSER", "msedge")
    if which == "chromium":
        return await p.chromium.launch()
    return await p.chromium.launch(channel=which)


async def test_operator_starts_and_stops_a_session_from_the_dashboard():
    async with lab(Config(cap_kw=10.0), n=3, http=True) as (csms, fleet):
        async with async_playwright() as p:
            browser = await _browser(p)
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{csms.http_port}/")
            await expect(page.get_by_test_id("stations").locator("tr")).to_have_count(3)
            await expect(page.get_by_test_id("status-CS001")).to_have_text("Available")
            await page.get_by_test_id("start-CS001").click()
            await expect(page.get_by_test_id("status-CS001")).to_have_text("Occupied")
            await expect(page.get_by_test_id("power-CS001")).to_have_text("7.20")
            await page.get_by_test_id("stop-CS001").click()
            await expect(page.get_by_test_id("status-CS001")).to_have_text("Available")
            await expect(page.get_by_test_id("power-CS001")).to_have_text("0.00")
            await browser.close()
        assert len(csms.billed_kwh) == 1


async def test_lowering_the_cap_in_the_dashboard_curtails_the_site():
    async with lab(Config(cap_kw=20.0), n=3, http=True, panel_kw=20.0) as (csms, fleet):
        for sid in ("CS000", "CS001", "CS002"):
            await csms.remote_start(sid)
        async with async_playwright() as p:
            browser = await _browser(p)
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{csms.http_port}/")
            await expect(page.get_by_test_id("site-kw")).to_have_text("20.00 kW")
            await page.get_by_test_id("cap-input").fill("9")
            await page.get_by_test_id("cap-apply").click()
            await expect(page.get_by_test_id("cap-kw")).to_have_text("9.00")
            await expect(page.get_by_test_id("site-kw")).to_have_text("9.00 kW")
            for sid in ("CS000", "CS001", "CS002"):
                await expect(page.get_by_test_id(f"limit-{sid}")).to_have_text("3.00")
            await expect(page.get_by_test_id("site-kw")).not_to_have_class("over")
            await page.screenshot(path=str(os.path.join(os.path.dirname(__file__), "..", "docs", "dashboard.png")))
            await browser.close()
        assert fleet.site_kw <= 9.0 + 1e-6
