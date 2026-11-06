import json
import tempfile
import unittest
from pathlib import Path

from restore_scan import restore


class RestoreScanTests(unittest.TestCase):
    def test_recovery_and_stale_snapshot_rejection(self):
        for mode in ('fresh', 'missing', 'old_day', 'old_evidence'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, target = root / 'saved', root / 'data'
                source.mkdir()
                target.mkdir()
                old = {'url': {'last_checked': '2026-11-05', 'host_evidence': {
                    'checked_at': '2026-11-05T12:00:00+00:00'}}}
                new = {'url': {'last_checked': '2026-11-06', 'host_evidence': {
                    'checked_at': '2026-11-06T12:00:00+00:00'}}}
                if mode == 'missing':
                    new = {'different': new['url']}
                elif mode == 'old_day':
                    new['url']['last_checked'] = '2026-11-04'
                elif mode == 'old_evidence':
                    new['url']['host_evidence']['checked_at'] = '2026-11-05T11:00:00+00:00'
                for folder, feeds, day in [(source, new, '2026-11-06'), (target, old, '2026-11-05')]:
                    (folder / 'tracked_feeds.json').write_text(json.dumps(feeds))
                    (folder / 'cumulative_history.json').write_text(json.dumps({
                        'last_entry': day, 'entries': [{'date': day}]}))
                if mode == 'fresh':
                    restore(source, target)
                    self.assertEqual(json.loads((target / 'tracked_feeds.json').read_text()), new)
                else:
                    with self.assertRaises(ValueError):
                        restore(source, target)
                    self.assertEqual(json.loads((target / 'tracked_feeds.json').read_text()), old)


if __name__ == '__main__':
    unittest.main()
