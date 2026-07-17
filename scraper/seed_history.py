"""Seed placeholder history so the bump chart is viewable before real data.

Creates weekly entries going back ~6 months from the most recent real
entry, marked with "synthetic": true. Counts follow a gentle deterministic
random walk per platform (Megaphone is pinned safely at #1) so rank swaps
and share movement are visible for QC, while staying anchored to today's
real values. The frontend draws these dashed/hollow and labels them as
placeholders, and scrape.py retires one synthetic entry per real daily
scan (oldest first) until none remain.

Run: python scraper/seed_history.py
Re-running regenerates all placeholder entries; real entries are untouched.
"""

import json
import random
import sys
from datetime import datetime, timedelta

from scrape import (HISTORY_PATH, SYNTHETIC_BUDGET, compact_history,
                    load_history, log, prune_synthetic)

WEEKLY_DRIFT = 0.07  # max +/- fractional count change per week per platform


def main() -> int:
    history = load_history()
    real = [e for e in history["entries"] if not e.get("synthetic")]
    if not real:
        log("ERROR: history.json has no real entries to seed from; run the scraper first")
        return 1

    anchor = max(real, key=lambda e: e["date"])
    anchor_date = datetime.strptime(anchor["date"], "%Y-%m-%d").date()
    real_dates = {e["date"] for e in real}

    rng = random.Random(42)  # deterministic: re-runs produce identical data
    factors = {p["name"]: 1.0 for p in anchor["platforms"]}
    synthetic = []
    # Walk backwards from the anchor so recent weeks stay closest to today
    for week in range(1, SYNTHETIC_BUDGET + 1):
        date = (anchor_date - timedelta(weeks=week)).isoformat()
        if date in real_dates:
            continue
        for name in factors:
            factors[name] *= 1 + rng.uniform(-WEEKLY_DRIFT, WEEKLY_DRIFT)
        counts = {}
        for p in anchor["platforms"]:
            counts[p["name"]] = max(1, round(p["count"] * factors[p["name"]]))
        # Pin Megaphone at #1: keep it comfortably above the runner-up
        runner_up = max(c for n, c in counts.items() if n != "Megaphone")
        counts["Megaphone"] = max(counts["Megaphone"], round(runner_up * 1.5))
        total = sum(counts.values())
        platforms = sorted(
            ({"name": n, "count": c, "share": round(c / total * 100, 1)}
             for n, c in counts.items()),
            key=lambda p: (-p["count"], p["name"]))
        for rank, p in enumerate(platforms, 1):
            p["rank"] = rank
        synthetic.append({
            "date": date,
            "total_shows": total,
            "platforms": platforms,
            "synthetic": True,
        })

    entries = compact_history(prune_synthetic(real + synthetic))
    history["entries"] = entries
    history["first_entry"] = entries[0]["date"]
    history["last_entry"] = entries[-1]["date"]
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    synth_count = sum(1 for e in entries if e.get("synthetic"))
    log(f"Seeded placeholder history from {anchor['date']} rankings with variance; "
        f"history now has {len(entries)} entries ({synth_count} synthetic)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
