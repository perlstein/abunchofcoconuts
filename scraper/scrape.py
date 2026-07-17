"""Scrape Apple Podcasts top charts, identify hosting platforms, track history.

Pipeline:
  1. Fetch the top 100 chart for all 19 categories (iTunes RSS API).
  2. Deduplicate shows by iTunes ID, tracking category membership.
  3. Resolve each show's RSS feed URL via the iTunes lookup API.
  4. Detect the hosting platform from the feed URL domain, falling back
     to fetching the feed and inspecting the first media enclosure URL.
  5. Write data/leaderboard.json (current snapshot with show lists).
  6. Append today's aggregated rankings to data/history.json and compact
     old entries (daily for 90 days, weekly to 1 year, monthly beyond).
"""

import calendar
import json
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import requests

from hosts import UNMATCHED, detect_host

CATEGORIES = {
    "Arts": 1301,
    "Business": 1321,
    "Comedy": 1303,
    "Education": 1304,
    "Fiction": 1483,
    "Government": 1511,
    "Health & Fitness": 1307,
    "History": 1487,
    "Kids & Family": 1305,
    "Leisure": 1502,
    "Music": 1310,
    "News": 1489,
    "Religion & Spirituality": 1314,
    "Science": 1315,
    "Society & Culture": 1324,
    "Sports": 1545,
    "Technology": 1318,
    "True Crime": 1488,
    "TV & Film": 1309,
}

CHART_URL = "https://itunes.apple.com/us/rss/toppodcasts/limit=100/genre={genre_id}/json"
LOOKUP_URL = "https://itunes.apple.com/lookup?id={itunes_id}&entity=podcast"
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 10
MIN_SHOWS = 500

ROOT = Path(__file__).resolve().parent.parent
LEADERBOARD_PATH = ROOT / "data" / "leaderboard.json"
HISTORY_PATH = ROOT / "data" / "history.json"
SHOW_HISTORY_PATH = ROOT / "data" / "show_history.json"
HOST_STATE_PATH = ROOT / "data" / "host_state.json"
APPLE_VIDEO_STATE_PATH = ROOT / "data" / "apple_video_state.json"  # built by apple_video.py

# Retention windows for compact_history()
DAILY_WINDOW_DAYS = 90
WEEKLY_WINDOW_DAYS = 365


def log(message: str) -> None:
    print(message, flush=True)


def fetch_with_retries(url: str, retries: int = 3) -> "requests.Response | None":
    """GET a URL, retrying up to `retries` times with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt == retries:
                log(f"WARNING: giving up on {url}: {exc}")
                return None
            wait = 2 ** attempt
            log(f"WARNING: request failed ({exc}), retrying in {wait}s...")
            time.sleep(wait)
    return None


def parse_chart_entries(payload: dict) -> list:
    """Parse one chart response into [(itunes_id, title, artwork)] tuples."""
    entries = payload.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):  # single-entry charts arrive as an object
        entries = [entries]
    parsed = []
    for entry in entries:
        try:
            itunes_id = entry["id"]["attributes"]["im:id"]
            title = entry["im:name"]["label"]
        except (KeyError, TypeError):
            continue
        images = entry.get("im:image", [])
        artwork = ""
        if len(images) >= 3:
            artwork = images[2].get("label", "")
        elif images:
            artwork = images[-1].get("label", "")
        parsed.append((itunes_id, title, artwork))
    return parsed


def fetch_charts(categories: dict, pause: float = 0.0) -> dict:
    """Fetch every category chart and return unique shows keyed by iTunes ID.

    `pause` adds a delay between charts; iTunes throttles many rapid calls and
    starts returning degraded (near-empty) charts. The main scrape (19 charts)
    is fine at 0, but large sweeps (sub-genres) should pace themselves.
    """
    shows: dict = {}
    for i, (category, genre_id) in enumerate(categories.items(), 1):
        if pause and i > 1:
            time.sleep(pause)
        log(f"Fetching category {i}/{len(categories)}: {category}")
        resp = fetch_with_retries(CHART_URL.format(genre_id=genre_id))
        if resp is None:
            log(f"ERROR: could not fetch chart for {category}, skipping")
            continue
        try:
            entries = parse_chart_entries(resp.json())
        except ValueError as exc:
            log(f"ERROR: bad JSON for {category} chart ({exc}), skipping")
            continue
        # The chart is an ordered top-100; the position IS the within-category
        # rank. We kept discarding it; capturing it costs nothing (no extra HTTP).
        for rank, (itunes_id, title, artwork) in enumerate(entries, 1):
            show = shows.setdefault(itunes_id, {
                "title": title,
                "itunes_id": itunes_id,
                "artwork": artwork,
                "categories": [],
                "ranks": {},
                "feed_url": None,
                "host": None,
            })
            if category not in show["categories"]:
                show["categories"].append(category)
            show["ranks"][category] = rank
    return shows


def lookup_feed_url(itunes_id: str) -> "str | None":
    """Resolve a show's RSS feed URL from its iTunes ID."""
    resp = fetch_with_retries(LOOKUP_URL.format(itunes_id=itunes_id))
    if resp is None:
        return None
    try:
        results = resp.json().get("results", [])
    except ValueError:
        return None
    return results[0].get("feedUrl") if results else None


