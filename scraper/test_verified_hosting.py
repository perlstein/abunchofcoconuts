import unittest
from unittest.mock import Mock, patch

import cumulative
import scrape
import spotify_scrape
from hosts import detect_host
from test_research import build, record


class VerifiedHostingTests(unittest.TestCase):
    def test_orphaned_legacy_detection_remains_in_permanent_history(self):
        registry = build([], legacy=[{'feed_url': 'https://retired.test/feed',
            'title': 'Archived show', 'from': 'Libsyn', 'to': 'PRX', 'date': '2026-01-01'}])
        show = next(iter(registry['shows'].values()))
        self.assertEqual(show['title'], 'Archived show')
        self.assertEqual(show['events'][0]['status'], 'legacy_unverified')

    def test_libsyn_redirect_uses_new_response_and_media(self):
        response = Mock(url='https://feeds.megaphone.fm/new', content=b'''
            <rss version="2.0"><channel><title>A</title><item>
            <enclosure url="https://traffic.megaphone.fm/a.mp3"/>
            </item></channel></rss>''')
        with patch.object(scrape.requests, 'get', return_value=response), patch.object(scrape.time, 'sleep'):
            host, meta = scrape.fetch_show_feed('https://old.libsyn.com/rss')
        self.assertEqual(host, 'Megaphone')
        self.assertTrue(meta['host_evidence']['success'])
        self.assertFalse(meta['host_evidence']['conflict'])

    def test_html_error_page_is_not_hosting_evidence(self):
        response = Mock(url='https://feeds.megaphone.fm/new', content=b'<html>Maintenance</html>')
        with patch.object(scrape.requests, 'get', return_value=response):
            host, meta = scrape.fetch_show_feed('https://old.libsyn.com/rss')
        self.assertEqual(host, 'Unknown')
        self.assertFalse(meta['host_evidence']['success'])

    def test_hostname_boundaries(self):
        self.assertEqual(detect_host('https://notlibsyn.com/feed'), 'Other/Self-Hosted')
        self.assertEqual(detect_host('https://libsyn.com.attacker.test/feed'), 'Other/Self-Hosted')

    def test_spotify_refreshes_cached_host_once_per_feed(self):
        rec = {'host': 'Libsyn', 'feed_url': 'https://old.libsyn.com/rss', 'owner_email': 'old@example.com'}
        shows = {'one': {'rec': dict(rec)}, 'two': {'rec': dict(rec)}}
        evidence = {'success': True, 'checked_at': '2026-11-03T10:00:00Z', 'host': 'Megaphone'}
        with patch.object(spotify_scrape, 'load_json', return_value={}), patch.object(
                spotify_scrape, 'fetch_show_feed', return_value=('Megaphone', {'host_evidence': evidence})) as fetch:
            cache = {}
            spotify_scrape.refresh_host_evidence(shows, cache, '2026-11-03')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(cache['one']['host'], 'Megaphone')
        self.assertEqual(cache['two']['owner_email'], 'old@example.com')

    def test_failed_spotify_refresh_does_not_present_cache_as_current(self):
        shows = {'one': {'rec': {'host': 'Libsyn', 'feed_url': 'https://x.test/rss'}}}
        with patch.object(spotify_scrape, 'load_json', return_value={}), patch.object(
                spotify_scrape, 'fetch_show_feed', return_value=('Unknown', {'host_evidence': {'success': False}})):
            spotify_scrape.refresh_host_evidence(shows, {}, '2026-11-03')
        self.assertEqual(shows['one']['rec']['host'], 'Unknown')
        self.assertEqual(shows['one']['rec']['last_known_host'], 'Libsyn')

    def test_stale_label_cannot_undo_current_placement(self):
        state = {'https://x.test': {'host': 'Megaphone', 'last_checked': '2026-11-02'}}
        cumulative.apply_observation(state, 'https://x.test', 'Libsyn', '2026-11-03')
        self.assertEqual(state['https://x.test']['host'], 'Megaphone')
        self.assertEqual(state['https://x.test']['last_checked'], '2026-11-02')

    def test_confirmation_must_fit_seven_day_window(self):
        first = build([record()])
        later = build([record(day='2026-09-20')], first, day='2026-09-20')
        self.assertEqual(next(iter(later['shows'].values()))['hosting_status'], 'pending')

    def test_failure_interrupts_pending_confirmation(self):
        first = build([record()])
        failed = record(day='2026-09-02')
        failed['host_evidence']['success'] = False
        second = build([failed], first, day='2026-09-02')
        third = build([record(day='2026-09-03')], second, day='2026-09-03')
        self.assertEqual(next(iter(third['shows'].values()))['hosting_status'], 'pending')

    def test_initial_placement_keeps_its_confirmation_evidence(self):
        first = build([record()])
        second = build([record(day='2026-09-02')], first, day='2026-09-02')
        show = next(iter(second['shows'].values()))
        self.assertEqual(show['confidence']['level'], 'high')
        self.assertEqual(show['placements'][0]['confirmation_dates'], ['2026-09-01', '2026-09-02'])
        self.assertEqual(len(show['placements'][0]['evidence']), 2)
        self.assertEqual(show['events'], [])


if __name__ == '__main__':
    unittest.main()
