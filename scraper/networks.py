"""Track podcast *networks* -> data/network_feeds.csv + data/networks.json.

A "network" here is a publisher/label whose shows we want to follow as a group
(e.g. The Washington Post, Higher Ground, Campside Media). Each network is
declared in data/network_seed.csv by its Apple Podcasts channel id and/or an
explicit list of show ids. This module resolves every show to its RSS feed via
the iTunes lookup API, detects the feed's delivery host, and writes two things:

  data/network_feeds.csv  feed_url,label rows -- consumed by cumulative.py's
    watchlist loader as ALWAYS-CHECKED feeds, so once a network is added every
    one of its shows is re-fetched each run and any host migration is caught,
    exactly like the manual watchlist. Regenerated wholesale each run.
  data/networks.json      per-network roster + host breakdown (title, feed_url,
    host, episode count, latest episode per show) -- the queryable "who hosts
    this network" artifact.

Where the show ids come from, per network:
  1. channel_id  -> the Apple channel page is fetched and every show it lists is
     discovered (same public-page technique as apple_video.py). Optional; set
     NETWORKS_NO_CHANNEL_SCRAPE=1 to skip and trust only the curated ids.
  2. extra_ids   -> curated show ids from the seed, always included. Use these
     for back-catalog shows the channel page no longer lists, or for networks
     that have no Apple channel at all (declare ids, no channel_id).

Limited / scoped scan: this script only ever touches the seeded networks'
feeds, never the whole corpus, so running it *is* a limited scan. Narrow it
further to one or a few networks with NETWORKS_ONLY="The Washington Post" (a
comma-separated list of labels, matched case-insensitively). See the README.

No credentials needed (only the public iTunes API + public RSS + the public
Apple channel page). Never blocks the pipeline. Run: python scraper/networks.py
"""

import csv
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

from hosts import UNMATCHED, detect_host
from scrape import HEADERS, ROOT, TIMEOUT, fetch_show_feed, log

SEED_PATH = ROOT / "data" / "network_seed.csv"
NETWORK_FEEDS_PATH = ROOT / "data" / "network_feeds.csv"
NETWORKS_JSON_PATH = ROOT / "data" / "networks.json"

CHANNEL_URL = "https://podcasts.apple.com/us/channel/id{channel_id}"
LOOKUP_URL = "https://itunes.apple.com/lookup?id={itunes_id}&entity=podcast"
# Safari UA for the channel-page fetch: podcasts.apple.com serves the full
# serialized page to a browser UA (same trick apple_video.py relies on).
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")}

WORKERS = 8             # feed fetches in flight; networks are small, so this is
                        # plenty and stays polite to each host.
MAX_CHANNEL_SHOWS = 500  # backstop on ids scraped from one channel page.

# A show link on an Apple page: /podcast/<slug>/id<digits> (episode links carry
# the same show adamId, then ?i=...). Captures the show adamId.
SHOW_ID_RE = re.compile(r"/podcast/[^\"'\\ ]*?/id(\d+)")


def load_seed():
    """Parse data/network_seed.csv into a list of network dicts. Tolerant of
    blank lines, #-comments and a header row. Columns:
    label, channel_id, extra_ids (;-separated), homepage."""
    out = []
    if not SEED_PATH.exists():
        return out
    for line in SEED_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader([line]))
        parts += [""] * (4 - len(parts))  # pad to 4 columns
        label, channel_id, extra_ids, homepage = (p.strip() for p in parts[:4])
        if not label or label.lower() == "label":  # skips the header row
            continue
        ids = [i.strip() for i in extra_ids.split(";") if i.strip().isdigit()]
        out.append({
            "label": label,
            "channel_id": channel_id if channel_id.isdigit() else None,
            "extra_ids": ids,
            "homepage": homepage or None,
        })
    return out


def channel_show_ids(channel_id: str) -> list:
    """Every show id Apple lists on a network's channel page. Best-effort: an
    unreachable or unparseable page yields [] and the run leans on curated ids.
    """
    if os.environ.get("NETWORKS_NO_CHANNEL_SCRAPE") == "1":
        return []
    try:
        r = requests.get(CHANNEL_URL.format(channel_id=channel_id), headers=UA, timeout=25)
        if r.status_code != 200 or len(r.text) < 5000:
            log(f"networks: channel {channel_id} page unavailable ({r.status_code})")
            return []
        dec = urllib.parse.unquote(r.text)
    except Exception as exc:
        log(f"networks: channel {channel_id} fetch failed: {exc}")
        return []
    ids, seen = [], set()
    for iid in SHOW_ID_RE.findall(dec):
        if iid != channel_id and iid not in seen:
            seen.add(iid)
            ids.append(iid)
        if len(ids) >= MAX_CHANNEL_SHOWS:
            break
    return ids


