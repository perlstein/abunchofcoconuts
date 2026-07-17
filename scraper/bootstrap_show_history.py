"""Create the first show_history.json without a full scrape.

Fetches only the 19 category charts (no per-show RSS, so ~15 seconds),
captures rank, writes today's real entry, and seeds ~8 weekly placeholder
entries with gentle rank movement so the Show Trajectory view is usable
immediately. Placeholders are marked synthetic and retired one-per-real-scan
by append_show_history (via prune_synthetic), exactly like the platform chart.

Run once: python scraper/bootstrap_show_history.py
"""

import json
import random
import sys
from datetime import datetime, timedelta, timezone

from scrape import (CATEGORIES, SHOW_HISTORY_PATH, build_show_history_entry,
                    compact_history, fetch_charts, log, prune_synthetic)

SYNTHETIC_WEEKS = 8


def shuffled_charts(base_charts: dict, week: int) -> dict:
    """Jitter each category's order slightly; movement grows with age."""
    out = {}
    for ci, (cat, ids) in enumerate(base_charts.items()):
        rng = random.Random(1000 * week + ci)
        sigma = 1.5 + 0.4 * week  # older weeks drift a bit more
        keyed = sorted(((i + rng.gauss(0, sigma), sid) for i, sid in enumerate(ids)),
                       key=lambda p: p[0])
        out[cat] = [sid for _, sid in keyed]
    return out


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_date = datetime.strptime(today, "%Y-%m-%d").date()

    log("Fetching category charts (no RSS, ~15s)...")
    shows = fetch_charts(CATEGORIES)
    if not shows:
        log("ERROR: no charts fetched")
        return 1

    real = build_show_history_entry(shows, today)
    entries = [real]
    for week in range(1, SYNTHETIC_WEEKS + 1):
        date = (today_date - timedelta(weeks=week)).isoformat()
        entries.append({
            "date": date,
            "charts": shuffled_charts(real["charts"], week),
            "synthetic": True,
        })
    entries = compact_history(prune_synthetic(entries))

    directory = {s["itunes_id"]: {"title": s["title"], "artwork": s["artwork"]}
                 for s in shows.values()}
    referenced = {sid for e in entries for ids in e["charts"].values() for sid in ids}
    directory = {sid: m for sid, m in directory.items() if sid in referenced}

    history = {
        "schema_version": 1,
        "first_entry": entries[0]["date"],
        "last_entry": entries[-1]["date"],
        "entries": entries,
        "shows": directory,
    }
    SHOW_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHOW_HISTORY_PATH.write_text(
        json.dumps(history, separators=(",", ":"), ensure_ascii=False))
    synth = sum(1 for e in entries if e.get("synthetic"))
    log(f"Wrote show_history.json: {len(entries)} entries ({synth} synthetic), "
        f"{len(directory)} shows in directory, "
        f"{SHOW_HISTORY_PATH.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
