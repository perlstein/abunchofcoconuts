"""Detect Apple-native video podcasts -> data/apple_video_state.json.

Shows like BigDeal or Baby This is Keke Palmer stream video inside Apple
Podcasts, but their public RSS stays audio-only (verified: known video shows
tested had audio/mpeg-only feeds). The reliable public signal is the show's
Apple Podcasts page: episodes carry "mediaType":"video" in the embedded
serialized data for video shows and "mediaType":"audio" for audio-only shows
(verified on both real positives and real negatives, incl. video-forward
YouTube shows like Call Her Daddy that turn out NOT to be Apple-video --
Apple's native video feature is a separate opt-in from having video
elsewhere, gated to specific hosting platforms: Acast, ART19, Omny Studio,
and Simplecast at launch).

IMPORTANT: a show's page also embeds "podcastOffer" blocks for OTHER shows
Apple recommends alongside it ("Listeners Also Subscribed To" etc.), and
those carry their own mediaType too. A blind substring search for the marker
anywhere on the page gives false positives whenever a recommended show
happens to be video (caught live: The Vanished Podcast's page contains a
video marker, but it belongs to Dr. Death, a recommended show -- Vanished's
own RSS is audio-only mp3s). So the marker is only trusted when it's paired,
within the same object, with a "podcastOffer" carrying the TARGET show's own
adamId (see OWN_VIDEO_RE).

Cost control: one page fetch per show (~0.6-1.4 MB), WORKERS at a time (a
thread pool -- pure I/O wait, so concurrency is close to free; kept more
conservative than cumulative.py's since every fetch here hits the same
single host). Measured live: 8 workers did 200 real shows in 37s
(0.18s/show, no errors), so the whole current chart (~1.7k) clears in ~5 min.
The run is time-boxed (DEADLINE_SECONDS), not count-boxed: it checks the seed
list first (always), then chart shows stalest-first, until the clock runs
out -- so it self-corrects if the chart grows, same as cumulative.py. The
seed list (data/apple_video_seed.csv) pins specific known/candidate shows to
re-check regardless of whether they're currently charting.

Outputs:
  data/apple_video_state.json  {itunes_id: {video, checked}}, one entry per
    show ever checked.
  data/apple_video_shows.json  full metadata (title, artwork, host, feed_url,
    episode_count, last_published) for every confirmed video show, sourced
    from the current board when charting or a fresh iTunes lookup otherwise.
    This is what the frontend's Apple Video tab reads, so a confirmed show
    stays visible even if it drops off the chart.
  data/leaderboard.json patched in place so this run's results ship in the
    same CI run the scraper stamps from state anyway.

Run: python scraper/apple_video.py
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

from hosts import detect_host
from scrape import HEADERS, LEADERBOARD_PATH, ROOT, TIMEOUT, log

STATE_PATH = ROOT / "data" / "apple_video_state.json"
SHOWS_PATH = ROOT / "data" / "apple_video_shows.json"
SEED_PATH = ROOT / "data" / "apple_video_seed.csv"
SHOW_URL = "https://podcasts.apple.com/us/podcast/id{itunes_id}"
LOOKUP_URL = "https://itunes.apple.com/lookup?id={itunes_id}&entity=podcast"
# Time-boxed like cumulative.py: seed list first (always), then stalest chart
# shows, until DEADLINE_SECONDS. Measured 0.18s/show at 8 workers, so a 12-min
# box clears the whole current chart (~1.7k) with margin; if the chart grows
# past what fits, the freshest simply roll to the next run. Override per
# workflow via APPLE_VIDEO_DEADLINE_SECONDS.
DEADLINE_SECONDS = int(os.environ.get("APPLE_VIDEO_DEADLINE_SECONDS", 12 * 60))
WAVE_SIZE = 300         # shows per wave; clock checked between waves.
HARD_CAP = 4000         # absolute ceiling on shows/run, backstop only.
WORKERS = 8             # every request here hits the SAME host
                        # (podcasts.apple.com), unlike cumulative.py's spread
                        # across hundreds of hosts, so this stays more
                        # conservative -- 8 concurrent connections to one
                        # server is normal browser-level concurrency, not a
                        # hammer.
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")}

# Matches a "podcastOffer" block whose own adamId is followed (within the same
# enclosing object -- no intervening "{" or "}") by a mediaType:video marker.
# Captures the adamId so callers can check it's the TARGET show, not a
# recommended one embedded on the same page. See module docstring.
OWN_VIDEO_RE = re.compile(r'"podcastOffer":\{"title":"[^"]*","adamId":"(\d+)"[^{}]*\}[^{}]*"mediaType":"video"')


def has_apple_video(itunes_id: str) -> "bool | None":
    """True/False from the show page, None when the page can't be read.
    Only counts a video marker that's tied to THIS show's own adamId, not one
    borrowed from a recommended show embedded on the same page."""
    try:
        r = requests.get(SHOW_URL.format(itunes_id=itunes_id), headers=UA, timeout=25)
        if r.status_code != 200 or len(r.text) < 10000:
            return None
        dec = urllib.parse.unquote(r.text)
        return str(itunes_id) in set(OWN_VIDEO_RE.findall(dec))
    except Exception:
        return None


def load_seed_ids() -> list:
    """data/apple_video_seed.csv: itunes_id,label -- always re-checked, so a
    confirmed or candidate show stays verified regardless of chart status."""
    if not SEED_PATH.exists():
        return []
    out = []
    for line in SEED_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader([line]))
        iid = (parts[0] if parts else "").strip()
        if iid.isdigit():
            out.append(iid)
    return out


def itunes_show_meta(itunes_id: str) -> "dict | None":
    """Fresh iTunes lookup for a show not on the current board: title,
    artwork, feed_url, host, episode_count, last_published."""
    try:
        r = requests.get(LOOKUP_URL.format(itunes_id=itunes_id), headers=HEADERS, timeout=TIMEOUT)
        res = r.json().get("results", [])
    except Exception:
        return None
    if not res:
        return None
    r0 = res[0]
    feed = r0.get("feedUrl") or ""
    last = r0.get("releaseDate")
    return {
        "itunes_id": itunes_id,
        "title": r0.get("collectionName") or r0.get("trackName") or "Untitled",
        "artwork": r0.get("artworkUrl600") or r0.get("artworkUrl100") or "",
        "feed_url": feed,
        "host": detect_host(feed) if feed else "Unknown",
        "episode_count": r0.get("trackCount"),
        "last_published": last[:10] if last else None,
    }


def build_shows_file(state: dict, board: dict) -> int:
    """Write data/apple_video_shows.json: full metadata for every confirmed
    video show, board data preferred (free), iTunes lookup as fallback for
    shows that aren't (or are no longer) charting."""
    board_by_id = {}
    for p in board.get("platforms", []):
        for s in p.get("shows", []):
            if s.get("itunes_id"):
                board_by_id[str(s["itunes_id"])] = {**s, "host": p["name"]}

    video_ids = [iid for iid, st in state.items() if st.get("video")]
    out = []
    for iid in video_ids:
        b = board_by_id.get(iid)
        if b:
            out.append({
                "itunes_id": iid, "title": b.get("title", "Untitled"),
                "artwork": b.get("artwork", ""), "feed_url": b.get("feed_url", ""),
                "host": b.get("host", "Unknown"), "episode_count": b.get("episode_count"),
                "last_published": b.get("last_published"), "on_chart": True,
            })
        else:
            meta = itunes_show_meta(iid)
            time.sleep(0.3)
            if meta:
                out.append({**meta, "on_chart": False})
    out.sort(key=lambda s: (s.get("episode_count") or 0), reverse=True)
    SHOWS_PATH.write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "shows": out},
        indent=2, ensure_ascii=False))
    return len(out)


