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

Where a network's shows come from (any combination, unioned + deduped):
  1. channel_id  -> the Apple channel page is fetched and every show it lists is
     discovered (same public-page technique as apple_video.py). Optional; set
     NETWORKS_NO_CHANNEL_SCRAPE=1 to skip and trust only the curated ids.
  2. extra_ids   -> curated show ids from the seed, always included. Use these
     for back-catalog shows the channel page no longer lists, or for networks
     that have no Apple channel at all (declare ids, no channel_id).
  3. search      -> an iTunes name-search (artistTerm), keeping only shows whose
     author credits the network. This is how networks that have no Apple channel
     are resolved by name; the label is used as the term when none is given.

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
SEARCH_API = "https://itunes.apple.com/search"
# Safari UA for the channel-page fetch: podcasts.apple.com serves the full
# serialized page to a browser UA (same trick apple_video.py relies on).
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")}

WORKERS = 8             # feed fetches in flight; networks are small, so this is
                        # plenty and stays polite to each host.
MAX_CHANNEL_SHOWS = 500  # backstop on ids scraped from one channel page.
SEARCH_LIMIT = 200      # iTunes search results pulled per name-search network.
PER_NETWORK = 20        # cap on name-search shows kept per network (10-20 is plenty).

# A show link on an Apple page: /podcast/<slug>/id<digits> (episode links carry
# the same show adamId, then ?i=...). Captures the show adamId.
SHOW_ID_RE = re.compile(r"/podcast/[^\"'\\ ]*?/id(\d+)")

# Generic words dropped when reducing a network name to its match tokens, so a
# name-search keeps only shows whose iTunes author actually credits the network
# (e.g. "PAVE Studios" -> token "pave"; a show by "PAVE Studios" matches).
_GENERIC = {"media", "network", "networks", "podcast", "podcasts", "inc", "the",
            "studios", "studio", "co", "productions", "production", "group",
            "audio", "entertainment", "labs", "original", "originals", "llc",
            "company", "and", "a"}


# Pinned rosters for sales-rep / agency networks that don't credit themselves
# in each show's iTunes author, so name-search can't find them. Keyed by the
# seed label -> [(feed_url, title), ...]. Resolved to hosts like any other feed.
EXPLICIT_FEEDS = {
    "True Native Media": [
        ("https://feeds.megaphone.fm/QCD2921626995", "The History Chicks"),
        ("https://feeds.megaphone.fm/ARML9966973519", "The Box of Oddities"),
        ("https://rss.art19.com/i-have-adhd", "I Have ADHD Podcast"),
        ("https://rss.pdrl.fm/353ca0/rss.art19.com/reality-life-with-kate-casey", "Reality Life with Kate Casey"),
        ("https://feeds.megaphone.fm/really-very-crunchy", "The Really Very Crunchy Podcast"),
        ("https://rss.art19.com/post-wrestling", "POST Wrestling"),
        ("https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/976825e3-37d8-4387-a0cd-b24b002d91e5/3df02c7c-f75a-4bea-abe8-b24b002d91ff/podcast.rss", "Buried Bones"),
        ("https://feeds.megaphone.fm/ARML7840024233", "The Conspirators Podcast"),
        ("https://feeds.megaphone.fm/WFH5218810446", "The 1000 Hours Outside Podcast"),
        ("https://rss.pdrl.fm/bddf6c/feeds.megaphone.fm/RNMG7301081241", "Book Riot - The Podcast"),
    ],
}


def _tokens(name: str) -> list:
    """Distinctive lowercase tokens of a network name (generic words removed)."""
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
             if w not in _GENERIC]
    return words or [re.sub(r"[^a-z0-9]+", "", name.lower())]


def _artist_matches(artist: str, tokens: list) -> bool:
    """True when every distinctive token appears in the show's iTunes author."""
    a = (artist or "").lower()
    return bool(tokens) and all(t in a for t in tokens)


def itunes_search(term: str) -> list:
    """Name-search a network via the iTunes Search API's artistTerm attribute,
    keeping only results whose author credits the network. Returns show metas
    (same shape as itunes_meta), best-effort: any failure yields []."""
    tokens = _tokens(term)
    try:
        r = requests.get(SEARCH_API, params={
            "term": term, "entity": "podcast",
            "attribute": "artistTerm", "limit": SEARCH_LIMIT,
        }, headers=HEADERS, timeout=TIMEOUT)
        res = r.json().get("results", [])
    except Exception as exc:
        log(f"networks: search '{term}' failed: {exc}")
        return []
    out = []
    for x in res:
        feed = x.get("feedUrl")
        if not feed or not _artist_matches(x.get("artistName"), tokens):
            continue
        last = x.get("releaseDate")
        out.append({
            "itunes_id": str(x.get("collectionId") or ""),
            "title": x.get("collectionName") or x.get("trackName") or "Untitled",
            "artwork": x.get("artworkUrl600") or x.get("artworkUrl100") or "",
            "feed_url": feed,
            "episode_count": x.get("trackCount"),
            "last_published": last[:10] if last else None,
        })
        if len(out) >= PER_NETWORK:
            break
    return out


