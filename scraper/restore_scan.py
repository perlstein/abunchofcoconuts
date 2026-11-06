"""Restore a complete recovery snapshot without rolling back newer scan data."""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def restore(source, destination):
    names = ('tracked_feeds.json', 'cumulative_history.json')
    recovered = {name: json.loads((source / name).read_text()) for name in names}
    current = {name: json.loads((destination / name).read_text()) for name in names}
    feeds = recovered[names[0]]
    if not feeds or not recovered[names[1]].get('entries'):
        raise ValueError('Recovery snapshot is empty')
    if recovered[names[1]]['last_entry'] < current[names[1]]['last_entry']:
        raise ValueError('Recovery snapshot is older than committed history')
    for url, old in current[names[0]].items():
        new = feeds.get(url)
        if new is None:
            raise ValueError('Recovery snapshot is missing a committed feed')
        for day_key in ('last_checked', 'metadata_observed_on'):
            if (new.get(day_key) or '') < (old.get(day_key) or ''):
                raise ValueError('Recovery would roll back a newer feed observation')
        if (new.get('host_evidence', {}).get('checked_at') or '') < (old.get('host_evidence', {}).get('checked_at') or ''):
            raise ValueError('Recovery would roll back newer hosting evidence')
    for name in names:
        shutil.copyfile(source / name, destination / name)
    print(f'Restored {len(feeds)} feeds through {recovered[names[1]]["last_entry"]}')


if __name__ == '__main__':
    restore(Path(sys.argv[1]), ROOT / 'data')
