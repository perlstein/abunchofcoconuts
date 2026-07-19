"""Discover shows from Apple Podcasts sub-genre charts -> data/subgenre_feeds.csv.

Apple's top-level genres each have sub-genres (Business -> Investing, Society &
Culture -> Relationships, ...), and every sub-genre has its own top-100 chart at
the same endpoint the main scraper already uses. This pulls those charts, keeps
only shows we are not already tracking, resolves each to an RSS feed, and writes
them out as SEED feeds for the cumulative tracker.

Important: these are seeds for the *rolling re-check* corpus, NOT the always-
checked watchlist. cumulative.py adds them with no last_checked so they enter
the stale-rotation and get host-checked over the next few runs, which keeps the
per-run cost flat no matter how many we add (see the sustainability math).

Bogus sub-genre ids are harmless: an unknown id tends to echo the overall top
chart, whose shows we already track, so dedup drops them.

Re-run occasionally to pick up newly-charting shows: python scraper/subgenres.py
"""

import csv
import json
import sys
import time

import requests

from scrape import (CHART_URL, HEADERS, LEADERBOARD_PATH, ROOT, TIMEOUT,
                    fetch_with_retries, log, parse_chart_entries)

OUTPUT_PATH = ROOT / "data" / "subgenre_feeds.csv"
TRACKED_PATH = ROOT / "data" / "tracked_feeds.json"
# iTunes throttles the chart endpoint cumulatively: too many calls in a window
# and it returns near-empty charts. The window clears with time, so we stay
# under it (a short pause per call, a cooldown every CHUNK charts) and treat a
# degraded chart as a throttle signal: back off and retry.
CHART_PAUSE = 3.0
CHART_CHUNK = 15          # charts between cooldowns (the 19-chart main scrape is safe)
CHART_COOLDOWN = 30.0
CHART_MIN_HEALTHY = 40    # fewer entries than this == throttled, retry
CHART_TRIES = 3
CHART_BACKOFF = 30.0
LOOKUP_PAUSE = 0.3
LOOKUP_COOLDOWN_EVERY = 200
LOOKUP_COOLDOWN = 20.0


def collect_subgenre_shows() -> dict:
    """Fetch each sub-genre chart, pacing and backing off around iTunes
    throttling, and return unique shows keyed by iTunes id."""
    shows = {}
    for i, (gid, label) in enumerate(SUBGENRES.items(), 1):
        ents = []
        for attempt in range(CHART_TRIES):
            resp = fetch_with_retries(CHART_URL.format(genre_id=gid))
            ents = parse_chart_entries(resp.json()) if resp else []
            if len(ents) >= CHART_MIN_HEALTHY:
                break
            log(f"subgenres: {label} returned {len(ents)} (throttled?), backing off")
            time.sleep(CHART_BACKOFF)
        for sid, title, artwork in ents:
            shows.setdefault(sid, {"itunes_id": sid, "title": title})
        log(f"subgenres: {i}/{len(SUBGENRES)} {label}: {len(ents)} (corpus {len(shows)})")
        time.sleep(CHART_PAUSE)
        if i % CHART_CHUNK == 0 and i < len(SUBGENRES):
            time.sleep(CHART_COOLDOWN)
    return shows

