#!/usr/bin/env python3
"""Collects press arrest-log PDFs from CT town police sites into data.json.

Keeps a rolling 30-day window. Publishes only name, town, charges, arrest date.
Never copies addresses, birth dates, or remarks. Respects each site's robots.txt.
"""
import datetime as dt
import hashlib
import io
import json
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

UA = "CT-Arrest-Collector/1.0 (contact: YOUR_EMAIL_HERE)"  # <- put your email
WINDOW_DAYS = 30
MAX_LOGS = 12      # newest-looking PDFs to read per town per run
OUT = "data.json"

# parser "blocks": logs where each arrest starts with "Name:" (Cheshire, Watertown, Southington*)
# parser "middlebury": logs where "Arrested:" starts each entry and the name follows later
# *Southington's PDF layout has not been verified; the run reports a warning if nothing parses.
TOWNS = [
    {"town": "Cheshire", "parser": "blocks",
     "index": "https://www.cheshirect.gov/211/Arrest-Logs",
     "link": r"DocumentCenter/View/\d+/.*arrest"},
    {"town": "Watertown/Oakville", "parser": "blocks",
     "index": "https://watertownct.org/departments/police_department/index.php",
     "link": r"arrest.*\.pdf"},
    {"town": "Southington", "parser": "blocks",
     "index": "https://southingtonpolice.org/arrests-log/",
     "link": r"ARREST-LOG.*\.pdf"},
    # Middlebury is a Power BI dashboard (see index.html). To automate it later, add an entry here
    # with "parser": "middlebury" and the page that lists its monthly arrest-log PDFs.
]

DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
STATUTE = r"\d+[a-z]{0,2}-\d+[\w()./*]*"

_robots = {}
_last = 0.0


def allowed(url):
    host = urlparse(url)
    base = f"{host.scheme}://{host.netloc}"
    if base not in _robots:
        rp = RobotFileParser(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def get(url):
    global _last
    if not allowed(url):
        raise PermissionError("robots.txt disallows automated access")
    time.sleep(max(0, 1.5 - (time.time() - _last)))
    _last = time.time()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=40)
    r.raise_for_status()
    return r


def pdf_text(content):
    import pdfplumber  # imported here so the parsers can be tested without it
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def find_logs(cfg):
    html = get(cfg["index"]).text
    seen, out = set(), []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        if re.search(cfg["link"], href, flags=re.I):
            url = urljoin(cfg["index"], href)
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out[:MAX_LOGS] if len(out) <= MAX_LOGS else out[-MAX_LOGS:]


def dates_in(s, today):
    found = []
    for m in DATE.finditer(s):
        try:
            d = dt.date(int(m[3]), int(m[1]), int(m[2]))
        except ValueError:
            continue
        if dt.date(2020, 1, 1) <= d <= today + dt.timedelta(days=1):
            found.append(d)  # drops birth dates and junk like 1919
    return found


def clean_name(raw):
    raw = re.sub(r"\s+", " ", raw).strip(" ,")
    if "," in raw:
        last, first = [p.strip() for p in raw.split(",", 1)]
        raw = f"{first} {last}"
    return raw.title()


def clean_charge(desc):
    desc = re.sub(rf"^(?:{STATUTE}\s+)+", "", desc.strip())
    desc = re.sub(r"\s+\d{1,2}/\d{1,2}/\d{4}.*$", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip().title()
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m[1] + m[2].lower(), desc)


def uniq(items):
    return list(dict.fromkeys(i for i in items if i))


def parse_blocks(text, today):
    out = []
    for blk in re.split(r"(?=\bName:\s)", text)[1:]:
        name = re.match(r"Name:\s*(.+)", blk)[1]
        ds = dates_in(blk, today)
        charges = [clean_charge(m[1]) for m in re.finditer(r"CNTS:\s*\(\d+\)\s*\S+\s+(.+)", blk)]
        for line in blk.splitlines():
            m = re.match(rf"\s*{STATUTE}\s+(.+)", line)
            if m:
                charges.append(clean_charge(m[1]))
        if ds and name:
            out.append({"name": clean_name(name), "date": min(ds), "charges": uniq(charges)})
    return out


def parse_middlebury(text, today):
    out = []
    for p in re.split(r"(?=Arrested:\s*\d{1,2}/\d{1,2}/\d{4})", text)[1:]:
        ds = dates_in(p.split("Court Date")[0], today)
        nm = re.search(r"Name:\s*(.+?)\s+D\.O\.B\.", p, re.S)
        charges = [clean_charge(m[2]) for m in re.finditer(r"^\$[\d,]*\.\d\d(\S+)\s+(.+)$", p, re.M)]
        if ds and nm:
            out.append({"name": clean_name(nm[1]), "date": min(ds), "charges": uniq(charges)})
    return out


PARSERS = {"blocks": parse_blocks, "middlebury": parse_middlebury}


def key(e):
    raw = "|".join([e["town"], e["name"], e["date"], ";".join(e["charges"])])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def main():
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=WINDOW_DAYS)
    try:
        old = json.load(open(OUT))
    except Exception:
        old = {"arrests": []}
    merged = {key(e): e for e in old.get("arrests", [])}
    status = {}

    for cfg in TOWNS:
        t = cfg["town"]
        st = {"status": "ok", "logs_read": 0, "latest": None, "note": ""}
        status[t] = st
        if not cfg["index"]:
            st.update(status="not set up", note="No log page URL configured.")
            continue
        try:
            logs = find_logs(cfg)
        except Exception as ex:
            st.update(status="error", note=f"Could not load log page: {ex}")
            continue
        found = 0
        for url in logs:
            try:
                text = pdf_text(get(url).content)
                for e in PARSERS[cfg["parser"]](text, today):
                    if e["date"] < cutoff:
                        continue
                    rec = {"town": t, "name": e["name"], "date": e["date"].isoformat(),
                           "charges": e["charges"], "source": url}
                    merged[key(rec)] = rec
                    found += 1
                st["logs_read"] += 1
            except Exception as ex:
                st["note"] = f"Skipped a log: {ex}"
        if not logs:
            st.update(status="warning", note="Found no PDF links on the log page.")
        elif found == 0 and st["logs_read"]:
            st.update(status="warning", note=(st["note"] + " No arrests parsed; no arrests in the window, or the PDF layout changed.").strip())

    arrests = sorted((e for e in merged.values() if e["date"] >= cutoff.isoformat()),
                     key=lambda e: (e["date"], e["town"], e["name"]), reverse=True)
    for e in arrests:
        s = status.get(e["town"])
        if s and (s["latest"] is None or e["date"] > s["latest"]):
            s["latest"] = e["date"]
    json.dump({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "window_days": WINDOW_DAYS, "arrests": arrests, "towns": status},
              open(OUT, "w"), indent=1)
    for t, s in status.items():
        print(f"{t}: {s['status']} ({s['logs_read']} logs) {s['note']}")


if __name__ == "__main__":
    main()
