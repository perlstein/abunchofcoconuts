"""One-time directory-ID/RSS link recovery from public Apple lookup responses.
Never match names. Preserves every observed URL for an ID across reruns.
"""
import time
from datetime import datetime, timezone
import requests
from research import ROOT, load, save


def main():
    data = ROOT / 'data'
    history = load(data / 'show_history.json', {})
    ids = set(history.get('shows', {}))
    for e in history.get('entries', []):
        ids.update(i for values in e.get('charts', {}).values() for i in values)
    existing = load(data / 'identity_links.json', [])
    seen = {(r['itunes_id'], r['feed_url']) for r in existing}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 100):
        batch = ordered[start:start+100]
        response = requests.get('https://itunes.apple.com/lookup', params={'id':','.join(batch), 'entity':'podcast'}, timeout=45)
        response.raise_for_status()
        for item in response.json().get('results', []):
            ident, feed = str(item.get('trackId', '')), item.get('feedUrl')
            if ident not in batch or not feed or (ident, feed) in seen:
                continue
            seen.add((ident, feed))
            existing.append({'itunes_id':ident, 'feed_url':feed,
                'title':item.get('trackName'), 'identity_observed_on':datetime.now(timezone.utc).date().isoformat(),
                'identity_source':'Apple lookup API'})
        save(data / 'identity_links.json', existing)
        print(f'Identity links: {min(start+100,len(ordered))}/{len(ordered)} IDs checked',flush=True)
        time.sleep(.35)


if __name__=='__main__':main()