# Apple Podcasts sub-genre ids (current taxonomy), label "Parent / Sub". Any id
# that doesn't return a real sub-chart is filtered out downstream by dedup.
SUBGENRES = {
    1482: "Arts / Books", 1402: "Arts / Design", 1459: "Arts / Fashion & Beauty",
    1306: "Arts / Food", 1405: "Arts / Performing Arts", 1406: "Arts / Visual Arts",
    1410: "Business / Careers", 1493: "Business / Entrepreneurship",
    1412: "Business / Investing", 1491: "Business / Management",
    1492: "Business / Marketing", 1494: "Business / Non-Profit",
    1495: "Comedy / Comedy Interviews", 1496: "Comedy / Improv", 1497: "Comedy / Stand-Up",
    1498: "Education / Courses", 1499: "Education / How To",
    1500: "Education / Language Learning", 1501: "Education / Self-Improvement",
    1485: "Fiction / Comedy Fiction", 1486: "Fiction / Drama", 1480: "Fiction / Science Fiction",
    1513: "Health & Fitness / Alternative Health", 1514: "Health & Fitness / Fitness",
    1515: "Health & Fitness / Medicine", 1516: "Health & Fitness / Mental Health",
    1517: "Health & Fitness / Nutrition", 1518: "Health & Fitness / Sexuality",
    1521: "Kids & Family / Education for Kids", 1522: "Kids & Family / Parenting",
    1523: "Kids & Family / Pets & Animals", 1524: "Kids & Family / Stories for Kids",
    1503: "Leisure / Animation & Manga", 1504: "Leisure / Automotive",
    1505: "Leisure / Aviation", 1506: "Leisure / Crafts", 1507: "Leisure / Games",
    1508: "Leisure / Hobbies", 1509: "Leisure / Home & Garden", 1510: "Leisure / Video Games",
    1490: "News / Business News", 1525: "News / Daily News",
    1526: "News / Entertainment News", 1527: "News / News Commentary",
    1528: "News / Politics", 1529: "News / Sports News", 1530: "News / Tech News",
    1439: "Religion & Spirituality / Buddhism", 1440: "Religion & Spirituality / Christianity",
    1441: "Religion & Spirituality / Hinduism", 1442: "Religion & Spirituality / Islam",
    1443: "Religion & Spirituality / Judaism", 1444: "Religion & Spirituality / Religion",
    1445: "Religion & Spirituality / Spirituality",
    1532: "Science / Astronomy", 1533: "Science / Chemistry", 1534: "Science / Earth Sciences",
    1535: "Science / Life Sciences", 1536: "Science / Mathematics",
    1537: "Science / Natural Sciences", 1538: "Science / Nature", 1539: "Science / Physics",
    1540: "Science / Social Sciences",
    1302: "Society & Culture / Documentary", 1320: "Society & Culture / Personal Journals",
    1448: "Society & Culture / Philosophy", 1450: "Society & Culture / Places & Travel",
    1451: "Society & Culture / Relationships",
    1546: "Sports / Baseball", 1547: "Sports / Basketball", 1548: "Sports / Cricket",
    1549: "Sports / Fantasy Sports", 1550: "Sports / Football", 1551: "Sports / Golf",
    1552: "Sports / Hockey", 1553: "Sports / Rugby", 1554: "Sports / Running",
    1555: "Sports / Soccer", 1556: "Sports / Swimming", 1557: "Sports / Tennis",
    1558: "Sports / Volleyball", 1559: "Sports / Wilderness", 1560: "Sports / Wrestling",
    1561: "TV & Film / After Shows", 1562: "TV & Film / Film History",
    1563: "TV & Film / Film Interviews", 1564: "TV & Film / Film Reviews",
    1565: "TV & Film / TV Reviews",
}


def already_tracked_feed_urls() -> set:
    try:
        return set(json.loads(TRACKED_PATH.read_text()).keys())
    except Exception:
        return set()


def leaderboard_itunes_ids() -> set:
    """iTunes ids already in the main Apple board (already tracked via charts)."""
    try:
        d = json.loads(LEADERBOARD_PATH.read_text())
    except Exception:
        return set()
    ids = set()
    for p in d.get("platforms", []):
        for s in p.get("shows", []):
            if s.get("itunes_id"):
                ids.add(str(s["itunes_id"]))
    return ids


def _lookup(sid):
    """Resolve one show id to (feed_url, title), with a single retry on an empty
    response (transient iTunes throttling)."""
    for attempt in range(2):
        resp = requests.get(
            f"https://itunes.apple.com/lookup?id={sid}&entity=podcast",
            headers=HEADERS, timeout=TIMEOUT)
        try:
            res = resp.json().get("results", [])
        except ValueError:
            res = []
        if res:
            return res[0].get("feedUrl"), res[0].get("collectionName") or ""
        time.sleep(1.5)
    return None, ""


def main() -> int:
    shows = collect_subgenre_shows()
    log(f"subgenres: {len(shows)} distinct shows across {len(SUBGENRES)} sub-genres")

    known_ids = leaderboard_itunes_ids()
    new_shows = {sid: s for sid, s in shows.items() if str(sid) not in known_ids}
    log(f"subgenres: {len(new_shows)} are new (not already on the main Apple board)")

    have_feeds = already_tracked_feed_urls()
    rows = {}  # feed_url -> label(title)
    for i, (sid, s) in enumerate(new_shows.items(), 1):
        feed, coll = _lookup(sid)
        time.sleep(LOOKUP_PAUSE)
        if i % LOOKUP_COOLDOWN_EVERY == 0:
            log(f"subgenres: resolved {i}/{len(new_shows)} ({len(rows)} new feeds), cooldown")
            time.sleep(LOOKUP_COOLDOWN)
        if not feed or feed in have_feeds or feed in rows:
            continue
        rows[feed] = s.get("title") or coll

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["feed_url", "label"])
        for feed, label in sorted(rows.items()):
            w.writerow([feed, label])
    log(f"subgenres: wrote {len(rows)} new seed feeds to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never block the pipeline
        log(f"subgenres: unexpected error, skipping: {exc}")
        sys.exit(0)
