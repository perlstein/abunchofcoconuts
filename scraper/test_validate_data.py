import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from validate_data import validate


class DataValidationTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'shows').mkdir()
        day = '2026-11-06'
        values = {
            'show_index.json': {'generated': day, 'shows': [{'id': 'show-a', 'shard': '00'}],
                                'redirects': {}, 'chart_scan_dates': {'apple': [day]}},
            'host_moves.json': {'generated': day, 'total_tracked': 1, 'moves': [],
                                'unverified': [{'event_id': 'move-a'}]},
            'shows/00.json': {'show-a': {'id': 'show-a', 'events': [{'event_id': 'move-a'}]}},
            'cumulative_history.json': {'last_entry': day},
        }
        for name in ('leaderboard.json', 'spotify_leaderboard.json'):
            values[name] = {'last_scanned': day, 'total_unique_shows': 1,
                            'platforms': [{'count': 1, 'shows': [{'id': '1'}]}]}
        self.save(root, values)
        return values

    def save(self, root, values):
        for name, value in values.items():
            (root / name).write_text(json.dumps(value))
        files = ('show_index.json', 'host_moves.json', 'shows/00.json')
        (root / 'research_manifest.json').write_text(json.dumps({
            'schema_version': 1, 'generated': values['show_index.json']['generated'],
            'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}))

    def test_valid_generation_and_identity_merge_preserve_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); values = self.fixture(root)
            baseline = copy.deepcopy((values['show_index.json'], values['host_moves.json']))
            values['show_index.json']['shows'][0]['id'] = 'show-b'
            values['show_index.json']['redirects'] = {'show-a': 'show-b'}
            values['shows/00.json'] = {'show-b': {'id': 'show-b', 'events': [{'event_id': 'move-a'}]}}
            self.save(root, values)
            self.assertEqual(validate(root, baseline, 1, date(2026, 11, 6))['counts']['archive'], 1)

    def test_lost_show_event_or_chart_date_stops_publication(self):
        for problem in ('identity', 'event', 'chart_date'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); values = self.fixture(root)
                old_index, old_moves = copy.deepcopy((values['show_index.json'], values['host_moves.json']))
                if problem == 'identity':
                    old_index['shows'].append({'id': 'lost-show'})
                elif problem == 'event':
                    old_moves['unverified'].append({'event_id': 'lost-event'})
                else:
                    old_index['chart_scan_dates']['apple'].append('2026-11-05')
                with self.assertRaises(ValueError):
                    validate(root, (old_index, old_moves))

    def test_stale_data_and_invalid_counts_stop_publication(self):
        for problem in ('stale', 'count', 'empty', 'shard', 'checksum'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); values = self.fixture(root)
                if problem == 'count':
                    values['leaderboard.json']['total_unique_shows'] = 9
                if problem == 'empty':
                    values['show_index.json']['shows'] = []
                if problem == 'shard':
                    values['shows/00.json'] = {}
                self.save(root, values)
                if problem == 'checksum':
                    (root / 'shows/00.json').write_text('{}')
                with self.assertRaises(ValueError):
                    validate(root, max_age_days=1, today=date(2026, 11, 8) if problem == 'stale' else date(2026, 11, 6))


if __name__ == '__main__':
    unittest.main()
