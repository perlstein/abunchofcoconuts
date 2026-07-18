"""Cumulative host-change tracker -> data/tracked_feeds.json + data/host_moves.json.

Why this exists: scrape.py / spotify_scrape.py only detect host changes among
shows that are *currently* charting. A show that charted last month and switched
hosts this month is invisible, because its feed is never re-fetched once it
drops off the chart. This module keeps a growing corpus of every feed we have
ever seen (charts + trending + a manual watchlist) and re-checks it on a rolling
budget, so migrations are caught long after a show leaves the charts.

Cost control: feeds that charted or trended *this run* are free (their host was
already resolved upstream; we just read it back from the leaderboard/trending
JSON). Beyond those, we re-fetch the stalest feeds within a wall-clock budget
(DEADLINE_SECONDS), oldest first, so per-run cost stays bounded by time as the
corpus grows; only the full-cycle length stretches. Watchlist feeds are always
checked, before the deadline can bite.

The re-check batch is fetched WORKERS-at-a-time (a thread pool): this is pure
I/O wait (each feed is one HTTP GET), so concurrency is close to free -- a
16-worker pool measured 0.26s/feed on a real 1200-feed batch against this
corpus's actual host mix (a live end-to-end test, not a projection), vs
~1.3s/feed sequential, and (more importantly) one slow/hung host no longer
stalls every feed queued behind it. At that rate a 35-minute box covers on
the order of ~8k feeds -- roughly the whole current off-run corpus -- so in
practice every run still re-checks (nearly) everything, keeping migration
detection latency at "next scheduled run." But coverage is bounded by TIME,
not a feed count (see DEADLINE_SECONDS): if the corpus grows past what fits,
the run stops cleanly at the deadline having done the stalest feeds first,
and the rest come around next run. Nothing to retune and no job timeout to
blow as it grows. State mutation (apply_observation) always happens back in
the main thread as results land, never inside a worker, so there's no
locking needed on the shared `state` dict.

Outputs:
  data/tracked_feeds.json  full corpus state, keyed by feed URL (compact)
  data/host_moves.json     derived list of host changes, newest first (frontend)

No credentials needed (only fetches public RSS). Never blocks the pipeline.

Run: python scraper/cumulative.py
"""

import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from hosts import UNMATCHED
from scrape import LEADERBOARD_PATH, ROOT, fetch_show_feed, log

TRACKED_PATH = ROOT / "data" / "tracked_feeds.json"
MOVES_PATH = ROOT / "data" / "host_moves.json"
CUMULATIVE_HISTORY_PATH = ROOT / "data" / "cumulative_history.json"
WATCHLIST_PATH = ROOT / "data" / "watchlist.csv"
CHANNEL_FEEDS_PATH = ROOT / "data" / "channel_feeds.csv"  # auto-built by channels.py
NETWORK_FEEDS_PATH = ROOT / "data" / "network_feeds.csv"  # auto-built by networks.py
SUBGENRE_FEEDS_PATH = ROOT / "data" / "subgenre_feeds.csv"  # auto-built by subgenres.py
# Blended, label-less corpus of bare feed URLs: the seed universe tracked for
# host changes. Carries no labels or attribution. Optional (absent in dev).
CORPUS_FEEDS_PATH = ROOT / "data" / "corpus_feeds.csv"
SPOTIFY_LEADERBOARD_PATH = ROOT / "data" / "spotify_leaderboard.json"
TRENDING_PATH = ROOT / "data" / "trending.json"

# The re-check is time-boxed, not count-boxed: it works watchlist-first, then
# stalest-first, until DEADLINE_SECONDS elapses, then stops cleanly and writes
# what it got. This self-corrects -- if the corpus doubles, each run still
# fits the deadline and stalest-first ordering guarantees everything keeps
# rotating through, just over more runs; no budget to retune, no timeout to
# blow. Override the deadline per-workflow via CUMULATIVE_DEADLINE_SECONDS
# (e.g. a slim daily move-scan job could set 900 = 15 min while the main
# every-other-day run uses the default 35 min).
DEADLINE_SECONDS = int(os.environ.get("CUMULATIVE_DEADLINE_SECONDS", 35 * 60))
WAVE_SIZE = 500         # feeds submitted per wave; the clock is checked between
                        # waves, so this is the granularity of the deadline.
