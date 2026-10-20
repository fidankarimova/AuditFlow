"""Clicks through the UI happy path in headless Chrome and saves screenshots (Phase 8 evidence).

Needs `make up` + `make ui` running, and `pip install playwright` (uses the system Chrome, no download).
Usage: python scripts/ui_screenshots.py evidence/screenshots
"""

import sys
from playwright.sync_api import sync_playwright, expect

BASE = "http://localhost:8501"
OUT = sys.argv[1]


def settle(page, ms=1500):
    page.wait_for_timeout(400)
    try:
        page.get_by_test_id("stStatusWidget").wait_for(state="hidden", timeout=120_000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def shot(page, name):
    settle(page)
    page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
    print("saved", name)


def go(page, path):
    page.goto(f"{BASE}/{path}")
    settle(page, 2500)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 2600})
    go(page, "")
    shot(page, "0_home")
    go(page, "Financial_Command_Center")
    expect(page.get_by_text("Cash crisis in 10 days")).to_be_visible(timeout=120_000)
    shot(page, "1_financial_command_center")
    go(page, "Liquidity_Stress_Lab")
    shot(page, "2_liquidity_stress_lab")
    go(page, "Intelligent_Receivables")
    shot(page, "3_intelligent_receivables")

    go(page, "Live_AI_Negotiation_Room")
    page.get_by_test_id("stMainBlockContainer").get_by_test_id("stSelectbox").click()
    page.get_by_role("option", name="ALF-2026-0142").click()
    settle(page)
    page.get_by_role("button", name="Verify invoice").click()
    expect(page.get_by_role("button", name="Run negotiation")).to_be_visible(timeout=120_000)
    page.get_by_role("button", name="Run negotiation").click()
    expect(page.get_by_text("Agreement: x =")).to_be_visible(timeout=180_000)
    shot(page, "4_negotiation_room")

    go(page, "Privacy_and_Compliance")
    shot(page, "5_privacy_and_compliance")

    go(page, "Agreement_and_Approval")
    page.get_by_role("button", name="Sign as supplier").click()
    expect(page.get_by_text("✅ signed")).to_be_visible(timeout=60_000)
    page.get_by_text("Buyer (Beta Markets)").first.click()
    settle(page)
    page.get_by_role("button", name="Sign as buyer").click()
    expect(page.get_by_role("button", name="Execute bank payment")).to_be_visible(timeout=60_000)
    shot(page, "6a_agreement_approved")
    page.get_by_role("button", name="Execute bank payment").click()
    settle(page, 3000)
    shot(page, "6b_agreement_executed")

    go(page, "Settlement_and_Audit_Explorer")
    page.wait_for_timeout(3000)
    go(page, "Settlement_and_Audit_Explorer")
    expect(page.get_by_text("SETTLED").first).to_be_visible(timeout=60_000)
    page.get_by_role("button", name="Replay bank request x3").click()
    expect(page.get_by_text("No second debit")).to_be_visible(timeout=60_000)
    page.get_by_role("button", name="Send forged webhook").click()
    expect(page.get_by_text("Rejected. Coordinator answered HTTP 401")).to_be_visible(timeout=60_000)
    page.get_by_role("button", name="Verify audit chain").click()
    expect(page.get_by_text("Chain intact")).to_be_visible(timeout=60_000)
    shot(page, "7_settlement_and_audit")
    browser.close()
