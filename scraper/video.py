"""Build a ranked video-podcast list from Podcast Index -> data/video.json.

Source: https://public.podcastindex.org/recommendations_video.json (no auth) —
PI's curated set of `medium=video` podcasts, each with a `popularity` score we
rank by.

WHAT THIS IS (and isn't): this is the OPEN podcast ecosystem's video set. In
practice it is dominated by PeerTube and alt-media instances (beetoons.tv,
blurt.media, vigilante.tv, cast.garden). It is NOT mainstream video podcasting:
YouTube, Spotify Video, and Apple's video shows are not here, and most top
audio-chart shows publish video on YouTube while their RSS stays audio (so they
never appear as "video" in any RSS-based source). Treat this as a niche-but-real
view of open video podcasting, not a market-wide ranking.

Easy to disable: drop the CI step or set FEATURE_VIDEO=false in the frontend.

Run: python scraper/video.py
"""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from scrape import HEADERS, LEADERBOARD_PATH, TIMEOUT, log

ROOT = LEADERBOARD_PATH.parent.parent
OUTPUT_PATH = ROOT / "data" / "video.json"
VIDEO_API = "https://public.podcastindex.org/recommendations_video.json"
MAX_ITEMS = 200

# Friendly names for the video hosts that actually show up here. Most are
# PeerTube instances; the rest fall back to the registrable domain.
VIDEO_HOSTS = {
    "beetoons.tv": "Beetoons (PeerTube)",
    "blurt.media": "Blurt",
    "vigilante.tv": "Vigilante.tv (PeerTube)",
    "dollarvigilante.tv": "Vigilante.tv (PeerTube)",
    "tdvdev.xyz": "Vigilante.tv (PeerTube)",
    "cast.garden": "Cast.garden (PeerTube)",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "rumble.com": "Rumble",
}


def video_host(url: str) -> str:
    net = (urlparse(url).netloc or "").lower().split(":")[0]
    for domain, name in VIDEO_HOSTS.items():
        if net == domain or net.endswith("." + domain):
            return name
    parts = net.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (net or "Unknown")


def main() -> int:
    try:
        r = requests.get(VIDEO_API, headers={**HEADERS, "User-Agent": "podcast-leaderboard/1.0"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log(f"video: fetch failed: {exc}")
        return 0  # never block the pipeline

    rows = data if isinstance(data, list) else data.get("feeds", [])
    items = []
    for x in rows:
        url = x.get("url", "") or ""
        items.append({
            "title": x.get("title") or "Untitled",
            "popularity": x.get("popularity", 0),
            "feed_url": url,
            "image": x.get("image") or "",
            "host": video_host(url),
            "feed_id": x.get("feedId"),
        })
    items.sort(key=lambda i: i["popularity"], reverse=True)
    items = items[:MAX_ITEMS]

    hosts = Counter(i["host"] for i in items)
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "Podcast Index video recommendations (open ecosystem; PeerTube-heavy, not YouTube/Spotify)",
        "total_available": len(rows),
        "hosts": [{"name": h, "count": c} for h, c in hosts.most_common()],
        "items": items,
    }
    OUTPUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log(f"video: wrote top {len(items)} of {len(rows)} to {OUTPUT_PATH}")
    log("video hosts: " + ", ".join(f"{h['name']} ({h['count']})" for h in out["hosts"][:6]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
