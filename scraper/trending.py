"""Fetch trending podcasts from Podcast Index -> data/trending.json.

WHAT THIS IS (and isn't): Podcast Index derives "trending" from listen and
subscription activity reported by apps in *its* ecosystem (Podverse, Fountain,
Castamatic, Podcast Addict, and others), NOT from Apple or Spotify. The signal
skews toward indie/tech/enthusiast audiences, so treat it as an early-warning
prospecting signal, not a chart. Mainstream shows whose audiences live on Apple
or Spotify are underrepresented here.

Output: data/trending.json with each show's hosting platform already detected
from its feed URL, so the frontend can surface "who's climbing" by host.

Easy to disable: remove the CI step (or set FEATURE_TRENDING=false in
frontend/index.html). If no Podcast Index credentials are present this exits 0
without writing anything, so it never blocks the rest of the pipeline.

Run: python scraper/trending.py
"""

import hashlib
import json
import sys
import time
from datetime import datetime, timezone

import requests

from hosts import detect_host
from scrape import HEADERS, LEADERBOARD_PATH, TIMEOUT, fetch_show_feed, log, strip_html
from spotify_scrape import pi_creds

FEED_PAUSE = 0.4  # polite pacing between the per-show RSS fetches

ROOT = LEADERBOARD_PATH.parent.parent
OUTPUT_PATH = ROOT / "data" / "trending.json"
TRENDING_API = "https://api.podcastindex.org/api/1.0/podcasts/trending?max={max}&lang=en"
MAX_ITEMS = 60


def main() -> int:
    key, secret = pi_creds()
    if not key:
        log("trending: no Podcast Index credentials; skipping (not an error)")
        return 0

    epoch = str(int(time.time()))
    auth = hashlib.sha1((key + secret + epoch).encode()).hexdigest()
    headers = {**HEADERS, "User-Agent": "podcast-leaderboard/1.0",
               "X-Auth-Key": key, "X-Auth-Date": epoch, "Authorization": auth}
    try:
        r = requests.get(TRENDING_API.format(max=MAX_ITEMS), headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        feeds = r.json().get("feeds", [])
    except Exception as exc:
        log(f"trending: fetch failed: {exc}")
        return 0  # never block the pipeline

    items = []
    for f in feeds:
        feed_url = f.get("url", "") or ""
        # Fetch the RSS once for host detection AND contact metadata. PI's
        # trending payload has no itunes:owner email or owner name; those live
        # only in the feed, so without this the contact card shows blanks even
        # when the feed clearly carries them.
        if feed_url:
            host, meta = fetch_show_feed(feed_url)
            time.sleep(FEED_PAUSE)
        else:
            host, meta = detect_host(feed_url), {}
        # Prefer the feed's own description; fall back to PI's.
        desc = meta.get("description")
        if not desc:
            desc = " ".join(strip_html(f.get("description") or "").split())
            if len(desc) > 240:
                desc = desc[:240].rstrip() + "…"
        cats = list((f.get("categories") or {}).values())
        npub = f.get("newestItemPubdate")
        items.append({
            "title": f.get("title") or "Untitled",
            "author": meta.get("itunes_author") or f.get("author") or "",
            "image": f.get("image") or f.get("artwork") or "",
            "feed_url": feed_url,
            "website": meta.get("website") or f.get("link") or "",
            "owner_name": meta.get("owner_name") or "",
            "owner_email": meta.get("owner_email") or "",
            "cadence": meta.get("cadence"),
            "schedule": meta.get("schedule"),
            "rss_video": meta.get("rss_video"),
            "episode_count": f.get("episodeCount") or None,
            "last_published": time.strftime("%Y-%m-%d", time.gmtime(npub)) if npub else None,
            "host": host,
            "categories": cats,
            "trend_score": f.get("trendScore", 0),
            "description": desc or None,
        })
    items.sort(key=lambda x: x["trend_score"], reverse=True)

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "Podcast Index trending (open-ecosystem listening, not Apple/Spotify)",
        "items": items,
    }
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log(f"trending: wrote {len(items)} items to {OUTPUT_PATH}")
    if items:
        log("trending top hosts: " + ", ".join(
            f"{i['title'][:30]} [{i['host']}]" for i in items[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
