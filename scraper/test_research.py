import unittest
from research import build_registry, normalize_feed


def record(url='https://old.example/feed', host='PRX', day='2026-09-01', apple='1', **extra):
    r = {'feed_url':url, 'title':'A show', 'itunes_id':apple, 'host':host,
         'host_evidence':{'feed_url':url, 'host':host, 'success':True,
                          'checked_at':day+'T10:00:00+00:00'}}
    r.update(extra)
    return r


def build(rows, prior=None, legacy=None, day='2026-09-01'):
    return build_registry(rows, prior or {}, legacy or [], day)


class ResearchTests(unittest.TestCase):
    def test_alias_redirect_and_historical_conflict(self):
        old='https://feeds.megaphone.fm/20k'
        new='https://feed.20k.org/'
        r=build([record(old, resolved_feed_url=new), record(new)], legacy=[
            {'feed_url':old,'from':'PRX','to':'Megaphone','date':'2026-07-23'},
            {'feed_url':new,'from':'Megaphone','to':'PRX','date':'2026-07-23'}])
        self.assertEqual(len(r['shows']),1)
        self.assertEqual([e['status'] for e in next(iter(r['shows'].values()))['events']], ['conflict','conflict'])

    def test_same_title_never_merges(self):
        r=build([record(apple='1'), record('https://different.example/feed',apple='2')])
        self.assertEqual(len(r['shows']),2)
        self.assertTrue(all(x['possible_duplicates'] for x in r['shows'].values()))

    def test_replay_cannot_confirm_and_events_never_overwrite(self):
        r=build([record()]); sid=next(iter(r['shows']))
        r=build([record()],r)
        self.assertEqual(r['shows'][sid]['hosting_status'],'pending')
        r=build([record(day='2026-09-02')],r,day='2026-09-02')
        self.assertEqual(r['shows'][sid]['hosting_status'],'confirmed')
        self.assertFalse(r['shows'][sid]['events'])
        for day,host in [('2026-09-03','Megaphone'),('2026-09-04','Megaphone'),('2026-09-05','Libsyn'),('2026-09-06','Libsyn')]:
            r=build([record(host=host,day=day)],r,day=day)
        self.assertEqual([(e['from'],e['to']) for e in r['shows'][sid]['events']], [('Megaphone','Libsyn'),('PRX','Megaphone')])
        self.assertTrue(all(len(e['confirmation_dates'])==2 for e in r['shows'][sid]['events']))

    def test_conflicting_aliases_never_confirm(self):
        r=build([record(),record('https://second.example/feed',host='Libsyn')])
        for day in ['2026-09-02','2026-09-03']:
            r=build([record(day=day),record('https://second.example/feed',host='Libsyn',day=day)],r,day=day)
        s=next(iter(r['shows'].values()))
        self.assertEqual(s['hosting_status'],'conflict');self.assertFalse(s['events'])

    def test_failed_or_stale_observations_do_not_confirm(self):
        row=record();row['host_evidence']['success']=False
        r=build([row]);self.assertNotEqual(next(iter(r['shows'].values()))['hosting_status'],'confirmed')
        r=build([record()],day='2026-09-15');self.assertNotEqual(next(iter(r['shows'].values()))['hosting_status'],'confirmed')

    def test_off_chart_metadata_history_and_id_survive(self):
        r=build([record(owner_email='public@example.com',chart_history={'apple':[{'date':'2026-08-01','ranks':{'Arts':5}}]})]);sid=next(iter(r['shows']))
        r=build([],r,day='2026-09-03');s=r['shows'][sid]
        self.assertEqual(s['owner_email'],'public@example.com');self.assertEqual(s['chart_history']['apple'][0]['ranks']['Arts'],5)

    def test_id_redirect_survives_later_identity_merge(self):
        r=build([record(apple='1'),record('https://new.example/feed',apple='2')]);ids=set(r['shows'])
        r=build([record(resolved_feed_url='https://new.example/feed')],r)
        self.assertEqual(len(r['shows']),1)
        self.assertEqual(set(r['redirects']), ids-set(r['shows']))

    def test_query_and_path_case_retained(self):
        self.assertNotEqual(normalize_feed('https://x.test/A?q=1'),normalize_feed('https://x.test/a?q=1'))
        self.assertNotEqual(normalize_feed('https://x.test/a?q=1'),normalize_feed('https://x.test/a?q=2'))

    def test_spotify_id_matches_history(self):
        r=build([{'id':'spotify:show:abc','feed_url':'https://x.test/rss','title':'Test'}, {'ids':{'spotify':['spotify:show:abc']},'title':'Test'}])
        self.assertEqual(len(r['shows']),1)
        self.assertEqual(next(iter(r['shows'].values()))['ids']['spotify'],['spotify:show:abc'])

if __name__=='__main__':unittest.main()
