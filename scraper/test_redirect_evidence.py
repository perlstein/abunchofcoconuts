import unittest
from unittest.mock import patch, Mock
from scrape import fetch_show_feed

RSS=b'''<rss version="2.0"><channel><title>Twenty Thousand Hertz</title><item><title>An episode</title><enclosure url="https://dts.podtrac.com/redirect.mp3/dovetail.prxu.org/20k/episode.mp3" type="audio/mpeg"/></item></channel></rss>'''

class RedirectEvidenceTests(unittest.TestCase):
    @patch('scrape.time.sleep')
    @patch('scrape.requests.get')
    def test_old_megaphone_url_is_classified_from_actual_response(self,get,sleep):
        get.return_value=Mock(url='https://feed.20k.org',content=RSS)
        host,meta=fetch_show_feed('https://feeds.megaphone.fm/20k')
        self.assertEqual(host,'PRX');self.assertTrue(meta['host_evidence']['success'])
        self.assertEqual(meta['resolved_feed_url'],'https://feed.20k.org')
        self.assertFalse(meta['host_evidence']['conflict'])

    @patch('scrape.time.sleep')
    @patch('scrape.requests.get')
    def test_disagreeing_feed_and_media_hosts_remain_conflicting(self,get,sleep):
        get.return_value=Mock(url='https://feeds.megaphone.fm/active',content=RSS)
        _,meta=fetch_show_feed('https://feeds.megaphone.fm/active')
        self.assertTrue(meta['host_evidence']['conflict'])

    @patch('scrape.requests.get')
    def test_failed_fetch_has_no_success_evidence(self,get):
        import requests
        get.side_effect=requests.Timeout('test timeout')
        _,meta=fetch_show_feed('https://feeds.megaphone.fm/old')
        self.assertNotIn('host_evidence',meta)

if __name__=='__main__':unittest.main()
