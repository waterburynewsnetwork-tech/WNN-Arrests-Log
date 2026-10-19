#!/usr/bin/env python3
"""Reads the Arrests page of the Naugatuck and Middlebury Power BI dashboards and merges the
results into data.json (run after scrape.py). Keeps name, town, charges, arrest date only.
Skips anyone under 18. If nothing can be read, it saves a screenshot and the page text
to debug/ so the problem can be diagnosed."""
import asyncio
import datetime as dt
import json
import os
import re

from playwright.async_api import async_playwright

import scrape  # reuses clean_name, clean_charge, key, WINDOW_DAYS

DASHBOARDS = [
    {"town": "Naugatuck",
     "url": "https://app.powerbi.com/view?r=eyJrIjoiYzg2Zjc5ZmUtYWYyZi00ZDQ5LTgyNjgtZGU4YjI1ZWEyZTUxIiwidCI6IjNiMjc3NDMwLTNjOGYtNGQ3Yy04Y2EwLTY2Zjk5YzA0MTcwYSIsImMiOjJ9"},
    {"town": "Middlebury",
     "url": "https://app.powerbigov.us/view?r=eyJrIjoiMDQ5ZjI3ZmItOWU1MS00ZDkyLWE4N2YtMTMxYTY3NjU2ZTA0IiwidCI6ImU5YzM5MTVjLTRkNjgtNDY1MC1iNDUzLTViNmI3ZDQxYzA3NSJ9"},
]

ROWS_JS = """() => [...document.querySelectorAll('[role="row"]')].map(r => ({
  cells: [...r.querySelectorAll('[role="gridcell"],[role="rowheader"]')].map(c => {
    const b = c.getBoundingClientRect();
    return {t: (c.innerText || '').trim(), x: b.x + b.width / 2, y: b.y + b.height / 2};
  })
}))"""

DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2})$")


def parse_row(cells):
    """cells: list of {t,x,y}. Returns a dict for an arrest row, else None."""
    texts = [c["t"] for c in cells]
    date = next((DATE_RE.match(t) for t in texts if DATE_RE.match(t)), None)
    name_cell = next((c for c in cells if "," in c["t"] and not DATE_RE.match(c["t"])), None)
    if not date or not name_cell:
        return None
    age = next((int(t) for t in texts if re.fullmatch(r"\d{1,3}", t) and int(t) < 120), None)
    try:
        d = dt.date(2000 + int(date[3]), int(date[1]), int(date[2]))
    except ValueError:
        return None
    return {"date": d, "name": scrape.clean_name(name_cell["t"]), "age": age,
            "id": next((t for t in texts if re.fullmatch(r"\d{4,}", t)), ""),
            "click": (name_cell["x"], name_cell["y"])}


def charges_from(text):
    m = re.search(r"\nCharges\n(.*?)\nArrest Notes\n", text, re.S)
    if not m:
        return []
    out = []
    for ln in m[1].splitlines():
        ln = ln.strip()
        if (not ln or ln in ("Counts", "Select Row", "Row Selection") or ln.startswith("Scroll ")
                or re.fullmatch(r"[\d,]+", ln)):
            continue
        out.append(scrape.clean_charge(ln))
    return list(dict.fromkeys(c for c in out if c))


async def read_dashboard(browser, cfg, cutoff):
    page = await browser.new_page(viewport={"width": 1700, "height": 1100})
    await page.goto(cfg["url"], wait_until="networkidle", timeout=90000)
    await page.wait_for_timeout(6000)
    await page.get_by_text("Arrests", exact=True).first.click()
    await page.wait_for_timeout(6000)
    seen, stale = {}, 0
    for _ in range(80):
        rows = await page.evaluate(ROWS_JS)
        new = False
        first_xy = None
        for r in rows:
            p = parse_row(r["cells"])
            if not p:
                continue
            first_xy = first_xy or p["click"]
            k = (p["id"], p["name"], p["date"])
            if k in seen:
                continue
            new = True
            try:
                await page.mouse.click(*p["click"])
                await page.wait_for_timeout(1500)
                p["charges"] = charges_from(await page.inner_text("body"))
            except Exception:
                p["charges"] = []
            seen[k] = p
        stale = 0 if new else stale + 1
        if stale >= 3 or not first_xy:
            break
        await page.mouse.move(*first_xy)
        await page.mouse.wheel(0, 260)
        await page.wait_for_timeout(800)
    if not seen:
        os.makedirs("debug", exist_ok=True)
        await page.screenshot(path=f"debug/{cfg['town']}.png", full_page=True)
        open(f"debug/{cfg['town']}.txt", "w").write(await page.inner_text("body"))
    await page.close()
    return [p for p in seen.values() if p["date"] >= cutoff and (p["age"] is None or p["age"] >= 18)]


async def main():
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=scrape.WINDOW_DAYS)
    data = json.load(open(scrape.OUT))
    merged = {scrape.key(e): e for e in data.get("arrests", [])}
    towns = data.get("towns", {})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for cfg in DASHBOARDS:
            st = {"status": "ok", "logs_read": 1, "latest": None, "note": "Power BI dashboard."}
            towns[cfg["town"]] = st
            try:
                entries = await read_dashboard(browser, cfg, cutoff)
                for p in entries:
                    rec = {"town": cfg["town"], "name": p["name"], "date": p["date"].isoformat(),
                           "charges": p["charges"], "source": "dashboard"}
                    merged[scrape.key(rec)] = rec
                if not entries:
                    st.update(status="warning", note="No arrests read from the dashboard; layout may have changed.")
                elif not any(p["charges"] for p in entries):
                    st.update(status="warning", note="Names and dates read, but no charges.")
            except Exception as ex:
                st.update(status="error", logs_read=0, note=f"Dashboard failed: {ex}")
        await browser.close()
    arrests = sorted((e for e in merged.values() if e["date"] >= cutoff.isoformat()),
                     key=lambda e: (e["date"], e["town"], e["name"]), reverse=True)
    for s in towns.values():
        s["latest"] = None
    for e in arrests:
        s = towns.get(e["town"])
        if s and (s["latest"] is None or e["date"] > s["latest"]):
            s["latest"] = e["date"]
    data.update(arrests=arrests, towns=towns,
                updated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    json.dump(data, open(scrape.OUT, "w"), indent=1)
    for t in ("Naugatuck", "Middlebury"):
        print(t, towns[t])


if __name__ == "__main__":
    asyncio.run(main())
