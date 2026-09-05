"""Durable public show identity, evidence, and append-only migration history.

No title-only identity matching. Merge by directory ID, normalized RSS URL,
verified HTTP redirect, or podcast namespace GUID. Legacy moves remain explicitly
unverified. Only successful RSS observations on two distinct UTC dates can
confirm a new placement/migration; replaying the same scan never confirms it.
"""
import copy
import hashlib
import json
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
UNKNOWN = {None, '', 'Unknown', 'Other/Self-Hosted'}
FIELDS = ('title', 'artwork', 'owner_name', 'owner_email', 'itunes_author',
          'publisher', 'website', 'description', 'cadence', 'episode_count',
          'last_published', 'schedule', 'rss_video', 'apple_video')


def normalize_feed(url):
    """Normalize only transport/default-port/terminal slash, retaining query/path case."""
    if not url:
        return ''
    try:
        u = urlsplit(url.strip())
        if u.scheme not in ('http', 'https') or not u.hostname:
            return ''
        port = u.port
        host = u.hostname.lower()
        if port and not ((u.scheme == 'http' and port == 80) or (u.scheme == 'https' and port == 443)):
            host += ':' + str(port)
        return urlunsplit(('https', host, u.path.rstrip('/'), u.query, ''))
    except ValueError:
        return ''


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else copy.deepcopy(default)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
    tmp.replace(path)


def tokens(record):
    ts = set()
    for url in record.get('feeds', []) + [record.get('feed_url'), record.get('resolved_feed_url')]:
        n = normalize_feed(url)
        if n:
            ts.add('feed:' + n)
    for src, ids in record.get('ids', {}).items():
        ts.update((str(i) if src == 'spotify' and str(i).startswith('spotify:show:') else src + ':' + str(i)) for i in ids if i)
    if record.get('itunes_id'):
        ts.add('apple:' + str(record['itunes_id']))
    if str(record.get('id', '')).startswith('spotify:show:'):
        ts.add(record['id'])
    if record.get('podcast_guid'):
        ts.add('guid:' + record['podcast_guid'])
    return ts


def collect(data_dir):
    """Keep metadata dates honest: cached Spotify contacts have no inferred scan date."""
    records = load(data_dir / 'identity_links.json', [])
    tracked = load(data_dir / 'tracked_feeds.json', {})
    for url, st in tracked.items():
        r = dict(st, feed_url=url)
        r['observed_on'] = st.get('metadata_observed_on')
        r['title'] = st.get('title') or st.get('label') or url
        records.append(r)
    for filename, src in [('leaderboard.json', 'apple'), ('spotify_leaderboard.json', 'spotify')]:
        board = load(data_dir / filename, {})
        for p in board.get('platforms', []):
            for s in p.get('shows', []):
                r = dict(s, host=p['name'], chart_source=src, chart_date=board.get('last_scanned'))
                # Apple fetches metadata every scan. Spotify can reuse its cache.
                r['observed_on'] = (s.get('metadata_observed_on') or
                                    (board.get('last_scanned') if src == 'apple' else None))
                records.append(r)
    for ident, s in load(data_dir / 'spotify_show_map.json', {}).items():
        records.append(dict(s, id=ident))
    nets = load(data_dir / 'networks.json', {})
    for n in nets.get('networks', []):
        for s in n.get('shows', []):
            records.append(dict(s, network=n['label'], network_observed_on=nets.get('generated')))
    for src, filename in [('apple', 'show_history.json'), ('spotify', 'spotify_show_history.json')]:
        history = load(data_dir / filename, {})
        directory = history.get('shows', history.get('directory', {}))
        trajectories = defaultdict(list)
        for entry in history.get('entries', []):
            day = {}
            for category, ids in entry.get('charts', {}).items():
                for rank, ident in enumerate(ids, 1):
                    day.setdefault(str(ident), {})[category] = rank
            for ident, ranks in day.items():
                trajectories[ident].append({'date': entry['date'], 'ranks': ranks, 'synthetic': bool(entry.get('synthetic'))})
        for ident in set(directory) | set(trajectories):
            r = dict(directory.get(ident, {}), ids={src: [ident]}, chart_history={src: trajectories.get(ident, [])})
            records.append(r)
    return records


