import json
import tempfile
import unittest
from pathlib import Path

from research import build_registry, load_registry, publish, save
from test_research import record


class RegistryStorageTests(unittest.TestCase):
    def registry(self):
        state = {}
        for day, host in [('01', 'PRX'), ('02', 'PRX'), ('03', 'Libsyn'), ('04', 'Libsyn')]:
            date = '2026-09-' + day
            state = build_registry([record(day=date, host=host,
                owner_email='public@example.com', chart_history={'apple': [
                    {'date': date, 'ranks': {'Arts': 5}}]})], state, [], date)
        state['chart_scan_dates'] = {'apple': ['2026-09-01']}
        return state

    def test_legacy_migration_preserves_every_field_and_future_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.registry()
            save(root / 'show_registry.json', registry)
            self.assertEqual(load_registry(root), registry)
            publish(root, load_registry(root))
            self.assertNotIn('shows', json.loads((root / 'show_registry.json').read_text()))
            restored = load_registry(root)
            self.assertEqual(restored, registry)
            continued = build_registry([], restored, [], '2026-09-05')
            sid = next(iter(registry['shows']))
            for field in ('events', 'placements', 'chart_history', 'owner_email'):
                self.assertEqual(continued['shows'][sid][field], registry['shows'][sid][field])
            publish(root, continued)
            self.assertEqual(load_registry(root), continued)

    def test_missing_or_changed_shard_fails_closed(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                publish(root, self.registry())
                shard = next((root / 'shows').glob('*.json'))
                if missing:
                    shard.unlink()
                else:
                    shard.write_text('{}')
                with self.assertRaises((ValueError, FileNotFoundError)):
                    load_registry(root)

    def test_incomplete_manifest_and_unsafe_paths_fail_closed(self):
        for change in ({'show_count': 99}, {'shards': {'../outside.json': 'hash'}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                publish(root, self.registry())
                path = root / 'show_registry.json'
                header = json.loads(path.read_text())
                header.update(change)
                save(path, header)
                with self.assertRaises(ValueError):
                    load_registry(root)


if __name__ == '__main__':
    unittest.main()
