"""Unit tests for history compaction. Run: python -m unittest test_history"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import scrape
from scrape import (SYNTHETIC_BUDGET, build_show_history_entry, compact_history,
                    prune_synthetic)


def entry(d, synthetic=False):
    e = {"date": d.isoformat(), "total_shows": 1000, "platforms": []}
    if synthetic:
        e["synthetic"] = True
    return e


class TestCompactHistory(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(compact_history([]), [])

    def test_recent_daily_entries_all_kept(self):
        newest = date(2026, 6, 12)
        entries = [entry(newest - timedelta(days=i)) for i in range(90)]
        result = compact_history(entries)
        self.assertEqual(len(result), 90)

    def test_weekly_compaction_between_90_days_and_1_year(self):
        newest = date(2026, 6, 12)
        # 30 consecutive daily entries centered ~200 days back, plus the
        # newest entry to anchor the cutoffs
        old = [entry(newest - timedelta(days=200 + i)) for i in range(30)]
        result = compact_history(old + [entry(newest)])
        old_kept = [e for e in result if e["date"] != newest.isoformat()]
        # 30 consecutive days span 5-6 ISO weeks
        self.assertLessEqual(len(old_kept), 6)
        self.assertGreaterEqual(len(old_kept), 4)
        # The kept entry per week is the latest of that week
        self.assertIn(entry(newest - timedelta(days=200))["date"],
                      [e["date"] for e in result])

    def test_monthly_compaction_beyond_1_year(self):
        newest = date(2026, 6, 12)
        # Daily entries across ~3 months, two years back
        old = [entry(newest - timedelta(days=730 + i)) for i in range(90)]
        result = compact_history(old + [entry(newest)])
        old_kept = [e for e in result if e["date"] != newest.isoformat()]
        self.assertLessEqual(len(old_kept), 4)
        self.assertGreaterEqual(len(old_kept), 3)

    def test_keeps_last_entry_of_bucket(self):
        newest = date(2026, 6, 12)
        # Mon/Wed/Fri of one ISO week, ~150 days back (weekly zone)
        monday = newest - timedelta(days=150)
        monday -= timedelta(days=monday.weekday())
        week = [entry(monday), entry(monday + timedelta(days=2)),
                entry(monday + timedelta(days=4))]
        result = compact_history(week + [entry(newest)])
        kept_dates = [e["date"] for e in result]
        self.assertIn((monday + timedelta(days=4)).isoformat(), kept_dates)
        self.assertNotIn(monday.isoformat(), kept_dates)

    def test_output_sorted_and_idempotent(self):
        newest = date(2026, 6, 12)
        entries = ([entry(newest - timedelta(days=i)) for i in range(0, 120, 3)]
                   + [entry(newest - timedelta(days=400 + i)) for i in range(0, 60, 5)])
        once = compact_history(entries)
        self.assertEqual([e["date"] for e in once], sorted(e["date"] for e in once))
        self.assertEqual(compact_history(once), once)


class TestPruneSynthetic(unittest.TestCase):
    def _seeded(self, real_count):
        today = date(2026, 6, 12)
        synth = [entry(today - timedelta(weeks=w), synthetic=True)
                 for w in range(1, SYNTHETIC_BUDGET + 1)]
        real = [entry(today + timedelta(days=i)) for i in range(real_count)]
        return synth + real

    def test_one_real_entry_retires_one_synthetic(self):
        result = prune_synthetic(self._seeded(real_count=1))
        synth = [e for e in result if e.get("synthetic")]
        self.assertEqual(len(synth), SYNTHETIC_BUDGET - 1)

    def test_oldest_synthetic_dropped_first(self):
        entries = self._seeded(real_count=3)
        oldest = min(e["date"] for e in entries if e.get("synthetic"))
        result = prune_synthetic(entries)
        self.assertNotIn(oldest, [e["date"] for e in result])

    def test_all_synthetic_gone_once_budget_reached(self):
        result = prune_synthetic(self._seeded(real_count=SYNTHETIC_BUDGET))
        self.assertFalse(any(e.get("synthetic") for e in result))

    def test_real_entries_never_pruned(self):
        result = prune_synthetic(self._seeded(real_count=40))
        self.assertEqual(len([e for e in result if not e.get("synthetic")]), 40)

    def test_no_synthetic_is_noop(self):
        today = date(2026, 6, 12)
        entries = [entry(today - timedelta(days=i)) for i in range(5)]
        self.assertEqual(len(prune_synthetic(entries)), 5)


class TestShowHistory(unittest.TestCase):
    def _shows(self):
        # 3 shows; A ranks high in two categories, B and C single-category
        return {
            "A": {"itunes_id": "A", "title": "Show A", "artwork": "artA",
                  "ranks": {"Comedy": 2, "News": 5}},
            "B": {"itunes_id": "B", "title": "Show B", "artwork": "artB",
                  "ranks": {"Comedy": 1}},
            "C": {"itunes_id": "C", "title": "Show C", "artwork": "artC",
                  "ranks": {"News": 3}},
        }

    def test_entry_orders_by_rank_index_is_rank(self):
        e = build_show_history_entry(self._shows(), "2026-06-17")
        # Comedy: B(rank1) before A(rank2)
        self.assertEqual(e["charts"]["Comedy"], ["B", "A"])
        # rank = index+1
        self.assertEqual(e["charts"]["Comedy"].index("A") + 1, 2)
        # News: C(rank3) before A(rank5)
        self.assertEqual(e["charts"]["News"], ["C", "A"])
        # categories a show isn't in stay empty
        self.assertEqual(e["charts"]["Sports"], [])

    def test_append_prunes_directory_and_is_compact(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = scrape.SHOW_HISTORY_PATH
            scrape.SHOW_HISTORY_PATH = Path(tmp) / "show_history.json"
            try:
                shows = self._shows()
                e = build_show_history_entry(shows, "2026-06-17")
                scrape.append_show_history(e, shows)
                data = json.loads(scrape.SHOW_HISTORY_PATH.read_text())
                # directory holds exactly the referenced shows, with metadata
                self.assertEqual(set(data["shows"]), {"A", "B", "C"})
                self.assertEqual(data["shows"]["A"]["title"], "Show A")
                # written compact (no indentation newlines)
                self.assertNotIn("\n", scrape.SHOW_HISTORY_PATH.read_text())
                # a show that drops out of all retained entries is pruned
                shows2 = {"B": shows["B"]}  # only B charts next time
                # force the old entry to age out by using a far-future date
                e2 = build_show_history_entry(shows2, "2027-06-17")
                scrape.append_show_history(e2, shows2)
                data2 = json.loads(scrape.SHOW_HISTORY_PATH.read_text())
                self.assertIn("B", data2["shows"])
            finally:
                scrape.SHOW_HISTORY_PATH = orig

    def test_directory_last_seen_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = scrape.SHOW_HISTORY_PATH
            scrape.SHOW_HISTORY_PATH = Path(tmp) / "show_history.json"
            try:
                shows = self._shows()
                scrape.append_show_history(build_show_history_entry(shows, "2026-06-17"), shows)
                # B's artwork rotates next scan
                shows["B"]["artwork"] = "artB-new"
                scrape.append_show_history(build_show_history_entry(shows, "2026-06-18"), shows)
                data = json.loads(scrape.SHOW_HISTORY_PATH.read_text())
                self.assertEqual(data["shows"]["B"]["artwork"], "artB-new")
            finally:
                scrape.SHOW_HISTORY_PATH = orig


if __name__ == "__main__":
    unittest.main()