def main() -> int:
    try:
        board = json.loads(LEADERBOARD_PATH.read_text())
    except Exception as exc:
        log(f"apple_video: no leaderboard to scan: {exc}")
        return 0
    chart_shows = [(str(s["itunes_id"]), s.get("title", ""))
                   for p in board.get("platforms", []) for s in p.get("shows", [])
                   if s.get("itunes_id")]

    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except ValueError:
            state = {}
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    seed_ids = load_seed_ids()
    if seed_ids:
        log(f"apple_video: {len(seed_ids)} seed ids (always checked first)")
    # Seed list first (always, never deferred), then chart shows stalest-first.
    chart_shows.sort(key=lambda x: state.get(x[0], {}).get("checked") or "")
    seed_set = {i for i, _ in chart_shows}
    seed_entries = [(iid, "") for iid in seed_ids if iid not in seed_set]
    ordered = (seed_entries + chart_shows)[:HARD_CAP]
    titles = dict(ordered)
    log(f"apple_video: checking up to {len(ordered)} shows "
        f"({len(seed_entries)} seed always-first, then {len(chart_shows)} charting "
        f"stalest-first), {WORKERS} at a time, deadline {DEADLINE_SECONDS // 60} min")

    found = errors = done = 0
    start = time.monotonic()
    stopped_early = False
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for wave_start in range(0, len(ordered), WAVE_SIZE):
            wave = ordered[wave_start:wave_start + WAVE_SIZE]
            futures = {pool.submit(has_apple_video, iid): iid for iid, _ in wave}
            for fut in as_completed(futures):
                iid = futures[fut]
                try:
                    v = fut.result()
                except Exception:
                    v = None
                if v is None:
                    errors += 1
                    # bump checked so a persistently broken page doesn't hog the rotation
                    state.setdefault(iid, {"video": False})["checked"] = scan_date
                else:
                    state[iid] = {"video": v, "checked": scan_date}
                    if v:
                        found += 1
                        log(f"apple_video: VIDEO: {titles.get(iid, '')[:50] or iid}")
                done += 1
            elapsed = time.monotonic() - start
            # deadline can only stop us after the seed wave (seed < WAVE_SIZE)
            if elapsed >= DEADLINE_SECONDS and done >= len(seed_entries):
                stopped_early = wave_start + WAVE_SIZE < len(ordered)
                break
    if stopped_early:
        log(f"apple_video: hit {DEADLINE_SECONDS // 60}-min deadline at "
            f"{done}/{len(ordered)}; freshest roll to the next run")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))

    # Patch the freshly-scraped leaderboard so this run's results ship now
    # (the next scrape stamps from state anyway).
    stamped = 0
    for p in board.get("platforms", []):
        for s in p.get("shows", []):
            st = state.get(str(s.get("itunes_id")))
            if st is not None:
                s["apple_video"] = bool(st.get("video"))
                stamped += 1
    LEADERBOARD_PATH.write_text(json.dumps(board, indent=2, ensure_ascii=False))

    shows_written = build_shows_file(state, board)

    total_video = sum(1 for v in state.values() if v.get("video"))
    log(f"apple_video: batch found {found} new ({errors} errors); "
        f"{total_video} video shows known of {len(state)} checked; "
        f"stamped {stamped} on board; wrote {shows_written} to apple_video_shows.json")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never block the pipeline
        log(f"apple_video: unexpected error, skipping: {exc}")
        sys.exit(0)
