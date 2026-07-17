"""Build a hosting leaderboard from Spotify's podcast charts (MVP).

Spotify's public charts API (no auth) gives show names but no RSS feed or
host, so we resolve each show's host through a layered bridge, cheapest first:

  1. CACHE          data/spotify_show_map.json (showUri -> host), permanent.
  2. APPLE MATCH    normalized-name match against today's data/leaderboard.json
                    (the shows we already resolved for the Apple board). Free.
  3. PODCAST INDEX  if PODCASTINDEX_KEY/SECRET are set (env or
                    scraper/podcastindex_creds.json): search by name -> feed.
  4. iTUNES SEARCH  fallback, heavily rate-limited; capped per run and stops
                    if Apple starts blocking. Whatever resolves is cached.

Anything still unresolved is bucketed "Unresolved (Spotify-only)" and retried
next run. Output: data/spotify_leaderboard.json (same shape as the Apple board).

Run: python scraper/spotify_scrape.py
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from hosts import UNMATCHED, detect_host
from scrape import (HEADERS, LEADERBOARD_PATH, TIMEOUT, compact_history, log,
                    parse_feed_meta, prune_synthetic, strip_html,
                    track_host_changes)

# Contact fields carried on every show record (mirrors the Apple card).
CONTACT_FIELDS = ("feed_url", "owner_name", "owner_email", "itunes_author",
                  "website", "description", "cadence", "episode_count",
                  "last_published", "schedule", "rss_video")

ROOT = LEADERBOARD_PATH.parent.parent
OUTPUT_PATH = ROOT / "data" / "spotify_leaderboard.json"
CACHE_PATH = ROOT / "data" / "spotify_show_map.json"
HISTORY_PATH = ROOT / "data" / "spotify_history.json"
SHOW_HISTORY_PATH = ROOT / "data" / "spotify_show_history.json"
HOST_STATE_PATH = ROOT / "data" / "spotify_host_state.json"
CREDS_PATH = ROOT / "scraper" / "podcastindex_creds.json"
SEED_WEEKS = 8  # placeholder history so the time views are usable immediately

CHART_API = "https://podcastcharts.byspotify.com/api/charts/{slug}?region={region}&limit=100"
SEARCH_API = "https://itunes.apple.com/search?term={q}&entity=podcast&limit=1"
PI_SEARCH = "https://api.podcastindex.org/api/1.0/search/bytitle?q={q}&max=1"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Spotify chart id (hyphenated) -> our display category. "top"/"top-episodes"
# are intermittently 500 on Spotify's side; the category charts are reliable.
CATEGORIES = {
    "business": "Business", "comedy": "Comedy", "education": "Education",
    "fiction": "Fiction", "health-fitness": "Health & Fitness", "history": "History",
    "leisure": "Leisure", "music": "Music", "news": "News",
    "religion-spirituality": "Religion & Spirituality", "science": "Science",
    "society-culture": "Society & Culture", "sports": "Sports",
    "technology": "Technology", "true-crime": "True Crime", "tv-film": "TV & Film",
}
ITUNES_PER_RUN_CAP = 120   # politeness cap on the rate-limited fallback
ITUNES_SLEEP = 2.5


def norm(s: str) -> str:
    s = re.sub(r"\bthe\b|\bpodcast\b|\bshow\b|\bwith\b", " ", s.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def fetch_chart(slug: str, region: str = "us") -> list:
    headers = {**HEADERS, "User-Agent": BROWSER_UA, "Accept": "application/json",
               "Referer": "https://podcastcharts.byspotify.com/"}
    try:
        r = requests.get(CHART_API.format(slug=slug, region=region), headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log(f"  WARNING: chart '{slug}' failed: {exc}")
        return []
    return data if isinstance(data, list) else next(
        (v for v in data.values() if isinstance(v, list)), [])


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            pass
    return default


def apple_index() -> dict:
    """normalized show name -> full record (host + contact) from the Apple board.

    Apple shows already carry parsed contact metadata (incl. owner email), so
    a Spotify show that matches one gets complete contact for free.
    """
    board = load_json(LEADERBOARD_PATH, {"platforms": []})
    idx = {}
    for p in board["platforms"]:
        for s in p.get("shows", []):
            idx[norm(s["title"])] = dict(
                host=p["name"], **{f: s.get(f) for f in CONTACT_FIELDS})
    return idx


def pi_creds():
    key = os.environ.get("PODCASTINDEX_KEY")
    secret = os.environ.get("PODCASTINDEX_SECRET")
    if key and secret:
        return key, secret
    c = load_json(CREDS_PATH, {})
    return (c["key"], c["secret"]) if c.get("key") and c.get("secret") else (None, None)


def pi_lookup(name: str, key: str, secret: str) -> "tuple[str, dict]":
    """Return (feed_url, metadata) from Podcast Index. PI gives owner name,
    author, website and description directly, everything but the email."""
    epoch = str(int(time.time()))
    auth = hashlib.sha1((key + secret + epoch).encode()).hexdigest()
    headers = {"User-Agent": "podcast-leaderboard/1.0", "X-Auth-Key": key,
               "X-Auth-Date": epoch, "Authorization": auth}
    try:
        r = requests.get(PI_SEARCH.format(q=urllib.parse.quote(name)),
                         headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        feeds = r.json().get("feeds", [])
    except Exception:
        return "", {}
    if not feeds:
        return "", {}
    f = feeds[0]
    desc = f.get("description") or None
    if desc:
        desc = " ".join(strip_html(desc).split()) or None
        if desc and len(desc) > 300:
            desc = desc[:300].rstrip() + "…"
    npub = f.get("newestItemPubdate")
    meta = {"owner_name": f.get("ownerName") or None,
            "itunes_author": f.get("author") or None,
            "website": f.get("link") or None, "description": desc,
            "episode_count": f.get("episodeCount") or None,
            "last_published": time.strftime("%Y-%m-%d", time.gmtime(npub)) if npub else None}
    return f.get("url", "") or "", meta


def feed_contact(feed_url: str) -> dict:
    """Fetch a feed and parse full contact (incl. owner email). Best-effort."""
    try:
        c = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT)
        c.raise_for_status()
        m = parse_feed_meta(c.content)
        return {k: m.get(k) for k in ("owner_name", "owner_email", "itunes_author",
                                      "website", "description", "cadence", "last_published",
                                      "schedule", "rss_video")}
    except Exception:
        return {}


def itunes_feed_for(name: str) -> "str | None":
    """Returns feed URL, '' for genuine no-match, or None if blocked/errored."""
    try:
        r = requests.get(SEARCH_API.format(q=urllib.parse.quote(name)),
                         headers=HEADERS, timeout=TIMEOUT)
        res = r.json().get("results", [])
    except ValueError:
        return None  # non-JSON body == throttled/blocked
    except Exception:
        return None
    return (res[0].get("feedUrl", "") or "") if res else ""


def host_from_feed(feed_url: str) -> str:
    host = detect_host(feed_url)
    if host != UNMATCHED or not feed_url:
        return host
    try:
        c = requests.get(feed_url, headers=HEADERS, timeout=TIMEOUT)
        return detect_host(feed_url, parse_feed_meta(c.content)["media_url"] or "")
    except Exception:
        return UNMATCHED


def platform_history_entry(platforms: list, total: int, date: str) -> dict:
    return {"date": date, "total_shows": total,
            "platforms": [{"name": p["name"], "count": p["count"],
                           "share": p["share"], "rank": p["rank"]} for p in platforms]}


def show_history_entry(shows: dict, date: str) -> dict:
    """Per-category ordered showUris for one scan. Rank = array index + 1."""
    charts = {}
    for display in CATEGORIES.values():
        members = sorted(((s["ranks"][display], uri) for uri, s in shows.items()
                          if display in s.get("ranks", {})), key=lambda x: x[0])
        charts[display] = [uri for _, uri in members]
    return {"date": date, "charts": charts}


def _seed_platform(entry: dict) -> list:
    """Weekly placeholders with gentle count jitter so ranks shift for QC."""
    base = datetime.strptime(entry["date"], "%Y-%m-%d").date()
    out = []
    for week in range(1, SEED_WEEKS + 1):
        rng = random.Random(7000 + week)
        counts = {p["name"]: max(1, round(p["count"] * (1 + rng.uniform(-0.08, 0.08))))
                  for p in entry["platforms"]}
        total = sum(counts.values())
        plats = sorted(({"name": n, "count": c, "share": round(c / total * 100, 1)}
                        for n, c in counts.items()), key=lambda p: (-p["count"], p["name"]))
        for i, p in enumerate(plats, 1):
            p["rank"] = i
        out.append({"date": (base - timedelta(weeks=week)).isoformat(),
                    "total_shows": total, "platforms": plats, "synthetic": True})
    return out


def _seed_shows(entry: dict) -> list:
    base = datetime.strptime(entry["date"], "%Y-%m-%d").date()
    out = []
    for week in range(1, SEED_WEEKS + 1):
        charts = {}
        for ci, (cat, ids) in enumerate(entry["charts"].items()):
            rng = random.Random(9000 * week + ci)
            sigma = 1.5 + 0.4 * week
            charts[cat] = [u for _, u in sorted(
                ((i + rng.gauss(0, sigma), u) for i, u in enumerate(ids)), key=lambda x: x[0])]
        out.append({"date": (base - timedelta(weeks=week)).isoformat(),
                    "charts": charts, "synthetic": True})
    return out


def append_history(path, entry, seed_fn):
    hist = load_json(path, {"schema_version": 1, "entries": []})
    entries = [e for e in hist["entries"] if e["date"] != entry["date"]]
    entries.append(entry)
    if seed_fn and not any(e.get("synthetic") for e in entries) and len(entries) == 1:
        entries += seed_fn(entry)  # first real run: seed placeholder history
    entries = compact_history(prune_synthetic(entries))
    hist.update({"schema_version": 1, "entries": entries,
                 "first_entry": entries[0]["date"], "last_entry": entries[-1]["date"]})
    return hist, entries


def write_show_history(entry: dict, shows: dict):
    hist, entries = append_history(SHOW_HISTORY_PATH, entry, _seed_shows)
    directory = hist.get("shows", {})
    for uri, s in shows.items():
        directory[uri] = {"title": s["name"], "artwork": s["artwork"]}
    referenced = {u for e in entries for ids in e["charts"].values() for u in ids}
    hist["shows"] = {u: m for u, m in directory.items() if u in referenced}
    SHOW_HISTORY_PATH.write_text(json.dumps(hist, separators=(",", ":"), ensure_ascii=False))
    return len(entries)


def main() -> int:
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log(f"[{scan_date}] Spotify charts scrape...")

    shows = {}  # showUri -> {name, publisher, artwork, cats, move}
    for slug, display in CATEGORIES.items():
        items = fetch_chart(slug)
        log(f"  {display}: {len(items)} shows")
        for rank, it in enumerate(items, 1):  # chart order = within-category rank
            s = shows.setdefault(it["showUri"], {
                "name": it["showName"], "publisher": it.get("showPublisher", ""),
                "artwork": it.get("showImageUrl", ""), "cats": [], "ranks": {},
                "move": it.get("chartRankMove", "")})
            if display not in s["cats"]:
                s["cats"].append(display)
            s["ranks"][display] = rank
        time.sleep(0.5)

    if not shows:
        log("ERROR: no Spotify chart data fetched")
        return 1
    log(f"{len(shows)} unique shows across {len(CATEGORIES)} categories")

    cache = load_json(CACHE_PATH, {})
    apple = apple_index()
    key, secret = pi_creds()
    log(f"bridge: cache={len(cache)} known, Apple corpus={len(apple)}, "
        f"PodcastIndex={'yes' if key else 'no'}")

    def empty_rec(host):
        return dict(host=host, **{f: None for f in CONTACT_FIELDS})

    counts = defaultdict(int)  # how each show got resolved
    itunes_used = itunes_blocked = 0
    for uri, s in shows.items():
        cached = cache.get(uri)
        if isinstance(cached, dict) and "host" in cached:  # full record cached
            # backfill fields added after this entry was cached, from the Apple match
            if (cached.get("cadence") is None or cached.get("last_published") is None
                    or cached.get("schedule") is None):
                am = apple.get(norm(s["name"]))
                if am:
                    for f in ("cadence", "episode_count", "last_published", "schedule", "rss_video"):
                        if cached.get(f) is None and am.get(f) is not None:
                            cached[f] = am[f]
            s["rec"] = cached; counts["cache"] += 1; continue

        am = apple.get(norm(s["name"]))
        if am:  # Apple match: complete contact (incl. email) for free
            s["rec"] = dict(am); cache[uri] = s["rec"]; counts["apple"] += 1; continue

        feed, pimeta = ("", {})
        via = None
        if key:
            feed, pimeta = pi_lookup(s["name"], key, secret); time.sleep(0.2)
            if feed:
                via = "podcastindex"
        if not feed and itunes_used < ITUNES_PER_RUN_CAP and itunes_blocked < 3:
            f = itunes_feed_for(s["name"]); itunes_used += 1; time.sleep(ITUNES_SLEEP)
            if f is None:
                itunes_blocked += 1
            elif f:
                feed, via = f, "itunes"

        if feed:
            rec = empty_rec(host_from_feed(feed))
            rec["feed_url"] = feed
            fmeta = feed_contact(feed)  # one fetch -> owner email + accurate fields
            for f in ("owner_name", "itunes_author", "website", "description"):
                rec[f] = fmeta.get(f) or pimeta.get(f)
            rec["owner_email"] = fmeta.get("owner_email")
            rec["cadence"] = fmeta.get("cadence")
            rec["episode_count"] = pimeta.get("episode_count")  # PI only; RSS count is unreliable
            rec["last_published"] = fmeta.get("last_published") or pimeta.get("last_published")
            rec["schedule"] = fmeta.get("schedule")
            rec["rss_video"] = fmeta.get("rss_video")
            s["rec"] = rec; cache[uri] = rec; counts[via] += 1; continue

        s["rec"] = empty_rec("Unresolved (Spotify-only)"); counts["unresolved"] += 1

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")))
    log("resolution: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    emails = sum(1 for s in shows.values() if s["rec"].get("owner_email"))
    log(f"contact email present for {emails}/{len(shows)} shows")

    for uri, s in shows.items():  # carry id + flatten host for grouping
        s["uri"] = uri
        s["host"] = s["rec"]["host"]

    moved = track_host_changes(shows.values(), "uri", HOST_STATE_PATH, scan_date)
    log(f"Spotify host changes since last scan: {moved}")

    # Build leaderboard (same shape as the Apple board)
    def rank(show_list):
        total = len(show_list)
        by_host = defaultdict(list)
        for s in show_list:
            by_host[s["host"]].append(s)
        plats = [{
            "name": h, "count": len(m), "share": round(len(m) / total * 100, 1) if total else 0.0,
            "shows": [{"title": x["name"], "id": x["uri"], "artwork": x["artwork"],
                       "categories": x["cats"], "publisher": x["publisher"],
                       "chart_move": x["move"],
                       "host_since": x.get("host_since"), "prev_host": x.get("prev_host"),
                       "moved_on": x.get("moved_on"),
                       **{f: x["rec"].get(f) for f in CONTACT_FIELDS}} for x in m],
        } for h, m in by_host.items()]
        plats.sort(key=lambda p: (-p["count"], p["name"]))
        for i, p in enumerate(plats, 1):
            p["rank"] = i
        return plats

    all_shows = list(shows.values())
    by_category = {disp: {"platforms": rank([s for s in all_shows if disp in s["cats"]])}
                   for disp in CATEGORIES.values()}
    resolved = sum(1 for s in all_shows if not s["host"].startswith("Unresolved"))
    output = {
        "source": "Spotify Podcast Charts",
        "last_scanned": scan_date,
        "total_unique_shows": len(all_shows),
        "resolved_rate": round(resolved / len(all_shows) * 100, 1),
        "platforms": rank(all_shows),
        "by_category": by_category,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log(f"resolved host for {resolved}/{len(all_shows)} ({output['resolved_rate']}%)")
    log("Top: " + ", ".join(f"{p['name']} ({p['count']})" for p in output["platforms"][:5]))
    log(f"Wrote {OUTPUT_PATH}")

    # Time series (platform rankings + per-show ranks), seeded on first run
    ph, pe = append_history(HISTORY_PATH, platform_history_entry(
        output["platforms"], len(all_shows), scan_date), _seed_platform)
    HISTORY_PATH.write_text(json.dumps(ph, separators=(",", ":"), ensure_ascii=False))
    sh = write_show_history(show_history_entry(shows, scan_date), shows)
    log(f"spotify_history.json: {len(pe)} entries; spotify_show_history.json: {sh} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
