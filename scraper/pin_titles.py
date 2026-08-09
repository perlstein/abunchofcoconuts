"""Resolve pasted show TITLES to Apple ids and pin them onto a network.

Why: ad-rep / monetization networks (DAX, AMP Sales, The Podcast Exchange,
JAMX, Adelicious, PAVE Studios, AdLarge, ...) never credit themselves in a
show's iTunes author, so networks.py cannot find their rosters by name. But
their show LISTS are visible to a human (e.g. on a Podscribe publisher page).
This script turns such a list into real tracked shows.

Input: data/pinned_titles.csv -- "network,title" rows (blank lines and
#-comments ignored, header optional). One row per show:

    network,title
    DAX,The Rest Is History
    DAX,Shagged Married Annoyed
    JAMX,Some Show Name

For each title it searches the iTunes API, picks the best match, and writes the
resolved Apple SHOW ids into that network's `extra_ids` column in
data/network_seed.csv. From there the normal pipeline takes over: networks.py
resolves each id to its real RSS feed, delivery host, artwork and episode
counts, and dedupes by id -- so this needs no separate pinned-feed list and
cannot double-count a show.

Matching is fuzzy (titles are typed/scraped by hand), so ALWAYS review first:

    python scraper/pin_titles.py            # dry run: prints every match
    python scraper/pin_titles.py --write    # apply to data/network_seed.csv

Each match prints the resolved title + iTunes author so a wrong hit is obvious.
Low-confidence matches are skipped unless --loose is passed.
"""

import csv
import difflib
import io
import os
import re
import sys
import time

import requests

from scrape import HEADERS, ROOT, TIMEOUT, log

TITLES_PATH = ROOT / "data" / "pinned_titles.csv"
SEED_PATH = ROOT / "data" / "network_seed.csv"
SEARCH_API = "https://itunes.apple.com/search"
MIN_RATIO = 0.72        # title similarity floor for a confident match
PAUSE = 0.25            # be polite to the iTunes API


def _norm(s: str) -> str:
    """Lowercase, strip punctuation and common noise words for comparison."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = re.sub(r"\b(the|a|an|podcast|show|with)\b", " ", s)
    return " ".join(s.split())


def load_titles() -> list:
    """[(network, title)] from data/pinned_titles.csv."""
    if not TITLES_PATH.exists():
        return []
    out = []
    for line in TITLES_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = next(csv.reader([line]))
        if len(parts) < 2:
            continue
        net, title = parts[0].strip(), parts[1].strip()
        if not net or not title or net.lower() == "network":
            continue
        out.append((net, title))
    return out


def find_show(title: str) -> "dict | None":
    """Best iTunes match for a show title, or None when nothing is close."""
    try:
        r = requests.get(SEARCH_API, params={
            "term": title, "entity": "podcast", "limit": 12,
        }, headers=HEADERS, timeout=TIMEOUT)
        res = r.json().get("results", [])
    except Exception as exc:
        log(f"pin_titles: search failed for {title!r}: {exc}")
        return None
    want = _norm(title)
    best, best_ratio = None, 0.0
    for x in res:
        name = x.get("collectionName") or x.get("trackName") or ""
        ratio = difflib.SequenceMatcher(None, want, _norm(name)).ratio()
        if _norm(name) == want:            # exact normalized hit wins outright
            ratio = 1.0
        if ratio > best_ratio:
            best, best_ratio = x, ratio
    if not best:
        return None
    return {
        "itunes_id": str(best.get("collectionId") or ""),
        "title": best.get("collectionName") or "",
        "artist": best.get("artistName") or "",
        "feed_url": best.get("feedUrl") or "",
        "ratio": round(best_ratio, 2),
    }


def apply_to_seed(by_network: dict) -> int:
    """Merge resolved ids into each network's extra_ids column (dedup, order
    preserved). Returns the number of seed rows changed."""
    lines = SEED_PATH.read_text().splitlines()
    out, changed = [], 0
    for line in lines:
        if not line or line.startswith("#") or line.startswith("label,"):
            out.append(line)
            continue
        parts = next(csv.reader([line]))
        parts += [""] * (5 - len(parts))
        label = parts[0].strip()
        new_ids = by_network.get(label)
        if not new_ids:
            out.append(line)
            continue
        have = [i for i in parts[2].split(";") if i.strip()]
        merged = have + [i for i in new_ids if i not in have]
        if merged != have:
            parts[2] = ";".join(merged)
            changed += 1
        buf = io.StringIO()
        csv.writer(buf).writerow(parts[:5])
        out.append(buf.getvalue().rstrip("\r\n"))
    SEED_PATH.write_text("\n".join(out) + "\n")
    return changed


def main() -> int:
    write = "--write" in sys.argv
    loose = "--loose" in sys.argv
    rows = load_titles()
    if not rows:
        print(f"pin_titles: no rows in {TITLES_PATH}.\n"
              "Add 'network,title' lines (see the module docstring) and re-run.")
        return 0

    print(f"pin_titles: resolving {len(rows)} titles "
          f"({'WRITE' if write else 'dry run'}; floor {MIN_RATIO})\n")
    by_network, skipped = {}, []
    for net, title in rows:
        m = find_show(title)
        time.sleep(PAUSE)
        if not m or not m["itunes_id"] or (m["ratio"] < MIN_RATIO and not loose):
            skipped.append((net, title, m["ratio"] if m else 0.0))
            print(f"  MISS  {net:24} {title[:38]:38} (best {m['title'][:30] if m else '-'!r})")
            continue
        by_network.setdefault(net, []).append(m["itunes_id"])
        flag = "  " if m["ratio"] >= 0.95 else "~ "
        print(f"  {flag}OK  {net:24} {title[:34]:34} -> {m['title'][:32]:32} [{m['artist'][:22]}] {m['ratio']}")

    print(f"\npin_titles: matched {sum(len(v) for v in by_network.values())}, "
          f"skipped {len(skipped)}")
    for net, ids in sorted(by_network.items()):
        print(f"  {net}: {len(ids)} ids")
    if not write:
        print("\nDry run only. Re-run with --write to apply to data/network_seed.csv.")
        return 0

    unknown = [n for n in by_network if n not in
               {next(csv.reader([l]))[0].strip() for l in SEED_PATH.read_text().splitlines()
                if l and not l.startswith("#")}]
    if unknown:
        print(f"\nWARNING: these networks are not in the seed and were ignored: {unknown}")
    changed = apply_to_seed(by_network)
    print(f"\npin_titles: updated {changed} seed row(s). "
          f"Run scraper/networks.py (or the Networks Resolve workflow) to fetch feeds.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        log(f"pin_titles: unexpected error: {exc}")
        sys.exit(1)