def lookup_show(itunes_id: str) -> dict:
    """One iTunes lookup -> feed URL plus the activity/maturity fields it already
    returns for free: episode count (trackCount) and the latest-episode date
    (releaseDate, normalized to YYYY-MM-DD)."""
    resp = fetch_with_retries(LOOKUP_URL.format(itunes_id=itunes_id))
    if resp is None:
        return {}
    try:
        results = resp.json().get("results", [])
    except ValueError:
        return {}
    if not results:
        return {}
    r = results[0]
    last = r.get("releaseDate")
    return {
        "feed_url": r.get("feedUrl"),
        "episode_count": r.get("trackCount"),
        "last_published": last[:10] if last else None,
    }


EMPTY_META = {
    "media_url": None, "owner_name": None, "owner_email": None,
    "itunes_author": None, "website": None, "description": None,
    "cadence": None, "episode_count": None, "last_published": None,
    "schedule": None, "rss_video": None,
}


def _cadence_label(days: float) -> str:
    """Human label for the typical gap (in days) between episodes."""
    if days <= 1.6:
        return "Daily"
    if days <= 4.5:
        return f"~{round(7 / days)}×/week"
    if days <= 10:
        return "Weekly"
    if days <= 18:
        return "Biweekly"
    if days <= 45:
        return "Monthly"
    return "Occasional"


