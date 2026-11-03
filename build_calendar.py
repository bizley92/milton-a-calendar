#!/usr/bin/env python3
"""Rebuild docs/fixtures.ics from Milton A's FA Full-Time team page.

Run by the daily GitHub Actions workflow. It reads the "Upcoming Fixtures" table
with a headless browser, merges it with the fixtures already published (played games
are kept, moved games are updated, withdrawn games are dropped) and writes:

  docs/fixtures.ics   the calendar you subscribe to
  docs/fixtures.json  the merged fixture list, used as state for the next run

If the page can't be read, nothing is overwritten and the run fails, so GitHub
emails you and the calendar keeps its last good version.

Local use:
  python build_calendar.py                      # fetch from Full-Time
  python build_calendar.py --from-text FILE     # parse a pasted Upcoming Fixtures list
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TEAM = "Milton A"
# Team page for this season's division (divisionseason changes each season - update it in August),
# plus the league's fixture list as a second source. Fixtures from both are combined.
TEAM_URL = "https://fulltime.thefa.com/displayTeam.html?divisionseason=294450662&teamID=695509665"
LEAGUE_FIXTURES_URL = "https://fulltime.thefa.com/fixtures.html?league=7984801"
SOURCES = (TEAM_URL, LEAGUE_FIXTURES_URL)
CAL_NAME = "Milton A fixtures"
UID_DOMAIN = "milton-a-calendar"

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ICS_PATH = DOCS / "fixtures.ics"
STATE_PATH = DOCS / "fixtures.json"
VENUES_PATH = ROOT / "venues.json"

LDN = ZoneInfo("Europe/London")
MEET_BEFORE = timedelta(hours=1)      # meet-up time before kick-off
EVENT_AFTER_KO = timedelta(hours=2)   # event ends this long after kick-off
ALARMS = (("-P1D", "Tomorrow"), ("-PT1H", "In 1 hour"))  # relative to the meet-up time

DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2})(?:\s+(\d{1,2}):(\d{2}))?")
PITCH_RE = re.compile(r"\s+#?(\d+)$")
NO_FIXTURES_TEXT = "There are currently no fixtures to show"
MAX_REMOVALS = 3  # more future games than this vanishing in one run = treat as a misread, not a real change


# --------------------------------------------------------------------------- fetching

# Returns every table row that looks like an unplayed fixture: it has a dd/mm/yy date
# and a "VS" cell (results have a score instead).
EXTRACT_ROWS_JS = r"""
() => {
  const dateRe = /\b\d{2}\/\d{2}\/\d{2}\b/;
  const rows = [], dated = [];
  for (const tr of document.querySelectorAll('tr')) {
    const cells = Array.from(tr.querySelectorAll('td,th'))
      .map(c => (c.innerText || '').replace(/\s+/g, ' ').trim());
    if (!cells.some(c => dateRe.test(c))) continue;
    if (dated.length < 40) dated.push(cells);
    if (cells.some(c => /^vs$/i.test(c))) rows.push(cells);
  }
  return {rows, dated};
}
"""

HAS_FIXTURES_JS = r"""
() => /\d{2}\/\d{2}\/\d{2}[^\n]*\bVS\b/i.test(document.body.innerText)
      || document.body.innerText.includes('There are currently no fixtures to show')
