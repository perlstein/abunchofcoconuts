"""Seed data/apple_video_history.json from git history.

apple_video.py only started snapshotting the Apple-video count going forward,
but every past daily "Apple video scan" commit already carries that day's
apple_video_state.json (and a leaderboard.json stamped with apple_video). This
walks those commits and reconstructs one history entry per date, so the growth
chart isn't empty on day one.

Backfilled entries are marked "backfill": true and flagged coverage-limited:
the daily scan is a rotating, time-boxed check, so early corpus totals reflect
how much had been scanned, not only real adoption. The chart_video/chart_total
pair (video adoption among that day's charting shows) is the more stable read.

Existing (going-forward) entries are never overwritten. Run once:
    python scraper/backfill_apple_video_history.py
"""

import json
import subprocess
import sys

from scrape import ROOT

HISTORY_PATH = ROOT / "data" / "apple_video_history.json"
REF = "origin/main"  # walk the published history


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def _show_json(commit: str, path: str):
    out = _git("show", f"{commit}:{path}")
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def chart_counts(board) -> "tuple[int, int]":
    """(chart_video, chart_total) from a stamped leaderboard, deduped by id."""
    if not board:
        return 0, 0
    video, ids = set(), set()
    for p in board.get("platforms", []):
        for s in p.get("shows", []):
            iid = s.get("itunes_id")
            if iid is None:
                continue
            ids.add(str(iid))
            if s.get("apple_video"):
                video.add(str(iid))
    return len(video), len(ids)


def main() -> int:
    # Commits that touched the video state file, oldest first, with commit date.
    log = _git("log", REF, "--reverse", "--date=short", "--format=%H|%cd",
               "--", "data/apple_video_state.json").splitlines()
    if not log:
        print("backfill: no history for data/apple_video_state.json under", REF)
        return 0

    by_date = {}  # date -> entry (last commit on a date wins)
    for line in log:
        if "|" not in line:
            continue
        commit, date = line.split("|", 1)
        state = _show_json(commit, "data/apple_video_state.json")
        if not isinstance(state, dict):
            continue
        video_total = sum(1 for v in state.values() if isinstance(v, dict) and v.get("video"))
        cv, ct = chart_counts(_show_json(commit, "data/leaderboard.json"))
        by_date[date.strip()] = {
            "date": date.strip(),
            "video_total": video_total,
            "checked_total": len(state),
            "chart_video": cv,
            "chart_total": ct,
            "backfill": True,
        }

    # Merge under any existing (real, going-forward) entries -- those win.
    try:
        hist = json.loads(HISTORY_PATH.read_text())
    except Exception:
        hist = {"schema_version": 1, "entries": []}
    have = {e["date"] for e in hist.get("entries", [])}
    added = 0
    for date, entry in by_date.items():
        if date not in have:
            hist.setdefault("entries", []).append(entry)
            added += 1
    hist["entries"].sort(key=lambda e: e["date"])
    hist["schema_version"] = 1
    HISTORY_PATH.write_text(json.dumps(hist, indent=2, ensure_ascii=False))
    print(f"backfill: wrote {added} new entries ({len(hist['entries'])} total) "
          f"spanning {hist['entries'][0]['date']}..{hist['entries'][-1]['date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
