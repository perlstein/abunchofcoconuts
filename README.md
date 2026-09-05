# Chart Feed Tracker

Scripts that snapshot Apple Podcasts and Spotify top-chart data and the
delivery domain behind each show's RSS feed over time.

Everything here is derived from public sources: the Apple and Spotify public
charts, the Podcast Index trending feed, and each show's own publicly published
RSS feed. No private or proprietary data is included.

## What's in `data/`

| File | What it is |
|------|------------|
| `leaderboard.json`, `spotify_leaderboard.json` | Current chart snapshot with feed-domain breakdown |
| `history.json`, `spotify_history.json` | Feed-domain share over time |
| `show_history.json`, `spotify_show_history.json` | Per-show feed-domain history |
| `host_state.json`, `spotify_host_state.json` | Last-seen feed domain per show (scraper state) |
| `tracked_feeds.json` | Rolling corpus of feeds watched for domain changes |
| `host_moves.json` | Detected feed-domain changes, newest first |
| `trending.json` | Podcast Index trending feed |
| `video.json`, `apple_video_shows.json` | Apple-native video show detection |
| `corpus_feeds.csv` | Blended list of feed URLs the tracker re-checks |
| `network_seed.csv` | Networks to track (Apple channel id and/or show ids) |
| `network_feeds.csv`, `networks.json` | Resolved per-network feeds + host breakdown (built by `networks.py`) |

## Scrapers (`scraper/`)

Plain Python (`requests` + `feedparser`), run daily by GitHub Actions
(`.github/workflows/scrape.yml`):

- `scrape.py` — Apple chart snapshot + histories
- `spotify_scrape.py` — Spotify chart snapshot (optional Podcast Index creds improve coverage)
- `subgenres.py` — enumerates Apple sub-genre charts, seeds the re-check corpus
- `trending.py` — Podcast Index trending
- `apple_video.py` / `video.py` — Apple-native video detection
- `cumulative.py` — re-checks the feed corpus for domain changes, writes `host_moves.json`
- `networks.py` — resolves whole *networks* (a publisher's shows) to their feeds + hosts
- `hosts.py` — maps a feed's delivery domain to a label

## Tracking a network

A *network* is a publisher whose shows you want to follow as a group (e.g. The
Washington Post, Higher Ground, Campside Media). Add one row to
`data/network_seed.csv` — an Apple Podcasts channel id (the `/id` number in a
`podcasts.apple.com/.../channel/.../id<N>` URL) and/or a `;`-separated list of
Apple show ids — then run `networks.py`. It resolves every show to its RSS feed
via the iTunes API, detects each feed's delivery host, and writes
`network_feeds.csv` (picked up by `cumulative.py` as always-checked watchlist
feeds) and `networks.json` (per-network roster + host breakdown).

This is a **standalone, limited scan** — it touches only the seeded networks,
never the whole corpus, and does not depend on the daily chart scrape:

```
python scraper/networks.py                              # all seeded networks
NETWORKS_ONLY="The Washington Post" python scraper/networks.py   # just one
NETWORKS_NO_CHANNEL_SCRAPE=1 python scraper/networks.py  # trust curated ids only
```

`networks.json` gives you the host breakdown immediately. To fold the new feeds
into the long-term host-move tracker without a full corpus re-check, follow with
`CUMULATIVE_SEED_ONLY=1 python scraper/cumulative.py`. In CI, the standalone
**Networks Resolve** workflow (`.github/workflows/networks.yml`) runs this — on a
daily schedule and on demand (Actions → Networks Resolve → Run workflow, with an
optional label filter) — and commits the results to `main`.

Run locally:

```
pip install -r scraper/requirements.txt
python scraper/scrape.py
```

Optional Podcast Index API creds (for wider Spotify/trending coverage) are read
from `PODCASTINDEX_KEY` / `PODCASTINDEX_SECRET` env vars, or a gitignored
`scraper/podcastindex_creds.json`.

## How domain detection works

A show's feed-delivery domain is read from its RSS enclosure URLs (see
`hosts.py`). Domain **changes** are detected by re-fetching feeds in
`corpus_feeds.csv` on a rolling, time-boxed budget and comparing the current
domain to the last seen one.

## Durable research records (September 2026)

`cumulative.py` now finishes by building `research.py`. The new public outputs
are `show_index.json`, `shows/<shard>.json`, `host_moves.json` (schema 2), and
`research_manifest.json`. `show_registry.json` is the retained state, including
per-show chart observations, contact metadata with observation dates, identities,
and event history. Keep this state between runs. Records are never pruned merely
because a show leaves a chart.

Identity links use directory IDs, normalized feed URLs, verified redirect
URLs, or podcast GUIDs. Equal titles do not merge shows. Former record IDs redirect
to the surviving record after an identity merge. `backfill_identity.py` can recover
Apple-ID-to-feed associations from the public lookup API for archived chart IDs;
its checked-in `identity_links.json` preserves every returned association.

Hosting is detected from the final HTTP response and enclosure origin, so a
retired Megaphone URL redirecting to a PRX feed no longer counts as Megaphone.
Disagreeing feed/media or alias observations are flagged. Cached Spotify bridge
records are refreshed for hosting evidence and public metadata every scan.

A placement requires successful, consistent RSS observations on two distinct UTC
dates. A migration requires both a previously confirmed placement and two-date
confirmation of the new placement. Replaying a scan cannot confirm a move.
Confirmation dates are observation dates, not claimed contract-effective dates.
Every newly confirmed migration retains both observations in the event history;
later moves do not overwrite earlier ones. Available legacy detections remain
unverified, with same-day opposite directions labeled as conflicts. Earlier events
already overwritten by the old tracker cannot be certified retroactively.

The frontend consumes the complete checksummed research generation. Run
`python -m unittest discover -s scraper -p 'test_*.py'` for regression coverage.