def episode_cadence(parsed) -> "str | None":
    """Publish frequency from the gaps between recent episodes.

    Uses the median gap across up to the 12 most recent items, so one long
    hiatus doesn't skew the label. This is a stable property of a feed (unlike
    "days since last episode"), so it's safe to cache. None if too few dated
    episodes to judge.
    """
    stamps = []
    for entry in parsed.entries[:12]:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            stamps.append(calendar.timegm(t))
    if len(stamps) < 3:
        return None
    stamps.sort(reverse=True)
    gaps = sorted(g for g in ((stamps[i] - stamps[i + 1]) / 86400.0
                              for i in range(len(stamps) - 1)) if g > 0)
    if not gaps:
        return None
    return _cadence_label(gaps[len(gaps) // 2])


WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_PLURAL = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays",
                  "Fridays", "Saturdays", "Sundays")


def publishing_schedule(parsed) -> "dict | None":
    """Weekday histogram + human schedule label from recent episode dates.

    {"days": [Mon..Sun counts], "label": "Weekly · Tues", "eps_year": 52,
     "trend": "steady"|"accelerating"|"slowing"|None}. Sampled from up to the
    24 most recent items so it reflects the show's current rhythm; None when
    too few dated episodes to judge.
    """
    stamps = []
    for entry in parsed.entries[:24]:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            stamps.append(t)
    if len(stamps) < 3:
        return None
    days = [0] * 7
    for t in stamps:
        days[t.tm_wday] += 1
    epochs = sorted((calendar.timegm(t) for t in stamps), reverse=True)
    gaps = [g for g in ((epochs[i] - epochs[i + 1]) / 86400.0
                        for i in range(len(epochs) - 1)) if g > 0]
    if not gaps:
        return None
    med = sorted(gaps)[len(gaps) // 2]
    cadence = _cadence_label(med)
    eps_year = min(730, max(1, round(365 / max(med, 0.5))))
    # name the dominant weekday(s) when they cover most recent episodes
    total = sum(days)
    order = sorted(range(7), key=lambda d: -days[d])
    label = cadence
    if days[order[0]] / total >= 0.55:
        label = f"{cadence} · {WEEKDAY_PLURAL[order[0]]}"
    elif days[order[1]] and (days[order[0]] + days[order[1]]) / total >= 0.7:
        label = f"{cadence} · {WEEKDAY_ABBR[order[0]]} & {WEEKDAY_ABBR[order[1]]}"
    # trend: median gap of the recent half vs the older half (gaps are newest-first)
    trend = None
    if len(gaps) >= 6:
        half = len(gaps) // 2
        recent = sorted(gaps[:half])[half // 2]
        older_sorted = sorted(gaps[half:])
        older = older_sorted[len(older_sorted) // 2]
        trend = ("accelerating" if recent < older * 0.7
                 else "slowing" if recent > older * 1.4 else "steady")
    return {"days": days, "label": label, "eps_year": eps_year, "trend": trend}


def rss_video_kind(parsed) -> "str | None":
    """'hls' or 'mp4' when recent episodes carry video enclosures, else None.
    Rare: most video shows deliver video to platforms directly and keep their
    public RSS audio-only (verified on four known Apple-video shows)."""
    video = None
    for entry in parsed.entries[:5]:
        for enc in entry.get("enclosures", []):
            typ = (enc.get("type") or "").lower()
            href = (enc.get("href") or enc.get("url") or "").lower()
            if typ in ("application/x-mpegurl", "application/vnd.apple.mpegurl") or ".m3u8" in href:
                return "hls"  # explicit streaming enclosure wins
            if typ.startswith("video/"):
                video = "mp4"
    return video


class _TextExtractor(HTMLParser):
    """Collects text content, dropping tags and decoding entities."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join(parser.parts)


def _itunes_channel_fields(feed_content: bytes) -> "tuple[str, str, str]":
    """Read (owner_name, owner_email, itunes_author) straight from the XML.

    feedparser's normalized fields are unusable here: it drops a
    channel-root <itunes:email> entirely, lets <itunes:owner> overwrite
    feed.author when it follows <itunes:author> (the common tag order),
    and leaks <webMaster>/<managingEditor> into the same fields. Direct
    extraction keeps the fields strictly per spec. Best-effort: any parse
    failure returns all None.
    """
    def is_itunes(el, name):
        tag = el.tag
        return (isinstance(tag, str) and tag.startswith("{")
                and "itunes" in tag.lower()
                and tag.rsplit("}", 1)[-1] == name)

    def text(el):
        return el.text.strip() if (el.text and el.text.strip()) else None

    try:
        channel = ET.fromstring(feed_content).find("channel")
    except Exception:
        return None, None, None
    if channel is None:
        return None, None, None

    owner_name = owner_email = root_email = author = None
    for el in channel:
        if is_itunes(el, "owner"):
            for sub in el:
                if is_itunes(sub, "name") and not owner_name:
                    owner_name = text(sub)
                elif is_itunes(sub, "email") and not owner_email:
                    owner_email = text(sub)
        elif is_itunes(el, "email") and not root_email:
            root_email = text(el)  # channel-root <itunes:email> fallback
        elif is_itunes(el, "author") and not author:
            author = text(el)
    return owner_name, owner_email or root_email, author


def parse_feed_meta(feed_content: bytes) -> dict:
    """Extract the first media URL plus channel-level contact metadata.

    All fields are best-effort and default to None. Contact fields come
    straight from the XML (see _itunes_channel_fields); feedparser covers
    enclosures, <link>, and the description.
    """
    parsed = feedparser.parse(feed_content)
    meta = dict(EMPTY_META)
    for entry in parsed.entries:
        for enclosure in entry.get("enclosures", []):
            href = enclosure.get("href") or enclosure.get("url")
            if href:
                meta["media_url"] = href
                break
        if not meta["media_url"]:
            for media in entry.get("media_content", []):
                if media.get("url"):
                    meta["media_url"] = media["url"]
                    break
        if meta["media_url"]:
            break

    owner_name, owner_email, itunes_author = _itunes_channel_fields(feed_content)
    meta["owner_name"] = owner_name
    meta["owner_email"] = owner_email
    meta["itunes_author"] = itunes_author
    meta["cadence"] = episode_cadence(parsed)
    meta["schedule"] = publishing_schedule(parsed)
    meta["rss_video"] = rss_video_kind(parsed)
    pubs = [e.published_parsed for e in parsed.entries if e.get("published_parsed")]
    meta["last_published"] = time.strftime("%Y-%m-%d", max(pubs)) if pubs else None

    channel = parsed.feed
    meta["website"] = channel.get("link") or None
    description = channel.get("summary") or channel.get("description") or None
    if description:
        # feeds embed HTML in descriptions; strip to plain text first so
        # the card never shows literal tags and truncation can't cut a tag
        description = " ".join(strip_html(description).split()) or None
        if description and len(description) > 300:
            description = description[:300].rstrip() + "…"
    meta["description"] = description
    return meta


def fetch_show_feed(feed_url: str) -> "tuple[str, dict]":
    """Fetch a show's RSS once: detect the host and pull contact metadata.

    The feed is always fetched (even when the feed domain alone identifies
    the host) because the contact card needs channel metadata. Fetch or
    parse failures degrade gracefully: metadata stays None and the host
    falls back to the feed-domain match, or "Unknown" if there is none.
    """
    domain_host = detect_host(feed_url)
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        meta = parse_feed_meta(resp.content)
    except requests.RequestException as exc:
        log(f"WARNING: could not fetch RSS {feed_url}: {exc}")
        return (domain_host if domain_host != UNMATCHED else "Unknown"), dict(EMPTY_META)
    except Exception as exc:  # malformed feeds must never abort the run
        log(f"WARNING: could not parse RSS {feed_url}: {exc}")
        return (domain_host if domain_host != UNMATCHED else "Unknown"), dict(EMPTY_META)
    time.sleep(0.5)
    if domain_host != UNMATCHED:
        return domain_host, meta
    return detect_host(feed_url, meta["media_url"] or ""), meta


def resolve_hosts(shows: dict, host_cache: dict = None) -> None:
    """Resolve feed URLs, detect hosts, and collect contact metadata.

    host_cache maps itunes_id -> (feed_url, host, meta) and lets callers
    (the backfill script) reuse lookups across multiple scrape passes.
    """
    cache = host_cache if host_cache is not None else {}
    total = len(shows)
    for i, show in enumerate(shows.values(), 1):
        if show["itunes_id"] in cache:
            show["feed_url"], show["host"], meta = cache[show["itunes_id"]]
            show.update({k: v for k, v in meta.items() if k != "media_url"})
            log(f"Processing show {i}/{total}: {show['title']} (cache hit)")
            continue
        log(f"Processing show {i}/{total}: {show['title']} (looking up feed)")
        info = lookup_show(show["itunes_id"])
        show["feed_url"] = info.get("feed_url")
        time.sleep(0.3)
        if not show["feed_url"]:
            log(f"WARNING: no feed URL for {show['title']}, marking host Unknown")
            show["host"], meta = "Unknown", dict(EMPTY_META)
        else:
            show["host"], meta = fetch_show_feed(show["feed_url"])
        # episode count + latest-episode date ride along from the same lookup;
        # the iTunes values win, with the feed-derived date as a fallback
        meta["episode_count"] = info.get("episode_count") or meta.get("episode_count")
        meta["last_published"] = info.get("last_published") or meta.get("last_published")
        show.update({k: v for k, v in meta.items() if k != "media_url"})
        cache[show["itunes_id"]] = (show["feed_url"], show["host"], meta)


def rank_platforms(show_list: list, include_shows: bool) -> list:
    """Group shows by platform, sorted by count desc, with 1-based ranks."""
    total = len(show_list)
    by_host = defaultdict(list)
    for show in show_list:
        by_host[show["host"]].append(show)
    platforms = []
    for host, members in by_host.items():
        entry = {
            "name": host,
            "count": len(members),
            "share": round(len(members) / total * 100, 1) if total else 0.0,
        }
        if include_shows:
            entry["shows"] = [{
                "title": s["title"],
                "itunes_id": s["itunes_id"],
                "artwork": s["artwork"],
                "categories": s["categories"],
                "feed_url": s["feed_url"],
                "owner_name": s.get("owner_name"),
                "owner_email": s.get("owner_email"),
                "itunes_author": s.get("itunes_author"),
                "website": s.get("website"),
                "description": s.get("description"),
                "cadence": s.get("cadence"),
                "episode_count": s.get("episode_count"),
                "last_published": s.get("last_published"),
                "schedule": s.get("schedule"),
                "rss_video": s.get("rss_video"),
                "apple_video": s.get("apple_video"),
                "host_since": s.get("host_since"),
                "prev_host": s.get("prev_host"),
                "moved_on": s.get("moved_on"),
            } for s in members]
        platforms.append(entry)
    platforms.sort(key=lambda p: (-p["count"], p["name"]))
    for rank, p in enumerate(platforms, 1):
        p["rank"] = rank
    return platforms


def track_host_changes(records, id_key: str, state_path, scan_date: str) -> int:
    """Track each show's hosting platform across scans.

    `records` is an iterable of show dicts, each with an id at `id_key` and a
    resolved `host`. Persists {id: {host, since, prev_host?, moved_on?}} to
    `state_path` and annotates each record with host_since / prev_host /
    moved_on so the frontend can flag a show that recently switched hosts.
    A move is only recorded between two confident hosts (transient "Unknown"
    or unresolved hosts are ignored, so a one-off lookup failure can't fake a
    migration). Returns how many shows moved this run.
    """
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            state = {}
    moved = 0
    for rec in records:
        sid, host = rec.get(id_key), rec.get("host")
        if not sid:
            continue
        st = state.get(sid)
        confident = host and host not in ("Unknown", UNMATCHED)
        if confident:
            if st is None:
                state[sid] = {"host": host, "since": scan_date}
            elif st.get("host") != host:
                state[sid] = {"host": host, "since": scan_date,
                              "prev_host": st.get("host"), "moved_on": scan_date}
                moved += 1
            st = state[sid]
        if st:
            rec["host_since"] = st.get("since")
            rec["prev_host"] = st.get("prev_host")
            rec["moved_on"] = st.get("moved_on")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))
    return moved


def build_leaderboard(shows: dict, scan_date: str) -> dict:
    """Build the current-snapshot leaderboard.json structure."""
    all_shows = list(shows.values())
    by_category = {}
    for category in CATEGORIES:
        members = [s for s in all_shows if category in s["categories"]]
        by_category[category] = {"platforms": rank_platforms(members, include_shows=False)}
    return {
        "last_scanned": scan_date,
        "total_unique_shows": len(all_shows),
        "platforms": rank_platforms(all_shows, include_shows=True),
        "by_category": by_category,
    }


def build_history_entry(leaderboard: dict) -> dict:
    """Aggregate a leaderboard snapshot into a compact history entry."""
    return {
        "date": leaderboard["last_scanned"],
        "total_shows": leaderboard["total_unique_shows"],
        "platforms": [{
            "name": p["name"],
            "count": p["count"],
            "share": p["share"],
            "rank": p["rank"],
        } for p in leaderboard["platforms"]],
    }


def compact_history(entries: list) -> list:
    """Apply retention rules and return the compacted, date-sorted list.

    Relative to the newest entry's date:
      - last 90 days: keep every entry
      - 90 days to 1 year: keep the last entry of each ISO week
      - older than 1 year: keep the last entry of each month
    """
    if not entries:
        return []
    entries = sorted(entries, key=lambda e: e["date"])
    newest = datetime.strptime(entries[-1]["date"], "%Y-%m-%d").date()
    daily_cutoff = newest - timedelta(days=DAILY_WINDOW_DAYS)
    weekly_cutoff = newest - timedelta(days=WEEKLY_WINDOW_DAYS)

    keep_by_bucket = {}  # bucket key -> entry (later entries overwrite earlier)
    for entry in entries:
        date = datetime.strptime(entry["date"], "%Y-%m-%d").date()
        if date > daily_cutoff:
            key = ("day", date.isoformat())
        elif date > weekly_cutoff:
            iso = date.isocalendar()
            key = ("week", iso[0], iso[1])
        else:
            key = ("month", date.year, date.month)
        keep_by_bucket[key] = entry
    return sorted(keep_by_bucket.values(), key=lambda e: e["date"])


SYNTHETIC_BUDGET = 26  # weekly placeholder entries seeded across ~6 months


def prune_synthetic(entries: list) -> list:
    """Shrink seeded placeholder history as real entries accumulate.

    Synthetic entries (marked "synthetic": true by seed_history.py) exist
    only so the bump chart is viewable before real history builds up.
    Each real entry retires one synthetic entry, oldest first, so the
    placeholder tail recedes and disappears entirely after ~a month of
    daily scrapes.
    """
    real = [e for e in entries if not e.get("synthetic")]
    synth = sorted((e for e in entries if e.get("synthetic")),
                   key=lambda e: e["date"])
    allowed = max(0, SYNTHETIC_BUDGET - len(real))
    if len(synth) > allowed:
        synth = synth[len(synth) - allowed:]
    return sorted(real + synth, key=lambda e: e["date"])


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except ValueError as exc:
            log(f"WARNING: history.json is corrupt ({exc}), starting fresh")
    return {"schema_version": 1, "first_entry": None, "last_entry": None, "entries": []}


def append_history(entry: dict) -> int:
    """Append an entry to history.json (replacing any same-date entry)."""
    history = load_history()
    entries = [e for e in history["entries"] if e["date"] != entry["date"]]
    entries.append(entry)
    entries = compact_history(prune_synthetic(entries))
    history["entries"] = entries
    history["first_entry"] = entries[0]["date"]
    history["last_entry"] = entries[-1]["date"]
    history["schema_version"] = 1
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    return len(entries)


def build_show_history_entry(shows: dict, date: str) -> dict:
    """Per-category ordered ID lists for one scan. Rank = array index + 1.

    This is the compact form: storing the ordered ids makes each show's
    rank in each category recoverable without repeating rank numbers.
    """
    charts = {}
    for category in CATEGORIES:
        members = sorted(
            ((s["ranks"][category], s["itunes_id"]) for s in shows.values()
             if category in s.get("ranks", {})),
            key=lambda pair: pair[0])
        charts[category] = [sid for _, sid in members]
    return {"date": date, "charts": charts}


def load_show_history() -> dict:
    if SHOW_HISTORY_PATH.exists():
        try:
            return json.loads(SHOW_HISTORY_PATH.read_text())
        except ValueError as exc:
            log(f"WARNING: show_history.json is corrupt ({exc}), starting fresh")
    return {"schema_version": 1, "first_entry": None, "last_entry": None,
            "entries": [], "shows": {}}


def append_show_history(entry: dict, shows: dict) -> int:
    """Append a show-rank entry and refresh the show directory.

    The directory ({id: {title, artwork}}) is last-seen-wins so rotated
    artwork URLs stay current, and is pruned to ids still referenced by a
    retained entry so it never grows unbounded. Written as compact JSON
    (this file is machine-read and lazy-loaded, never hand-edited).
    """
    history = load_show_history()
    entries = [e for e in history["entries"] if e["date"] != entry["date"]]
    entries.append(entry)
    entries = compact_history(prune_synthetic(entries))

    directory = history.get("shows", {})
    for s in shows.values():  # last-seen-wins refresh for today's shows
        directory[s["itunes_id"]] = {"title": s["title"], "artwork": s["artwork"]}
    referenced = {sid for e in entries for ids in e["charts"].values() for sid in ids}
    directory = {sid: meta for sid, meta in directory.items() if sid in referenced}

    history.update({
        "schema_version": 1,
        "first_entry": entries[0]["date"],
        "last_entry": entries[-1]["date"],
        "entries": entries,
        "shows": directory,
    })
    SHOW_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHOW_HISTORY_PATH.write_text(
        json.dumps(history, separators=(",", ":"), ensure_ascii=False))
    return len(entries)


def main() -> int:
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"[{scan_date}] Starting scrape...")

    shows = fetch_charts(CATEGORIES)
    log(f"Total unique shows: {len(shows)}")

    resolve_hosts(shows)

    # Stamp Apple-native video flags from apple_video.py's rotating scan.
    try:
        av_state = json.loads(APPLE_VIDEO_STATE_PATH.read_text())
    except Exception:
        av_state = {}
    for s in shows.values():
        st = av_state.get(str(s["itunes_id"]))
        if st is not None:
            s["apple_video"] = bool(st.get("video"))

    moved = track_host_changes(shows.values(), "itunes_id", HOST_STATE_PATH, scan_date)
    log(f"Host changes since last scan: {moved}")

    leaderboard = build_leaderboard(shows, scan_date)
    top = leaderboard["platforms"][:5]
    log("Detected: " + ", ".join(f"{p['name']} ({p['count']} shows)" for p in top) + " ...")

    if leaderboard["total_unique_shows"] < MIN_SHOWS:
        log(f"ERROR: only {leaderboard['total_unique_shows']} shows detected "
            f"(minimum {MIN_SHOWS}), refusing to write output")
        return 1
    log(f"Validation passed: {leaderboard['total_unique_shows']} shows")

    log("Writing leaderboard.json...")
    LEADERBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEADERBOARD_PATH.write_text(json.dumps(leaderboard, indent=2, ensure_ascii=False))

    count = append_history(build_history_entry(leaderboard))
    log(f"Appending to history.json (now {count} entries)...")

    sh_count = append_show_history(build_show_history_entry(shows, scan_date), shows)
    log(f"Appending to show_history.json (now {sh_count} entries)...")
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
