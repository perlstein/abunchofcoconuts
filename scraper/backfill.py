"""One-time best-effort backfill of history.json from the Wayback Machine.

Queries the Wayback CDX API for archived snapshots of each category's
top-100 chart, groups snapshots by date, rebuilds the aggregate platform
rankings for each date using the same parsing and host detection as the
live scraper, and appends entries for dates not already in history.json.

Host detection uses TODAY's feed lookups and host table (the iTunes
lookup API has no time machine), so old entries reflect historical chart
membership but current hosting. Good enough to seed the bump chart.

Run once: python scraper/backfill.py
"""

import json
import sys
import time
from datetime import datetime, timezone

import requests

from scrape import (CATEGORIES, CHART_URL, HEADERS, TIMEOUT, HISTORY_PATH,
                    build_history_entry, build_leaderboard, compact_history,
                    load_history, log, parse_chart_entries, resolve_hosts)

CDX_URL = ("http://web.archive.org/cdx/search/cdx?url={chart_url}"
           "&output=json&limit=100&fl=timestamp,original")
ARCHIVE_URL = "https://web.archive.org/web/{timestamp}/{original}"
SLEEP = 2  # be polite to the Wayback Machine
# A date needs at least this many archived category charts to produce a
# meaningful aggregate entry; fewer would skew ranks badly.
MIN_CATEGORIES_PER_DATE = 3


def wayback_get(url: str) -> "requests.Response | None":
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log(f"WARNING: wayback request failed: {exc}")
        return None


def find_snapshots() -> dict:
    """Return {date: {category: (timestamp, original_url)}} from the CDX API."""
    by_date = {}
    for category, genre_id in CATEGORIES.items():
        chart_url = CHART_URL.format(genre_id=genre_id)
        log(f"CDX query for {category}...")
        resp = wayback_get(CDX_URL.format(chart_url=chart_url))
        time.sleep(SLEEP)
        if resp is None:
            continue
        try:
            rows = resp.json()
        except ValueError:
            log(f"WARNING: bad CDX JSON for {category}")
            continue
        for ts, original in rows[1:]:  # row 0 is the header
            date = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
            # Keep the last snapshot per category per date
            by_date.setdefault(date, {})[category] = (ts, original)
    return by_date


def fetch_archived_chart(timestamp: str, original: str) -> list:
    """Fetch one archived chart and return parsed entries (or [])."""
    resp = wayback_get(ARCHIVE_URL.format(timestamp=timestamp, original=original))
    time.sleep(SLEEP)
    if resp is None:
        return []
    try:
        return parse_chart_entries(resp.json())
    except ValueError as exc:
        log(f"WARNING: unparseable archived chart ({exc})")
        return []


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history = load_history()
    existing_dates = {e["date"] for e in history["entries"]}
    log(f"history.json has {len(existing_dates)} entries; querying Wayback CDX...")

    snapshots = find_snapshots()
    candidates = {
        date: cats for date, cats in sorted(snapshots.items())
        if date not in existing_dates and date != today
        and len(cats) >= MIN_CATEGORIES_PER_DATE
    }
    skipped = {d: len(c) for d, c in snapshots.items() if d not in candidates}
    log(f"Found snapshots on {len(snapshots)} dates; "
        f"{len(candidates)} usable (>= {MIN_CATEGORIES_PER_DATE} categories, not already in history)")
    if skipped:
        log(f"Skipped dates (already present / too few categories): {skipped}")

    host_cache = {}  # itunes_id -> (feed_url, host), shared across dates
    added = 0
    for date, cats in candidates.items():
        log(f"--- Backfilling {date} ({len(cats)} categories archived) ---")
        shows = {}
        for category, (ts, original) in cats.items():
            log(f"Fetching archived {category} chart from {ts}...")
            for itunes_id, title, artwork in fetch_archived_chart(ts, original):
                show = shows.setdefault(itunes_id, {
                    "title": title, "itunes_id": itunes_id, "artwork": artwork,
                    "categories": [], "feed_url": None, "host": None,
                })
                if category not in show["categories"]:
                    show["categories"].append(category)
        if not shows:
            log(f"No shows recovered for {date}, skipping")
            continue
        log(f"{date}: {len(shows)} unique shows, resolving hosts "
            f"({sum(1 for s in shows if s in host_cache)} cached)...")
        resolve_hosts(shows, host_cache)
        entry = build_history_entry(build_leaderboard(shows, date))
        history = load_history()
        if any(e["date"] == date for e in history["entries"]):
            log(f"{date} appeared in history while running, skipping")
            continue
        history["entries"].append(entry)
        history["entries"] = compact_history(history["entries"])
        history["first_entry"] = history["entries"][0]["date"]
        history["last_entry"] = history["entries"][-1]["date"]
        HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False))
        added += 1
        log(f"Appended entry for {date} ({entry['total_shows']} shows)")

    log(f"Backfill done: {added} entries added, history now has "
        f"{len(load_history()['entries'])} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
