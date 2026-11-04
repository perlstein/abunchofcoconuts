import unittest

from research import build_registry, tokens
from scrape import parse_feed_meta


def feed(guid):
    return ('''<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
      <channel><title>Example</title>''' + guid + '</channel></rss>').encode()


class PodcastGuidTests(unittest.TestCase):
    def test_attributed_guid_keeps_text_and_merges_verified_identity(self):
        plain = parse_feed_meta(feed('<podcast:guid> show-identifier </podcast:guid>'))
        attributed = parse_feed_meta(feed(
            '<podcast:guid isPermaLink="false">show-identifier</podcast:guid>'))
        self.assertEqual(plain['podcast_guid'], 'show-identifier')
        self.assertEqual(attributed['podcast_guid'], plain['podcast_guid'])
        registry = build_registry([
            dict(plain, feed_url='https://old.example/rss'),
            dict(attributed, feed_url='https://new.example/rss'),
        ], {}, [], '2026-11-04')
        self.assertEqual(len(registry['shows']), 1)

    def test_attributes_are_not_identifiers(self):
        for tag in ('<podcast:guid isPermaLink="false"/>',
                    '<podcast:guid value="not-element-text"/>',
                    '<podcast:guid> </podcast:guid>',
                    '<podcast:guid><value>nested</value></podcast:guid>',
                    '<podcast:guid>one</podcast:guid><podcast:guid>two</podcast:guid>'):
            with self.subTest(tag=tag):
                self.assertIsNone(parse_feed_meta(feed(tag))['podcast_guid'])

    def test_cached_malformed_guids_do_not_crash_or_merge_unrelated_shows(self):
        for bad in ({'ispermalink': 'false'}, {'value': 'shared'}, ['shared'], 1, True):
            with self.subTest(value=bad):
                rows = [{'feed_url': 'https://' + host + '/rss', 'podcast_guid': bad}
                        for host in ('one.example', 'two.example')]
                registry = build_registry(rows, {}, [], '2026-11-04')
                self.assertEqual(len(registry['shows']), 2)
                self.assertFalse(any(t.startswith('guid:') for t in tokens(rows[0])))
                # Malformed archived records must also survive another run.
                for row in registry['shows'].values():
                    row['podcast_guid'] = bad
                self.assertEqual(len(build_registry([], registry, [], '2026-11-05')['shows']), 2)


if __name__ == '__main__':
    unittest.main()
