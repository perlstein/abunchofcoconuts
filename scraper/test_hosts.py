"""Unit tests for host detection. Run: python -m unittest test_hosts"""

import unittest

from hosts import UNMATCHED, detect_host


class TestDetectHost(unittest.TestCase):
    def test_feed_domain_matches(self):
        cases = [
            ("https://feeds.megaphone.fm/RM4031649020", "Megaphone"),
            ("https://feeds.simplecast.com/Sl5CSM3S", "Simplecast"),
            ("https://rss.libsyn.com/shows/67940/destinations/278377.xml", "Libsyn"),
            ("https://feeds.libsynpro.com/show/feed", "Libsyn"),
            ("https://rss.art19.com/the-daily", "Art19"),
            ("https://feeds.buzzsprout.com/123456.rss", "Buzzsprout"),
            ("https://feed.podbean.com/myshow/feed.xml", "Podbean"),
            ("https://feeds.acast.com/public/shows/some-show", "Acast"),
            ("https://anchor.fm/s/10a340624/podcast/rss", "Spotify/Anchor"),
            ("https://www.omnycontent.com.omny.fm/feed", "Omny / Triton"),
            # Omny feed domain alone (no fetch needed) — the Culturistas/Jay Shetty case
            ("https://www.omnycontent.com/d/playlist/abc/def/ghi/podcast.rss", "Omny / Triton"),
            ("https://audioboom.com/channels/12345.rss", "Audioboom"),
            ("https://feeds.transistor.fm/my-show", "Transistor"),
            ("https://feeds.redcircle.com/abc-def", "RedCircle"),
            ("https://feeds.captivate.fm/my-show/", "Captivate"),
            ("https://www.spreaker.com/show/123/episodes/feed", "Spreaker"),
            ("https://feeds.soundcloud.com/users/soundcloud:users:1/sounds.rss", "SoundCloud"),
            ("https://media.rss.com/myshow/feed.xml", "RSS.com"),
            ("https://feeds.fireside.fm/show/rss", "Fireside"),
        ]
        for url, expected in cases:
            self.assertEqual(detect_host(url), expected, url)

    def test_media_domain_fallback(self):
        # Unrecognized feed domain, host identified from the enclosure URL
        self.assertEqual(
            detect_host("https://feeds.example.org/show.xml",
                        "https://traffic.megaphone.fm/EP123.mp3"),
            "Megaphone")
        self.assertEqual(
            detect_host("https://www.someblog.com/feed.xml",
                        "https://npr.simplecastaudio.com/abc/episode.mp3"),
            "Simplecast")
        self.assertEqual(
            detect_host("https://feeds.example.org/show.xml",
                        "https://media123.lscdn.net/episode.mp3"),
            "Libsyn")

    def test_prefix_chain_in_media_url_path(self):
        # Measurement prefixes wrap the real host inside the URL path
        self.assertEqual(
            detect_host("https://feeds.example.org/show.xml",
                        "https://dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/EP1.mp3"),
            "Megaphone")
        self.assertEqual(
            detect_host("https://feeds.example.org/show.xml",
                        "https://pscrb.fm/rss/p/dovetail.prxu.org/123/episode.mp3"),
            "PRX")

    def test_prefix_domains_are_never_a_host(self):
        # A bare prefix wrapper with no recognizable origin must NOT be
        # attributed to the prefix service; it falls through to Other.
        for prefix_url in [
            "https://dts.podtrac.com/redirect.mp3/example.com/ep.mp3",
            "https://pscrb.fm/rss/p/example.org/ep.mp3",
            "https://pdst.fm/e/example.net/ep.mp3",
            "https://pdrl.fm/abc/example.com/ep.mp3",
            "https://claritaspod.com/m/example.com/ep.mp3",
            "https://chrt.fm/track/123/example.com/ep.mp3",
        ]:
            self.assertEqual(detect_host("https://feeds.example.org/s.xml", prefix_url),
                             UNMATCHED, prefix_url)

    def test_real_origin_behind_stacked_prefixes(self):
        # Several measurement prefixes stacked before the true host.
        url = ("https://pdrl.fm/x/claritaspod.com/m/chrt.fm/t/pdst.fm/e/"
               "dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/EP.mp3")
        self.assertEqual(detect_host("https://feeds.example.org/s.xml", url), "Megaphone")
        # Libsyn origin behind podtrac + chartable
        url2 = "https://dts.podtrac.com/redirect.mp3/chrt.fm/track/G/traffic.libsyn.com/x/ep.mp3"
        self.assertEqual(detect_host("https://feeds.example.org/s.xml", url2), "Libsyn")

    def test_feed_url_checked_before_media_url(self):
        self.assertEqual(
            detect_host("https://feeds.simplecast.com/abc",
                        "https://traffic.megaphone.fm/EP1.mp3"),
            "Simplecast")

    def test_unmatched_and_empty(self):
        self.assertEqual(detect_host("https://www.thisamericanlife.org/podcast/rss.xml",
                                     "https://stream.thisamericanlife.org/ep.mp3"),
                         UNMATCHED)
        self.assertEqual(detect_host("", ""), UNMATCHED)
        self.assertEqual(detect_host("", None), UNMATCHED)
        self.assertEqual(detect_host("https://feeds.npr.org/510289/podcast.xml"), UNMATCHED)

    def test_case_insensitive(self):
        self.assertEqual(detect_host("https://Feeds.MEGAPHONE.fm/ABC"), "Megaphone")

    def test_narrow_pod_co_pattern(self):
        # feed.pod.co is Podcast.co, but "...pod.com" domains must NOT match
        self.assertEqual(detect_host("https://feed.pod.co/the-caregivers-journey"), "Podcast.co")
        self.assertEqual(detect_host("https://feeds.mypod.com/show.xml"), UNMATCHED)

    def test_verified_additions(self):
        cases = [
            ("https://rss.amperwave.net/v2/feed/abc", "AmperWave (Audacy)"),
            ("https://rss2.flightcast.com/xyz.xml", "Flightcast"),
            ("https://api.substack.com/feed/podcast/123.rss", "Substack"),
            ("https://api.riverside.fm/hosting/abc.rss", "Riverside"),
            ("https://feeds.cohostpodcasting.com/show", "CoHost"),
            ("https://feed.cdnstream1.com/show", "SoundStack"),
            ("https://publicfeeds.net/f/3492/feed-rss.xml", "PRX"),
            ("https://feed.xyzfm.space/abcdef", "Xiaoyuzhou FM"),
        ]
        for url, expected in cases:
            self.assertEqual(detect_host(url), expected, url)


if __name__ == "__main__":
    unittest.main()