HARD_CAP = 15000        # absolute ceiling on feeds/run, backstop only -- the
                        # deadline stops us long before this in practice.
WORKERS = 16            # concurrent feed fetches; spread across hundreds of
                        # distinct hosts in this corpus, so ~16 in flight is
                        # a couple connections per host at most, not a hammer.


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _confident(host: str) -> bool:
    """A host firm enough to count as a real placement. A transient 'Unknown'
    or an unmatched/self-hosted feed never triggers a (false) migration."""
    return bool(host) and host not in ("Unknown", UNMATCHED)


def build_corpus_snapshot(state, scan_date, *, synthetic=False):
    """Aggregate the tracked-feed corpus into one history entry, shaped exactly
    like a data/history.json entry so the frontend's existing bump + share
    charts render it unchanged. Only feeds with a confident host count
    (apply_observation never stores Unknown/Other-Self-Hosted as a host, so a
    missing `host` just means "not confidently resolved" and is skipped)."""
    counts = {}
    for st in state.values():
        host = st.get("host")
        if host:
            counts[host] = counts.get(host, 0) + 1
    total = sum(counts.values())
    platforms = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    entry = {
        "date": scan_date,
        "total_shows": total,
        "platforms": [{
            "name": name,
            "count": n,
            "share": round(n / total * 100, 1) if total else 0.0,
            "rank": rank,
        } for rank, (name, n) in enumerate(platforms, 1)],
    }
    if synthetic:
        entry["synthetic"] = True
    return entry


def append_cumulative_history(entry):
    """Append one corpus snapshot to cumulative_history.json, replacing any
    entry with the same date (a same-day rerun updates rather than duplicates).
    Same file shape as history.json; no compaction -- the corpus grows slowly
    and synthetic backfill entries are kept so the chart stays dashed-then-solid."""
    hist = _load_json(CUMULATIVE_HISTORY_PATH) or {
        "schema_version": 1, "first_entry": None, "last_entry": None, "entries": []}
    entries = [e for e in hist.get("entries", []) if e.get("date") != entry["date"]]
    entries.append(entry)
    entries.sort(key=lambda e: e["date"])
    hist["entries"] = entries
    hist["first_entry"] = entries[0]["date"]
    hist["last_entry"] = entries[-1]["date"]
    hist["schema_version"] = 1
    CUMULATIVE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUMULATIVE_HISTORY_PATH.write_text(json.dumps(hist, indent=2, ensure_ascii=False))
    return len(entries)


def apply_observation(state, feed_url, host, scan_date, *, title=None, artwork=None, source=None):
    """Fold one (feed_url, host) observation into the corpus, recording a move
    when a feed switches between two confident hosts."""
    if not feed_url:
        return
    st = state.get(feed_url)
    if st is None:
        st = state[feed_url] = {"first_seen": scan_date}
    if title:
        st["title"] = title
    if artwork and not st.get("artwork"):
        st["artwork"] = artwork
    if source and st.get("source") != "watchlist":  # watchlist label is sticky
        st["source"] = source
    st["last_checked"] = scan_date
    if _confident(host):
        if not st.get("host"):
            st["host"], st["since"] = host, scan_date
        elif st["host"] != host:
            st["prev_host"], st["moved_on"] = st["host"], scan_date
            st["host"], st["since"] = host, scan_date


def free_observations(scan_date):
    """Feeds whose host was already resolved this run (charts + trending), so
    re-fetching them would be wasted work. Chart data wins over trending when a
    feed appears in both (the chart's platform name is the curated label).

    Also returns moves the per-show host-state tracking already detected
    (prev_host/moved_on annotations on chart shows), so a migration found
    before this corpus existed is never lost when the corpus is the UI source.
    """
    obs = {}          # feed_url -> [host, title, artwork, source, priority]
    known_moves = {}  # feed_url -> (prev_host, moved_on)

    def add(feed_url, host, title, artwork, source, priority):
        if not feed_url:
            return
        cur = obs.get(feed_url)
        if cur is None or priority > cur[4]:
            obs[feed_url] = [host, title, artwork, source, priority]

    for path in (LEADERBOARD_PATH, SPOTIFY_LEADERBOARD_PATH):
        d = _load_json(path)
        if not d:
            continue
        for p in d.get("platforms", []):
            host = p.get("name")
            for s in p.get("shows", []):
                fu = s.get("feed_url")
                add(fu, host, s.get("title"), s.get("artwork"), "chart", 2)
                if fu and s.get("prev_host") and s.get("moved_on"):
                    known_moves[fu] = (s["prev_host"], s["moved_on"])

    t = _load_json(TRENDING_PATH)
    if t:
        for it in t.get("items", []):
            add(it.get("feed_url"), it.get("host"), it.get("title"), it.get("image"), "trending", 1)
    return obs, known_moves