def build_registry(records, previous, legacy_moves, today):
    old = previous.get('shows', {})
    all_records = [dict(r, prior_id=i) for i, r in old.items()] + records
    parent = list(range(len(all_records)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(a, b):
        parent[root(b)] = root(a)
    owners = {}
    for i, r in enumerate(all_records):
        for t in tokens(r):
            if t in owners:
                union(i, owners[t])
            else:
                owners[t] = i
    groups = defaultdict(list)
    for i, r in enumerate(all_records):
        groups[root(i)].append(r)
    shows, redirects = {}, dict(previous.get('redirects', {}))
    for rows in groups.values():
        ts = sorted(set().union(*(tokens(r) for r in rows)))
        if not ts:
            continue
        prior_ids = sorted(r['prior_id'] for r in rows if r.get('prior_id'))
        sid = prior_ids[0] if prior_ids else 'show-' + digest(ts[0])
        for i in prior_ids[1:]:
            redirects[i] = sid
        rec = copy.deepcopy(old.get(sid, {}))
        # Merge archives when formerly separate identities become one.
        events = {e['event_id']: e for r in rows for e in r.get('events', [])}
        rec.update(id=sid, feeds=sorted({u for r in rows for u in r.get('feeds', []) +
                   [r.get('feed_url'), r.get('resolved_feed_url')] if normalize_feed(u)}),
                   ids={'apple': [], 'spotify': []}, events=list(events.values()))
        for t in ts:
            if t.startswith('apple:'):
                rec['ids']['apple'].append(t[6:])
            elif t.startswith('spotify:show:'):
                rec['ids']['spotify'].append(t)
            elif t.startswith('guid:'):
                rec['podcast_guid'] = t[5:]
        # Never overwrite rich archived fields with missing data when a show drops out.
        rec.setdefault('field_dates', {})
        for r in sorted(rows, key=lambda x: x.get('observed_on') or ''):
            stamp = r.get('observed_on')
            for f in FIELDS:
                v = r.get(f)
                if v is not None and v != '' and (f not in rec or
                        (stamp or '') >= (rec['field_dates'].get(f) or '')):
                    rec[f] = v
                    rec['field_dates'][f] = stamp
        rec['title'] = rec.get('title') or (rec['feeds'][0] if rec['feeds'] else sid)
        dates = [r.get('first_seen') for r in rows if r.get('first_seen')]
        rec['first_seen'] = min(dates) if dates else today
        observations = {}
        for r in rows:
            for o in r.get('observations', []):
                key = normalize_feed(o['feed_url'])
                if o.get('checked_at', '') >= observations.get(key, {}).get('checked_at', ''):
                    observations[key] = o
            evidence = r.get('host_evidence')
            if evidence and evidence.get('checked_at'):
                o = dict(evidence, feed_url=r.get('feed_url') or evidence.get('feed_url'))
                key = normalize_feed(o['feed_url'])
                if key and o['checked_at'] >= observations.get(key, {}).get('checked_at', ''):
                    observations[key] = o
        rec['observations'] = sorted(observations.values(), key=lambda o: o['feed_url'])
        networks = {n['name']: n for r in rows for n in r.get('networks', [])}
        for r in rows:
            if r.get('network'):
                networks[r['network']] = {'name': r['network'], 'observed_on': r.get('network_observed_on'), 'basis': 'curated roster'}
        rec['networks'] = sorted(networks.values(), key=lambda n: n['name'])
        chart_dates = dict(rec.get('last_charted', {}))
        for r in rows:
            if r.get('chart_date'):
                src = r['chart_source']
                chart_dates[src] = max(chart_dates.get(src, ''), r['chart_date'])
        archives = defaultdict(dict)
        for r in rows:
            for src, entries in r.get('chart_history', {}).items():
                for entry in entries:
                    current = archives[src].get(entry['date'])
                    if not current or current.get('synthetic') or not entry.get('synthetic'):
                        archives[src][entry['date']] = entry
        rec['chart_history'] = {src: sorted(days.values(), key=lambda e:e['date']) for src, days in archives.items()}
        for src, days in archives.items():
            real = [d for d, e in days.items() if not e.get('synthetic')]
            if real: chart_dates[src] = max(chart_dates.get(src, ''), max(real))
        rec['last_charted'] = chart_dates
        rec['last_checked'] = max((r.get('last_checked') or '' for r in rows), default='') or None
        # Provisional legacy labels are never sufficient evidence to confirm a move.
        if not rec.get('host'):
            hosts = {r.get('host') for r in rows if r.get('host') not in UNKNOWN}
            rec['host'] = next(iter(hosts)) if len(hosts) == 1 else None
            rec['hosting_status'] = 'unverified' if len(hosts) <= 1 else 'conflict'
        advance_host(rec, today)
        shows[sid] = rec
    index = {t: sid for sid, r in shows.items() for t in tokens(r)}
    # Import legacy evidence once, retain every row, never promote it to confirmed.
    for m in legacy_moves:
        sid = index.get('feed:' + normalize_feed(m.get('feed_url')))
        if not sid:
            continue
        r = shows[sid]
        eid = 'legacy-' + digest('|'.join(str(m.get(k, '')) for k in ('feed_url', 'from', 'to', 'date')))
        if not any(e['event_id'] == eid for e in r['events']):
            r['events'].append({k: m.get(k) for k in ('feed_url', 'from', 'to', 'date')})
            r['events'][-1].update(event_id=eid, status='legacy_unverified',
                reason='Imported detection; original supporting observations were not retained.')
    for r in shows.values():
        legacy = [e for e in r['events'] if e['status'] in ('legacy_unverified', 'conflict')]
        for e in legacy:
            if any(x['date'] == e['date'] and x['from'] == e['to'] and x['to'] == e['from'] for x in legacy):
                e.update(status='conflict', reason='Opposite directions were recorded for this show on the same date.')
        r['events'].sort(key=lambda e: (e.get('date') or '', e['event_id']), reverse=True)
    # Same-title records are review candidates, never automatic merges.
    titles = defaultdict(list)
    for sid, r in shows.items():
        titles[' '.join(r['title'].casefold().split())].append(sid)
    for r in shows.values():
        r['possible_duplicates'] = [i for i in titles[' '.join(r['title'].casefold().split())] if i != r['id']]
    for alias in redirects:
        seen = set()
        while redirects[alias] in redirects and redirects[alias] not in seen:
            seen.add(redirects[alias]); redirects[alias] = redirects[redirects[alias]]
    return {'schema_version': 1, 'generated': today, 'shows': shows, 'redirects': redirects}


def advance_host(r, today):
    cutoff = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    recent = [o for o in r.get('observations', []) if cutoff <= o.get('checked_at', '')[:10] <= today]
    usable = [o for o in recent if o.get('success') and o.get('host') not in UNKNOWN]
    conflicting = any(o.get('conflict') for o in recent)
    hosts = {o['host'] for o in usable}
    if conflicting or len(hosts) > 1:
        r['hosting_status'] = 'conflict'
        r.pop('candidate', None)
        return
    if not hosts:
        r['hosting_status'] = 'stale' if r.get('confirmed_on') else r.get('hosting_status', 'unverified')
        return
    candidate = next(iter(hosts))
    observed_date = max(o['checked_at'][:10] for o in usable)
    if r.get('confirmed_on') and r.get('host') == candidate:
        r['hosting_status'] = 'confirmed'
        r['host_last_verified'] = observed_date
        r.pop('candidate', None)
        return
    c = r.get('candidate', {})
    if c.get('host') != candidate:
        c = {'host': candidate, 'dates': [], 'first_seen': observed_date}
    c['dates'] = sorted(set(c['dates'] + [observed_date]))
    c.setdefault('evidence_by_date', {})[observed_date] = copy.deepcopy(usable)
    r['candidate'] = c
    r['hosting_status'] = 'pending'
    if len(c['dates']) < 2:
        return
    previous_host = r.get('host') if r.get('confirmed_on') else None
    if previous_host and previous_host != candidate:
        eid = 'move-' + digest('|'.join([r['id'], previous_host, candidate, c['first_seen'], observed_date]))
        if not any(e['event_id'] == eid for e in r['events']):
            r['events'].append({'event_id': eid, 'from': previous_host, 'to': candidate,
                'date': observed_date, 'first_observed': c['first_seen'], 'status': 'confirmed',
                'feed_url': usable[0]['feed_url'], 'evidence': [o for day in c['dates'] for o in c['evidence_by_date'].get(day, [])],
                'confirmation_dates': c['dates']})
    r.update(host=candidate, hosting_status='confirmed', confirmed_on=observed_date,
             host_last_verified=observed_date)
    r.pop('candidate', None)


def publish(data_dir, registry):
    shows = registry['shows']
    rows, shards, all_events = [], defaultdict(dict), []
    for sid, r in sorted(shows.items()):
        shard = digest(sid)[:2]
        shards[shard][sid] = r
        rows.append({k: r.get(k) for k in ('id', 'title', 'artwork', 'host', 'hosting_status', 'ids', 'feeds', 'itunes_author', 'publisher', 'networks')})
        rows[-1]['shard'] = shard
        confirmed = [e for e in r['events'] if e['status'] == 'confirmed']
        if confirmed:
            rows[-1]['last_move'] = {k:confirmed[0].get(k) for k in ('from','to','date','status')}
        for e in r['events']:
            all_events.append(dict(e, show_id=sid, title=r['title'], artwork=r.get('artwork'), source='research', feed_aliases=r['feeds']))
    all_events.sort(key=lambda e: (e.get('date') or '', e['event_id']), reverse=True)
    save(data_dir / 'show_index.json', {'generated': registry['generated'], 'shows': rows, 'redirects': registry['redirects'], 'chart_scan_dates':registry.get('chart_scan_dates', {})})
    for name, content in shards.items():
        save(data_dir / 'shows' / (name + '.json'), content)
    save(data_dir / 'host_moves.json', {'schema_version': 2, 'generated': registry['generated'],
         'total_tracked': len(shows), 'moves': [e for e in all_events if e['status'] == 'confirmed'],
         'unverified': [e for e in all_events if e['status'] != 'confirmed'],
         'pending': [{'show_id': sid, 'title': r['title'], 'status': r['hosting_status'],
                      'host': r.get('host'), 'candidate': r.get('candidate')} for sid, r in shows.items()
                     if r['hosting_status'] in ('pending', 'conflict')]})
    # Written last; consumers must download the entire generation before publishing.
    files = ['show_index.json', 'host_moves.json'] + ['shows/' + s + '.json' for s in sorted(shards)]
    save(data_dir / 'research_manifest.json', {'schema_version': 1, 'generated': registry['generated'],
         'files': {f: hashlib.sha256((data_dir / f).read_bytes()).hexdigest() for f in files}})


def main():
    data_dir = ROOT / 'data'
    previous = load(data_dir / 'show_registry.json', {})
    moves = load(data_dir / 'host_moves.json', {})
    legacy = moves.get('moves', []) if moves.get('schema_version', 1) < 2 else []
    registry = build_registry(collect(data_dir), previous, legacy, datetime.now(timezone.utc).date().isoformat())
    scans = previous.get('chart_scan_dates', {})
    for src, filename in [('apple', 'show_history.json'), ('spotify', 'spotify_show_history.json')]:
        scans[src] = sorted(set(scans.get(src, [])) | {e['date'] for e in load(data_dir / filename, {}).get('entries', [])})
    registry['chart_scan_dates'] = scans
    save(data_dir / 'show_registry.json', registry)
    publish(data_dir, registry)
    print(f"Research registry: {len(registry['shows'])} durable show records")


if __name__ == '__main__':
    main()
