"""One-time approximate backfill of data/cumulative_history.json.

The "All Feeds" hosting time series (built by cumulative.py) only starts
accumulating real points from the day it's first run. This script reconstructs
an approximate history from what the corpus already knows: each feed's
`first_seen` date and any recorded host move (`prev_host` / `moved_on`).

For a past date D, a feed counts toward the corpus if `first_seen <= D`, and
its host on that date is `prev_host` when a move happened after D (`moved_on`
is later than D), otherwise its current `host`. Feeds with no confident host
are skipped, same as the live snapshot.

Every reconstructed entry is marked `"synthetic": true`, so the frontend bump
chart draws it dashed with hollow points -- the same treatment as seeded
placeholder history -- and it reads clearly as an approximation, not a
measured snapshot. Real entries appended by cumulative.py sit alongside these
and render solid; there is no retirement mechanic (the approximation is
retained, it just gets progressively overtaken by real data).

Approximation caveats (acceptable, and the dashed styling signals them):
  - A feed whose host changed *before* corpus tracking began reconstructs at
    its current host for the whole window (we only know moves we recorded).
  - `first_seen` is when the feed entered *our* corpus (any channel), not
    necessarily when it was created, so early dates under-count.

Run once, locally, then commit data/cumulative_history.json:

    python scraper/backfill_cumulative.py
"""

import json
import sys
from datetime import datetime, timedelta, timezone

from cumulative import (
    CUMULATIVE_HISTORY_PATH,
    TRACKED_PATH,
    _load_json,
    build_corpus_snapshot,
)
from scrape import log

STEP_DAYS = 7  # weekly reconstructed points


def host_on(st, date):
    """The feed's host as of `date`, or None if it wasn't in the corpus yet or
    had no confident host then."""
    first_seen = st.get("first_seen")
    if not first_seen or first_seen > date:
        return None
    moved_on, prev_host = st.get("moved_on"), st.get("prev_host")
    if moved_on and prev_host and moved_on > date:
        return prev_host
    return st.get("host")


def snapshot_as_of(state, date):
    """A build_corpus_snapshot-shaped entry reconstructed for a past date."""
    as_of = {url: {"host": host_on(st, date)} for url, st in state.items()}
    return build_corpus_snapshot(as_of, date, synthetic=True)


def main() -> int:
    state = _load_json(TRACKED_PATH)
    if not state:
        log("backfill_cumulative: no tracked_feeds.json, nothing to do")
        return 0

    seens = [st.get("first_seen") for st in state.values() if st.get("first_seen")]
    if not seens:
        log("backfill_cumulative: corpus has no first_seen dates, nothing to do")
        return 0

    start = datetime.strptime(min(seens), "%Y-%m-%d").date()
    # Stop the day before today so the live run owns the current (real) date.
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    if end < start:
        end = start

    existing = _load_json(CUMULATIVE_HISTORY_PATH) or {
        "schema_version": 1, "first_entry": None, "last_entry": None, "entries": []}
    # Keep any real (non-synthetic) entries already present; only (re)build the
    # synthetic reconstruction so re-running the backfill is idempotent.
    kept = [e for e in existing.get("entries", []) if not e.get("synthetic")]
    real_dates = {e["date"] for e in kept}

    entries = list(kept)
    added = 0
    d = start
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        if ds not in real_dates:
            entries.append(snapshot_as_of(state, ds))
            added += 1
        d += timedelta(days=STEP_DAYS)

    entries.sort(key=lambda e: e["date"])
    out = {
        "schema_version": 1,
        "first_entry": entries[0]["date"],
        "last_entry": entries[-1]["date"],
        "entries": entries,
    }
    CUMULATIVE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUMULATIVE_HISTORY_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log(f"backfill_cumulative: wrote {added} synthetic weekly entries "
        f"({start} .. {end}), kept {len(kept)} real, {len(entries)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
