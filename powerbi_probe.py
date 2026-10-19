#!/usr/bin/env python3
"""One-time probe: opens the Naugatuck and Middlebury Power BI dashboards in a headless
browser and saves a screenshot, the page text, and the raw data responses to probe/.
Send those files back so the real parsers can be written against what the dashboards return."""
import asyncio
import json
import os

from playwright.async_api import async_playwright

URLS = {
    "naugatuck": "https://app.powerbi.com/view?r=eyJrIjoiYzg2Zjc5ZmUtYWYyZi00ZDQ5LTgyNjgtZGU4YjI1ZWEyZTUxIiwidCI6IjNiMjc3NDMwLTNjOGYtNGQ3Yy04Y2EwLTY2Zjk5YzA0MTcwYSIsImMiOjJ9",
    "middlebury": "https://app.powerbigov.us/view?r=eyJrIjoiMDQ5ZjI3ZmItOWU1MS00ZDkyLWE4N2YtMTMxYTY3NjU2ZTA0IiwidCI6ImU5YzM5MTVjLTRkNjgtNDY1MC1iNDUzLTViNmI3ZDQxYzA3NSJ9",
}


async def probe(browser, name, url):
    page = await browser.new_page(viewport={"width": 1700, "height": 1100})
    captured = []

    async def on_response(resp):
        if any(k in resp.url for k in ("querydata", "conceptualschema", "modelsAndExploration")):
            try:
                captured.append({"url": resp.url, "body": await resp.text()})
            except Exception as ex:
                captured.append({"url": resp.url, "error": str(ex)})

    page.on("response", on_response)
    await page.goto(url, wait_until="networkidle", timeout=90000)
    await page.wait_for_timeout(8000)
    await page.screenshot(path=f"probe/{name}-home.png", full_page=True)
    open(f"probe/{name}-home.txt", "w").write(await page.inner_text("body"))
    # Naugatuck: open the Arrests tab (last button in the left column)
    if name == "naugatuck":
        try:
            await page.get_by_text("Arrests", exact=True).first.click()
            await page.wait_for_timeout(8000)
            await page.screenshot(path=f"probe/{name}-arrests.png", full_page=True)
            open(f"probe/{name}-arrests.txt", "w").write(await page.inner_text("body"))
        except Exception as ex:
            open(f"probe/{name}-arrests.txt", "w").write(f"Could not open Arrests tab: {ex}")
    json.dump(captured, open(f"probe/{name}-responses.json", "w"))
    await page.close()


async def main():
    os.makedirs("probe", exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for name, url in URLS.items():
            try:
                await probe(browser, name, url)
            except Exception as ex:
                open(f"probe/{name}-error.txt", "w").write(str(ex))
        await browser.close()


asyncio.run(main())