def load_watchlist():
    """Parse the manual watchlist plus the auto-built channel feeds. Tolerant:
    skips blanks and #-comments, accepts a header row, accepts bare-URL lines
    (label optional), and dedupes by feed URL (manual rows win).

    Only the feed URL and an optional display label are read; any extra columns
    are ignored, and outputs carry no other metadata."""
    out = {}
    for path in (WATCHLIST_PATH, CHANNEL_FEEDS_PATH, NETWORK_FEEDS_PATH):
        if not path.exists():
            continue
        manual = path == WATCHLIST_PATH
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = next(csv.reader([line]))
            url = (parts[0] if parts else "").strip()
            if not url.lower().startswith("http"):
                continue  # silently skips the header row and any junk
            if url in out and not manual:
                continue  # manual watchlist row already set it
            out[url] = {
                "feed_url": url,
                "label": (parts[1].strip() if len(parts) > 1 else "") or None,
            }
    return list(out.values())


def load_seed_feeds():
    """Rotating-corpus seed feeds (e.g. sub-genre charts): tracked for host
    changes but NOT always-checked. They ride the stale re-check budget, so
    adding thousands does not raise per-run cost. Returns [(feed_url, label)].

    Reads both the labeled sub-genre feeds (private/dev) and the blended
    label-less corpus_feeds.csv (the public engine's whole seed universe).
    Deduped by URL; the first occurrence's label wins."""
    out, seen = [], set()
    for path in (SUBGENRE_FEEDS_PATH, CORPUS_FEEDS_PATH):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = next(csv.reader([line]))
            url = (parts[0] if parts else "").strip()
            if not url.lower().startswith("http") or url in seen:
                continue
            seen.add(url)
            out.append((url, (parts[1].strip() if len(parts) > 1 else "") or None))
    return out


