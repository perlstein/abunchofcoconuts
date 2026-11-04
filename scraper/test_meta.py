"""Unit tests for feed metadata extraction. Run: python -m unittest test_meta"""

import unittest

from scrape import parse_feed_meta

ITUNES = 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'


def feed(channel_body):
    return f'<?xml version="1.0"?><rss {ITUNES} version="2.0"><channel>{channel_body}</channel></rss>'.encode()


class TestParseFeedMeta(unittest.TestCase):
    def test_full_owner_block(self):
        meta = parse_feed_meta(feed("""
            <title>Show</title>
            <link>https://example.com</link>
            <description>A plain description.</description>
            <itunes:author>Host Person</itunes:author>
            <itunes:owner><itunes:name>Owner Co</itunes:name><itunes:email>own@example.com</itunes:email></itunes:owner>
            <item><enclosure url="https://media.example.com/ep1.mp3" type="audio/mpeg"/></item>
        """))
        self.assertEqual(meta["owner_name"], "Owner Co")
        self.assertEqual(meta["owner_email"], "own@example.com")
        self.assertEqual(meta["itunes_author"], "Host Person")
        self.assertEqual(meta["website"], "https://example.com")
        self.assertEqual(meta["description"], "A plain description.")
        self.assertEqual(meta["media_url"], "https://media.example.com/ep1.mp3")

    def test_author_survives_owner_in_common_tag_order(self):
        # <itunes:author> before <itunes:owner> must NOT be clobbered
        meta = parse_feed_meta(feed("""
            <itunes:author>NPR</itunes:author>
            <itunes:owner><itunes:name>NPR Ops</itunes:name><itunes:email>podcasts@npr.org</itunes:email></itunes:owner>
        """))
        self.assertEqual(meta["itunes_author"], "NPR")
        self.assertNotIn("(", meta["itunes_author"])
        self.assertEqual(meta["owner_name"], "NPR Ops")

    def test_channel_root_itunes_email_fallback(self):
        meta = parse_feed_meta(feed("<itunes:email>root@example.com</itunes:email>"))
        self.assertEqual(meta["owner_email"], "root@example.com")

    def test_owner_email_preferred_over_root_email(self):
        meta = parse_feed_meta(feed("""
            <itunes:email>root@example.com</itunes:email>
            <itunes:owner><itunes:email>owner@example.com</itunes:email></itunes:owner>
        """))
        self.assertEqual(meta["owner_email"], "owner@example.com")

    def test_webmaster_and_managing_editor_do_not_leak(self):
        meta = parse_feed_meta(feed("""
            <webMaster>webmaster@example.com (Web Master)</webMaster>
            <managingEditor>editor@example.com (Ed Itor)</managingEditor>
        """))
        self.assertIsNone(meta["owner_name"])
        self.assertIsNone(meta["owner_email"])
        self.assertIsNone(meta["itunes_author"])

    def test_html_stripped_from_description(self):
        meta = parse_feed_meta(feed(
            "<description><![CDATA[<p>Hello <b>world</b> &amp; friends</p>]]></description>"))
        self.assertEqual(meta["description"], "Hello world & friends")

    def test_markup_only_description_is_null(self):
        meta = parse_feed_meta(feed("<description><![CDATA[<p></p>]]></description>"))
        self.assertIsNone(meta["description"])

    def test_description_truncated_to_300(self):
        meta = parse_feed_meta(feed(f"<description>{'word ' * 100}</description>"))
        self.assertLessEqual(len(meta["description"]), 301)
        self.assertTrue(meta["description"].endswith("…"))

    def test_missing_fields_are_null(self):
        meta = parse_feed_meta(feed("<title>Bare</title>"))
        for key in ("owner_name", "owner_email", "itunes_author", "website", "description", "media_url"):
            self.assertIsNone(meta[key], key)

    def test_malformed_xml_never_raises(self):
        meta = parse_feed_meta(b"<rss><channel><title>broken")
        self.assertIsNone(meta["owner_email"])

    def test_https_itunes_namespace_variant(self):
        content = f'''<?xml version="1.0"?>
        <rss xmlns:itunes="https://www.itunes.com/dtds/podcast-1.0.dtd" version="2.0"><channel>
        <itunes:owner><itunes:email>sec@example.com</itunes:email></itunes:owner>
        </channel></rss>'''.encode()
        self.assertEqual(parse_feed_meta(content)["owner_email"], "sec@example.com")



class TestPodcastGuid(unittest.TestCase):
    def parse(self, body):
        return parse_feed_meta(('<rss xmlns:podcast="https://podcastindex.org/namespace/1.0" version="2.0"><channel><title>Example</title>' + body + '</channel></rss>').encode())

    def test_guid_with_attributes_uses_text_not_feedparser_dictionary(self):
        for attrs in ('', ' isPermaLink="false"', ' custom="yes"'):
            with self.subTest(attrs=attrs):
                self.assertEqual(self.parse('<podcast:guid'+attrs+'> good-guid </podcast:guid>')['podcast_guid'], 'good-guid')

    def test_non_guid_values_and_episode_guids_are_not_show_identity(self):
        for body in ('<podcast:guid value="bad"/>', '<podcast:guid>  </podcast:guid>',
                     '<item><guid>episode-id</guid><podcast:guid>episode-podcast-id</podcast:guid></item>', '<guid>non-namespaced</guid>'):
            with self.subTest(body=body):
                self.assertIsNone(self.parse(body)['podcast_guid'])

    def test_feed_with_attributes_can_build_and_publish_a_complete_registry(self):
        import tempfile
        from pathlib import Path
        from research import build_registry, publish, load
        record = dict(self.parse('<podcast:guid isPermaLink="false">good-guid</podcast:guid>'), feed_url='https://example.com/rss')
        registry = build_registry([record], {}, [], '2026-11-04')
        with tempfile.TemporaryDirectory() as folder:
            publish(Path(folder), registry)
            self.assertTrue(load(Path(folder)/'research_manifest.json', {})['files'])


if __name__ == "__main__":
    unittest.main()
