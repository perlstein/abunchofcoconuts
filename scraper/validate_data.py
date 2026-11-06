"""Validate a complete research generation before it can replace published data."""
import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(path):
    return json.loads(path.read_text())


def event_ids(moves):
    return {e['event_id'] for key in ('moves', 'unverified') for e in moves.get(key, [])}


def resolve(sid, ids, redirects):
    seen = set()
    while sid not in ids:
        if sid in seen or sid not in redirects:
            raise ValueError('Missing show identity or broken redirect: ' + sid)
        seen.add(sid)
        sid = redirects[sid]
    return sid


def validate(data, baseline=None, max_age_days=None, today=None):
    manifest = read(data / 'research_manifest.json')
    files = manifest.get('files', {})
    if manifest.get('schema_version') != 1 or not {'show_index.json', 'host_moves.json'} <= files.keys():
        raise ValueError('Incomplete research manifest')
    for name, checksum in files.items():
        if name not in ('show_index.json', 'host_moves.json') and not re.fullmatch(r'shows/[0-9a-f]{2}\.json', name):
            raise ValueError('Unsafe research artifact path')
        if hashlib.sha256((data / name).read_bytes()).hexdigest() != checksum:
            raise ValueError('Research checksum mismatch: ' + name)
    index, moves = read(data / 'show_index.json'), read(data / 'host_moves.json')
    rows = index['shows']
    ids = {row['id'] for row in rows}
    if not ids or len(ids) != len(rows):
        raise ValueError('Empty or duplicate show index')
    if moves['total_tracked'] != len(ids):
        raise ValueError('Show count does not match the event ledger')
    expected = {'show_index.json', 'host_moves.json'} | {'shows/' + row['shard'] + '.json' for row in rows}
    if set(files) != expected:
        raise ValueError('Research manifest does not cover exactly the indexed shards')
    registry_path = data / 'show_registry.json'
    if registry_path.exists():
        header = read(registry_path)
        if header.get('storage_version') == 2:
            if (header['show_count'] != len(ids)
                    or header['metadata']['generated'] != index['generated']
                    or header['shards'] != {name: checksum for name, checksum in files.items() if name.startswith('shows/')}):
                raise ValueError('Durable registry header does not match its published archive')
        elif set(header.get('shows', {})) != ids:
            raise ValueError('Legacy registry does not match its published archive')
    archived, archived_events = set(), set()
    for shard in sorted({row['shard'] for row in rows}):
        records = read(data / 'shows' / (shard + '.json'))
        if set(records) != {row['id'] for row in rows if row['shard'] == shard}:
            raise ValueError('Index/archive mismatch in shard ' + shard)
        for sid, record in records.items():
            if record['id'] != sid or sid in archived:
                raise ValueError('Duplicate or inconsistent archived identity')
            archived.add(sid)
            for event in record.get('events', []):
                eid = event['event_id']
                if eid in archived_events:
                    raise ValueError('Duplicate migration event: ' + eid)
                archived_events.add(eid)
    if event_ids(moves) != archived_events:
        raise ValueError('Event ledger does not match complete show histories')
    redirects = index.get('redirects', {})
    for sid in redirects:
        resolve(sid, ids, redirects)
    if baseline:
        old_index, old_moves = baseline
        for sid in {row['id'] for row in old_index['shows']} | set(old_index.get('redirects', {})):
            resolve(sid, ids, redirects)
        if not event_ids(old_moves) <= archived_events:
            raise ValueError('Previously recorded migration events were lost')
        for source, days in old_index.get('chart_scan_dates', {}).items():
            if not set(days) <= set(index.get('chart_scan_dates', {}).get(source, [])):
                raise ValueError('Previously recorded chart dates were lost')
    dates = {'research': index['generated'], 'cumulative': read(data / 'cumulative_history.json')['last_entry']}
    counts = {'archive': len(ids), 'events': len(archived_events)}
    for source, filename in [('apple', 'leaderboard.json'), ('spotify', 'spotify_leaderboard.json')]:
        board = read(data / filename)
        platforms = board['platforms']
        count = sum(len(p['shows']) for p in platforms)
        if not count or count != board['total_unique_shows'] or any(p['count'] != len(p['shows']) for p in platforms):
            raise ValueError('Empty or inconsistent ' + source + ' chart counts')
        counts[source], dates[source] = count, board['last_scanned']
    if manifest['generated'] != dates['research'] or moves['generated'] != dates['research']:
        raise ValueError('Research artifacts come from different generations')
    if max_age_days is not None:
        today = today or datetime.now(timezone.utc).date()
        for name, value in dates.items():
            age = (today - date.fromisoformat(value[:10])).days
            if age < 0 or age > max_age_days:
                raise ValueError(f'{name} data is stale or future-dated: {value}')
    return {'dates': dates, 'counts': counts}


def baseline_at(ref, root=ROOT):
    return tuple(json.loads(subprocess.check_output(
        ['git', 'show', ref + ':data/' + name], cwd=root))
        for name in ('show_index.json', 'host_moves.json'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref')
    parser.add_argument('--max-age-days', type=int)
    args = parser.parse_args()
    result = validate(ROOT / 'data', baseline_at(args.baseline_ref) if args.baseline_ref else None,
                      args.max_age_days)
    print('Validated complete generation:', json.dumps(result, sort_keys=True))