def itunes_meta(itunes_id: str) -> "dict | None":
    """One iTunes lookup -> title, artwork, feed_url, episode count, latest
    episode date. None when the id resolves to nothing."""
    try:
        r = requests.get(LOOKUP_URL.format(itunes_id=itunes_id), headers=HEADERS, timeout=TIMEOUT)
        res = r.json().get("results", [])
    except Exception:
        return None
    if not res:
        return None
    r0 = res[0]
    last = r0.get("releaseDate")
    return {
        "itunes_id": str(itunes_id),
        "title": r0.get("collectionName") or r0.get("trackName") or "Untitled",
        "artwork": r0.get("artworkUrl600") or r0.get("artworkUrl100") or "",
        "feed_url": r0.get("feedUrl") or "",
        "episode_count": r0.get("trackCount"),
        "last_published": last[:10] if last else None,
    }


def resolve_show(itunes_id: str) -> "dict | None":
    """Full record for one show: metadata + resolved delivery host."""
    meta = itunes_meta(itunes_id)
    time.sleep(0.2)
    if not meta:
        return None
    feed = meta["feed_url"]
    if feed:
        host, _fm = fetch_show_feed(feed)
    else:
        host = "Unknown"
    meta["host"] = host
    return meta


def resolve_network(net: dict) -> dict:
    """Discover + resolve every show in one network. Channel-discovered ids are
    unioned with the curated extra_ids (curated always kept)."""
    ids, seen = [], set()
    for iid in net["extra_ids"]:  # curated first
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)
    discovered = channel_show_ids(net["channel_id"]) if net["channel_id"] else []
    for iid in discovered:
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)

    log(f"networks: {net['label']}: {len(ids)} shows "
        f"({len(net['extra_ids'])} curated, {len(discovered)} from channel page)")

    shows = []
    if ids:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(resolve_show, iid): iid for iid in ids}
            for fut in as_completed(futures):
                try:
                    rec = fut.result()
                except Exception:
                    rec = None
                if rec:
                    shows.append(rec)
    shows.sort(key=lambda s: (s.get("episode_count") or 0), reverse=True)

    # Host breakdown across shows with a confidently-resolved host.
    counts = {}
    for s in shows:
        h = s.get("host")
        if h and h not in ("Unknown", UNMATCHED):
            counts[h] = counts.get(h, 0) + 1
    total = sum(counts.values())
    hosts = [{"name": n, "count": c, "share": round(c / total * 100, 1) if total else 0.0}
             for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    entry = {
        "label": net["label"],
        "channel_id": net["channel_id"],
        "homepage": net["homepage"],
        "show_count": len(shows),
        "feed_count": sum(1 for s in shows if s.get("feed_url")),
        "hosts": hosts,
        "shows": shows,
    }
    if not ids:
        entry["note"] = "no channel_id or extra_ids in seed -- add one to resolve feeds"
    return entry


def write_network_feeds(networks: list) -> int:
    """Write data/network_feeds.csv: one feed_url,label row per resolved feed,
    labelled '<Network> -- <Show>'. This is the always-checked watchlist input
    the cumulative host tracker reads."""
    seen = set()
    rows = []
    for n in networks:
        for s in n["shows"]:
            feed = s.get("feed_url")
            if not feed or feed in seen:
                continue
            seen.add(feed)
            rows.append((feed, f"{n['label']} -- {s['title']}"))
    NETWORK_FEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NETWORK_FEEDS_PATH.open("w", newline="") as fh:
        fh.write("# Auto-built by scraper/networks.py from data/network_seed.csv.\n")
        fh.write("# feed_url,label -- read by cumulative.py as always-checked "
                 "watchlist feeds.\n")
        w = csv.writer(fh)
        w.writerow(["feed_url", "label"])
        w.writerows(rows)
    return len(rows)


def main() -> int:
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seed = load_seed()
    if not seed:
        log("networks: no data/network_seed.csv (or it's empty), nothing to do")
        return 0

    only = os.environ.get("NETWORKS_ONLY")
    if only:
        wanted = {s.strip().lower() for s in only.split(",") if s.strip()}
        seed = [n for n in seed if n["label"].lower() in wanted]
        log(f"networks: NETWORKS_ONLY -> scanning {len(seed)} network(s): "
            f"{', '.join(n['label'] for n in seed) or '(none matched)'}")
    else:
        log(f"networks: scanning {len(seed)} network(s)")

    networks = [resolve_network(n) for n in seed]

    NETWORKS_JSON_PATH.write_text(json.dumps(
        {"generated": scan_date, "networks": networks}, indent=2, ensure_ascii=False))
    feeds = write_network_feeds(networks)

    total_shows = sum(n["show_count"] for n in networks)
    log(f"networks: wrote {len(networks)} networks, {total_shows} shows, "
        f"{feeds} feeds to network_feeds.csv")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never block the pipeline
        log(f"networks: unexpected error, skipping: {exc}")
        sys.exit(0)