def load_seed():
    """Parse data/network_seed.csv into a list of network dicts. Tolerant of
    blank lines, #-comments and a header row. Columns:
    label, channel_id, extra_ids (;-separated), homepage, search.

    A network resolves from any combination of: its Apple channel(s), curated
    show ids, and an iTunes name-search term. If a row gives none of channel /
    ids / search, the label itself is used as the search term."""
    out = []
    if not SEED_PATH.exists():
        return out
    for line in SEED_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader([line]))
        parts += [""] * (5 - len(parts))  # pad to 5 columns
        label, channel, extra_ids, homepage, search = (p.strip() for p in parts[:5])
        if not label or label.lower() == "label":  # skips the header row
            continue
        ids = [i.strip() for i in extra_ids.split(";") if i.strip().isdigit()]
        channel_ids = [c.strip() for c in channel.split(";") if c.strip().isdigit()]
        # Fall back to the label as the search term only when nothing else is given.
        if not search and not channel_ids and not ids:
            search = label
        out.append({
            "label": label,
            "channel_ids": channel_ids,  # 0+ Apple channel ids (;-separated)
            "extra_ids": ids,
            "homepage": homepage or None,
            "search": search or None,
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


def _host_for_meta(meta: dict) -> dict:
    """Attach a resolved delivery host to an already-fetched show meta."""
    feed = meta.get("feed_url")
    meta["host"] = fetch_show_feed(feed)[0] if feed else "Unknown"
    return meta


def resolve_show(itunes_id: str) -> "dict | None":
    """Full record for one show: metadata + resolved delivery host."""
    meta = itunes_meta(itunes_id)
    time.sleep(0.2)
    if not meta:
        return None
    return _host_for_meta(meta)


def resolve_network(net: dict) -> dict:
    """Discover + resolve every show in one network. Channel-discovered ids are
    unioned with the curated extra_ids (curated always kept)."""
    ids, seen = [], set()
    for iid in net["extra_ids"]:  # curated first
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)
    discovered = []
    for cid in net["channel_ids"]:  # union across every channel on the row
        discovered.extend(channel_show_ids(cid))
    for iid in discovered:
        if iid not in seen:
            seen.add(iid)
            ids.append(iid)

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

    # Name-search phase: fold in shows the iTunes author credits to this network,
    # deduped against everything already resolved by id/channel.
    searched = 0
    if net.get("search"):
        have_ids = {s.get("itunes_id") for s in shows}
        have_feeds = {s.get("feed_url") for s in shows}
        metas = [m for m in itunes_search(net["search"])
                 if m["itunes_id"] not in have_ids and m["feed_url"] not in have_feeds]
        searched = len(metas)
        if metas:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(_host_for_meta, m): m for m in metas}
                for fut in as_completed(futures):
                    try:
                        rec = fut.result()
                    except Exception:
                        rec = None
                    if rec:
                        shows.append(rec)

    # Pinned-roster phase: explicit (feed, title) pairs for rep networks that
    # name-search can't credit. Resolved to hosts, deduped against the rest.
    pinned = 0
    roster = EXPLICIT_FEEDS.get(net["label"], [])
    if roster:
        have_feeds = {s.get("feed_url") for s in shows}
        for feed, title in roster:
            if feed in have_feeds:
                continue
            shows.append({
                "itunes_id": "", "title": title, "artwork": "", "feed_url": feed,
                "episode_count": None, "last_published": None,
                "host": fetch_show_feed(feed)[0],
            })
            have_feeds.add(feed)
            pinned += 1

    log(f"networks: {net['label']}: {len(shows)} shows "
        f"({len(net['extra_ids'])} curated, {len(discovered)} channel, "
        f"{searched} search, {pinned} pinned)")
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
        "channel_ids": net["channel_ids"],
        "homepage": net["homepage"],
        "show_count": len(shows),
        "feed_count": sum(1 for s in shows if s.get("feed_url")),
        "hosts": hosts,
        "shows": shows,
    }
    if not shows:
        if net.get("search"):
            entry["note"] = f"name-search '{net['search']}' matched no author-credited shows"
        elif not ids:
            entry["note"] = "no channel_id, extra_ids, or search term in seed"
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
