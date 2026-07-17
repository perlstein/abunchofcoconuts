# Podcast Hosting Leaderboard — data engine

Open dataset and scrapers tracking **which hosting platform powers each show**
across the Apple Podcasts and Spotify top charts, plus host-migration history
over time.

Everything here is derived from public sources: the Apple and Spotify public
charts, the Podcast Index trending feed, and each show's own publicly published
RSS feed. No private or proprietary data is included.

## What's in `data/`

| File | What it is |
|------|------------|
| `leaderboard.json`, `spotify_leaderboard.json` | Current hosting breakdown per chart |
| `history.json`, `spotify_history.json` | Platform share over time |
| `show_history.json`, `spotify_show_history.json` | Per-show host history |
| `host_state.json`, `spotify_host_state.json` | Last-seen host per show (scraper state) |
| `tracked_feeds.json` | Rolling corpus of feeds watched for host changes |
| `host_moves.json` | Detected host migrations, newest first |
| `trending.json` | Podcast Index trending feed |
| `video.json`, `apple_video_shows.json` | Apple-native video show detection |
| `corpus_feeds.csv` | Blended list of feed URLs the tracker re-checks |

## Scrapers (`scraper/`)

Plain Python (`requests` + `feedparser`), run daily by GitHub Actions
(`.github/workflows/scrape.yml`):

- `scrape.py` — Apple chart leaderboard + histories
- `spotify_scrape.py` — Spotify leaderboard (optional Podcast Index creds improve coverage)
- `trending.py` — Podcast Index trending
- `apple_video.py` / `video.py` — Apple-native video detection
- `cumulative.py` — re-checks the feed corpus for host changes, writes `host_moves.json`
- `hosts.py` — maps a feed's delivery domain to its hosting platform

Run locally:

```
pip install -r scraper/requirements.txt
python scraper/scrape.py
```

Optional Podcast Index API creds (for wider Spotify/trending coverage) are read
from `PODCASTINDEX_KEY` / `PODCASTINDEX_SECRET` env vars, or a gitignored
`scraper/podcastindex_creds.json`.

## How host detection works

A show's hosting platform is inferred from the delivery domain in its RSS
enclosure URLs (see `hosts.py`). Host **changes** are detected by re-fetching
feeds in `corpus_feeds.csv` on a rolling, time-boxed budget and comparing the
current host to the last seen one.