def main() -> int:
    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = _load_json(TRACKED_PATH) or {}
    log(f"cumulative: loaded {len(state)} tracked feeds")

    # 1. Apply this run's free observations (charts + trending).
    obs, known_moves = free_observations(scan_date)
    for feed_url, (host, title, artwork, source, _) in obs.items():
        apply_observation(state, feed_url, host, scan_date,
                          title=title, artwork=artwork, source=source)
    log(f"cumulative: {len(obs)} feeds observed free this run")

    # 1b. Backfill moves the per-show host-state tracking already found, so a
    #     migration detected before this corpus existed still surfaces. Only
    #     fills a gap; never clobbers a move the corpus detected itself.
    imported = 0
    for feed_url, (prev_host, moved_on) in known_moves.items():
        st = state.get(feed_url)
        if st and not st.get("moved_on") and prev_host and prev_host != st.get("host"):
            st["prev_host"], st["moved_on"] = prev_host, moved_on
            imported += 1
    if imported:
        log(f"cumulative: backfilled {imported} moves from chart host-state annotations")

    # 2. Seed/refresh watchlist metadata (sticky source + label only).
    watch = load_watchlist()
    watch_urls = set()
    for w in watch:
        url = w["feed_url"]
        watch_urls.add(url)
        st = state.setdefault(url, {"first_seen": scan_date})
        st["source"] = "watchlist"
        if w["label"]:
            st["label"] = w["label"]
    if watch:
        log(f"cumulative: {len(watch)} watchlist feeds loaded")

    # 2b. Seed rotating-corpus feeds (sub-genre charts). Added with NO
    #     last_checked so they sort oldest and the rotation host-checks them
    #     over the next few runs; never added to the always-check watchlist.
    seeds = load_seed_feeds()
    seeded_new = 0
    for url, label in seeds:
        if url not in state:
            state[url] = {"first_seen": scan_date, "source": "subgenre"}
            if label:
                state[url]["title"] = label
            seeded_new += 1
    if seeds:
        log(f"cumulative: {len(seeds)} seed feeds ({seeded_new} new to the corpus)")

    # 3. Order the re-check candidates: watchlist first (never deferred by the
    #    deadline), then the stalest others. Feeds seen free this run are skipped.
    fresh = set(obs)
    candidates = [u for u in state if u not in fresh]
    must = [u for u in candidates if u in watch_urls]
    rest = [u for u in candidates if u not in watch_urls]
    rest.sort(key=lambda u: state[u].get("last_checked") or "")  # oldest first
    ordered = (must + rest)[:HARD_CAP]

    # SEED_ONLY: write the seeded corpus now and skip the (slow) re-checks. Used
    # to fold a big batch of new seed feeds in immediately; the scheduled runs
    # then host-check them.
    if os.environ.get("CUMULATIVE_SEED_ONLY") == "1":
        log(f"cumulative: SEED_ONLY set, skipping {len(ordered)} re-checks")
    else:
        log(f"cumulative: re-checking up to {len(ordered)} feeds "
            f"({len(must)} watchlist always-first, then stalest) "
            f"of {len(candidates)} off-run candidates, {WORKERS} at a time, "
            f"deadline {DEADLINE_SECONDS // 60} min")
        # 4. Re-fetch in waves until the deadline. Each wave is fetched
        #    concurrently (pure I/O wait; one slow/hung host no longer stalls
        #    the queue). State mutation (apply_observation) happens back here in
        #    the main thread as results land, so `state` needs no lock. The
        #    watchlist sits at the front of `ordered`, so it's always covered
        #    before the clock can run out. Stalest-first means whatever a run
        #    can't reach is simply the freshest, and it comes around next run.
        start = time.monotonic()
        done = 0
        stopped_early = False
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for wave_start in range(0, len(ordered), WAVE_SIZE):
                wave = ordered[wave_start:wave_start + WAVE_SIZE]
                futures = {pool.submit(fetch_show_feed, u): u for u in wave}
                for fut in as_completed(futures):
                    feed_url = futures[fut]
                    try:
                        host, _meta = fut.result()
                    except Exception:
                        host = None  # never let one bad feed take down the run
                    if host is not None:
                        apply_observation(state, feed_url, host, scan_date)
                    done += 1
                elapsed = time.monotonic() - start
                log(f"cumulative: re-checked {done}/{len(ordered)} ({elapsed / 60:.1f} min)")
                # Only the deadline can stop us mid-corpus, and only after the
                # watchlist wave(s) are already done (watchlist < WAVE_SIZE).
                if elapsed >= DEADLINE_SECONDS and done >= len(must):
                    stopped_early = wave_start + WAVE_SIZE < len(ordered)
                    break
        if stopped_early:
            log(f"cumulative: hit {DEADLINE_SECONDS // 60}-min deadline at "
                f"{done}/{len(ordered)}; remaining {len(ordered) - done} are the "
                f"freshest and roll to the next run")

    moved_today = [u for u, s in state.items() if s.get("moved_on") == scan_date]
    log(f"cumulative: {len(moved_today)} host changes detected this run")

    # 5. Persist the corpus (compact) and the derived moves list (readable).
    TRACKED_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACKED_PATH.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=False))

    moves = []
    for url, s in state.items():
        if s.get("moved_on") and s.get("prev_host"):
            moves.append({
                "title": s.get("title") or s.get("label") or "",
                "feed_url": url,
                "from": s["prev_host"],
                "to": s.get("host"),
                "date": s["moved_on"],
                "source": s.get("source"),
                "artwork": s.get("artwork") or "",
            })
    moves.sort(key=lambda m: m["date"], reverse=True)
    MOVES_PATH.write_text(json.dumps(
        {"generated": scan_date, "total_tracked": len(state), "moves": moves},
        indent=2, ensure_ascii=False))
    log(f"cumulative: wrote {len(state)} tracked feeds, {len(moves)} total moves")

    # Append today's corpus-wide hosting snapshot ("All Feeds" time series).
    snap = build_corpus_snapshot(state, scan_date)
    n = append_cumulative_history(snap)
    log(f"cumulative: corpus snapshot {snap['total_shows']} hosted feeds "
        f"across {len(snap['platforms'])} hosts ({n} history entries total)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never block the pipeline
        log(f"cumulative: unexpected error, skipping: {exc}")
        sys.exit(0)