"""


def fetch_pages(urls) -> list[dict]:
    """Load each page in headless Chromium; return per page: url, fixture rows, dated rows (debug), text, error."""
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        )
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_function(HAS_FIXTURES_JS, timeout=30_000)
                except Exception:
                    pass  # judged later from whatever loaded
                found = page.evaluate(EXTRACT_ROWS_JS)
                text = page.evaluate("document.body.innerText")
                results.append({"url": url, "rows": found["rows"], "dated": found["dated"], "text": text, "error": None})
            except Exception as e:  # one bad page shouldn't stop the other source
                results.append({"url": url, "rows": [], "dated": [], "text": "", "error": str(e)[:300]})
        browser.close()
    return results


def rows_from_text(text: str) -> list[list[str]]:
    """Fallback / paste parser: tab-separated lines with a date and a VS cell."""
    rows = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        cells = [c.strip() for c in line.split("\t")]
        if any(DATE_RE.search(c) for c in cells) and any(c.upper() == "VS" for c in cells):
            rows.append(cells)
    return rows


# --------------------------------------------------------------------------- parsing

def parse_row(cells: list[str]) -> dict | None:
    """Turn one fixture row into a dict. Layout on Full-Time:
    Type | Date/Time | Home | (Home) | VS | (Away) | Away | Venue [| Competition]  (name cells may be blank)."""
    di = next((i for i, c in enumerate(cells) if DATE_RE.search(c)), None)
    if di is None:
        return None
    vi = next((i for i in range(di + 1, len(cells)) if cells[i].upper() == "VS"), None)
    if vi is None:
        return None
    dd, mm, yy, hh, mi = DATE_RE.search(cells[di]).groups()
    home = next((c for c in cells[di + 1:vi] if c), "")
    tail = [c for c in cells[vi + 1:] if c]
    away = tail[0] if tail else ""
    # venue is the first cell after the (possibly repeated) away name; anything after it is e.g. the competition
    last_away = max(i for i, c in enumerate(tail) if c == away) if tail else 0
    venue = tail[last_away + 1] if len(tail) > last_away + 1 else ""
    if not home or not away:
        return None
    typ = cells[0] if di > 0 and cells[0] else "L"
    return {
        "type": typ,
        "date": f"20{yy}-{mm}-{dd}",
        "time": f"{int(hh):02d}:{mi}" if hh else None,
        "home": home,
        "away": away,
        "venue": venue,
    }


def season_of(date_str: str) -> str:
    y, m = int(date_str[:4]), int(date_str[5:7])
    start = y if m >= 7 else y - 1
    return f"{start}-{str(start + 1)[-2:]}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fixture_uid(f: dict) -> str:
    """Stable per fixture, not per date, so a rescheduled game moves instead of duplicating."""
    is_home = f["home"] == TEAM
    opp = f["away"] if is_home else f["home"]
    return f"{season_of(f['date'])}-{slug(f['type'])}-{'h' if is_home else 'a'}-{slug(opp)}@{UID_DOMAIN}"


def kickoff(f: dict) -> datetime:
    d = datetime.strptime(f["date"], "%Y-%m-%d")
    if f["time"]:
        h, m = map(int, f["time"].split(":"))
        d = d.replace(hour=h, minute=m)
    return d.replace(tzinfo=LDN)


# --------------------------------------------------------------------------- merging

CONTENT_KEYS = ("type", "date", "time", "home", "away", "venue")


def merge(old: dict[str, dict], upcoming: list[dict], now: datetime) -> tuple[dict[str, dict], list[str]]:
    """Upcoming fixtures replace what we had; games already played are kept as history."""
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    merged, log = {}, []
    for f in upcoming:
        uid = fixture_uid(f)
        prev = old.get(uid)
        if prev and all(prev.get(k) == f.get(k) for k in CONTENT_KEYS):
            f["stamp"] = prev["stamp"]
        else:
            f["stamp"] = stamp
            if prev:
                log.append(f"changed: {prev['date']} {prev['time']} {prev['venue']} -> {f['date']} {f['time']} {f['venue']} ({uid})")
            else:
                log.append(f"new: {f['date']} {f['home']} v {f['away']}")
        merged[uid] = f
    for uid, f in old.items():
        if uid in merged:
            continue
        if kickoff(f) < now:
            merged[uid] = f  # played: keep in the calendar
        else:
            log.append(f"removed: {f['date']} {f['home']} v {f['away']}")
    return merged, log


# --------------------------------------------------------------------------- calendar

def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > (75 if not out else 74):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(fixtures: dict[str, dict], venues: dict) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{UID_DOMAIN}//EN", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", f"X-WR-CALNAME:{CAL_NAME}", "X-WR-TIMEZONE:Europe/London",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H", "X-PUBLISHED-TTL:PT12H",
    ]
    for uid, f in sorted(fixtures.items(), key=lambda kv: (kickoff(kv[1]), kv[0])):
        is_home = f["home"] == TEAM
        opp = f["away"] if is_home else f["home"]
        comp = "League" if f["type"] == "L" else f"Cup ({f['type']})"
        ko = kickoff(f)

        venue_raw = f["venue"] or ""
        m = PITCH_RE.search(venue_raw)
        pitch = m.group(1) if m else None
        ground = PITCH_RE.sub("", venue_raw).strip()
        info = venues.get(ground.upper())

        title = f"{f['home']} v {f['away']} ({'H' if is_home else 'A'})"
        if f["type"] != "L":
            title += f" - {comp}"
        notes = []
        if f["time"]:
            meet = ko - MEET_BEFORE
            title += f" - KO {f['time']}"
            notes.append(f"Meet {meet:%H:%M}, kick-off {f['time']}.")
            when = [f"DTSTART:{utc(meet)}", f"DTEND:{utc(ko + EVENT_AFTER_KO)}"]
        else:
            title += " - KO TBC"
            notes.append("Kick-off time to be confirmed.")
            day = datetime.strptime(f["date"], "%Y-%m-%d")
            when = [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
        notes.append(f"{comp}, {'home' if is_home else 'away'} v {opp}.")
        if pitch:
            notes.append(f"Pitch {pitch}")
        notes.append(f"Full-Time: {TEAM_URL}")

        ev = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{f['stamp']}", f"LAST-MODIFIED:{f['stamp']}",
              *when, f"SUMMARY:{esc(title)}"]
        if info:
            # Apple's own format: place name + newline + address, plus the hidden map pin
            ev.append(f"LOCATION:{esc(info['title'] + chr(10) + info['address'])}")
            ev.append(f'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="{info["address"]}";'
                      f'X-APPLE-RADIUS=100;X-TITLE="{info["title"]}":geo:{info["lat"]:.6f},{info["lon"]:.6f}')
        else:
            ev.append(f"LOCATION:{esc(ground.title() if ground.isupper() else ground or 'Venue TBC')}")
        ev.append(f"DESCRIPTION:{esc(chr(10).join(notes))}")
        for trigger, label in ALARMS:
            ev += ["BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER;RELATED=START:{trigger}",
                   f"DESCRIPTION:{esc(f'{label}: {title}')}", "END:VALARM"]
        ev.append("END:VEVENT")
        lines += ev
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-text", type=Path, help="parse a pasted Upcoming Fixtures list instead of fetching")
    ap.add_argument("--now", help="override the current time (ISO), for testing")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now).astimezone(LDN) if args.now else datetime.now(LDN)

    if args.from_text:
        pages = [{"url": str(args.from_text), "rows": [], "dated": [], "text": args.from_text.read_text(), "error": None}]
    else:
        pages = fetch_pages(SOURCES)

    # Each page is read two ways (table cells and page text); everything is combined, team page first,
    # so one layout quirk or one unreadable page can't drop games.
    by_uid: dict[str, dict] = {}
    no_fixtures = False
    for pg in pages:
        if pg["error"]:
            print(f"WARNING: couldn't load {pg['url']}: {pg['error']}")
            continue
        text_rows = rows_from_text(pg["text"])
        dom_fx = [f for f in map(parse_row, pg["rows"]) if f and TEAM in (f["home"], f["away"])]
        text_fx = [f for f in map(parse_row, text_rows) if f and TEAM in (f["home"], f["away"])]
        before = len(by_uid)
        for f in dom_fx + text_fx:
            uid = fixture_uid(f)
            if uid not in by_uid or (f["venue"] and not by_uid[uid]["venue"]):
                by_uid[uid] = f
        print(f"{pg['url']}: {len(dom_fx)} Milton A fixtures from table cells, {len(text_fx)} from page text, "
              f"{len(by_uid) - before} not already found.")
        if pg["url"] == TEAM_URL or args.from_text:
            no_fixtures = no_fixtures or NO_FIXTURES_TEXT in pg["text"]
        if not args.from_text and pg["url"] == TEAM_URL and TEAM not in pg["text"]:
            print("WARNING: the team page didn't mention Milton A (blocked or changed). Start of page:")
            print("   ", pg["text"][:300].replace("\n", " | "))
        if pg["dated"] and len(dom_fx) != len(text_fx):
            print("  Diagnostics - dated table rows as seen by the browser:")
            for r in pg["dated"]:
                print("     ", r)
        for r in [r for r in pg["rows"] + text_rows if not parse_row(r)][:10]:
            print("  could not read row:", r)
    upcoming = list(by_uid.values())
    if not upcoming and not no_fixtures:
        print("ERROR: found no fixtures and no 'no fixtures' message; page layout may have changed. "
              "Keeping the previous calendar.", file=sys.stderr)
        return 1

    old = json.loads(STATE_PATH.read_text())["fixtures"] if STATE_PATH.exists() else {}
    merged, log = merge(old, upcoming, now)
    removed = [l for l in log if l.startswith("removed:")]
    if len(removed) > MAX_REMOVALS and not os.environ.get("ALLOW_BIG_CHANGES"):
        print(f"ERROR: this run would remove {len(removed)} future games at once, which looks like a misread "
              "rather than real fixture changes. Keeping the previous calendar. "
              "(Set ALLOW_BIG_CHANGES=1 to accept.)", file=sys.stderr)
        for line in removed:
            print(" ", line, file=sys.stderr)
        return 1
    venues = {k: v for k, v in json.loads(VENUES_PATH.read_text()).items() if not k.startswith("_")}

    DOCS.mkdir(exist_ok=True)
    ICS_PATH.write_text(build_ics(merged, venues), newline="")
    STATE_PATH.write_text(json.dumps({
        "team": TEAM, "sources": list(SOURCES),
        "last_checked": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixtures": dict(sorted(merged.items(), key=lambda kv: kickoff(kv[1]))),
    }, indent=2) + "\n")

    unknown = sorted({PITCH_RE.sub("", f["venue"]).strip() for f in upcoming
                      if f["venue"] and PITCH_RE.sub("", f["venue"]).strip().upper() not in venues})
    print(f"{len(upcoming)} upcoming fixtures read, {len(merged)} in calendar.")
    for line in log:
        print(" ", line)
    if unknown:
        print("Venues without an address in venues.json (shown by name only):", ", ".join(unknown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
