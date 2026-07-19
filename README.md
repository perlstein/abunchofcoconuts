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

## Scrapers (`scraper/`)

Plain Python (`requests` + `feedparser`), run daily by GitHub Actions
(`.github/workflows/scrape.yml`):

- `scrape.py` — Apple chart snapshot + histories
- `spotify_scrape.py` — Spotify chart snapshot (optional Podcast Index creds improve coverage)
- `subgenres.py` — enumerates Apple sub-genre charts, seeds the re-check corpus
- `trending.py` — Podcast Index trending
- `apple_video.py` / `video.py` — Apple-native video detection
- `cumulative.py` — re-checks the feed corpus for domain changes, writes `host_moves.json`
- `hosts.py` — maps a feed's delivery domain to a label

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
