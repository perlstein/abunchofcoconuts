"""Hosting platform detection.

Maps feed/media URL domains to canonical hosting platform names.
Patterns are checked as substrings so subdomains (traffic.megaphone.fm,
feeds.simplecast.com) match without needing their own entries.
"""

import re
from urllib.parse import urlparse

HOST_PATTERNS = {
    # Domain substring -> Platform name
    "megaphone.fm": "Megaphone",
    "megaphone.audio": "Megaphone",
    "simplecast.com": "Simplecast",
    "simplecast.audio": "Simplecast",
    "libsyn.com": "Libsyn",
    "libsynpro.com": "Libsyn",
    "libsynstaging.com": "Libsyn",
    "art19.com": "Art19",
    "buzzsprout.com": "Buzzsprout",
    "podbean.com": "Podbean",
    "acast.com": "Acast",
    "acastcdn.com": "Acast",
    "anchor.fm": "Spotify/Anchor",
    "podcasters.spotify.com": "Spotify/Anchor",
    "podomatic.com": "Spotify/Anchor",
    "iheart.com": "iHeart / Triton",
    "tritondigital.com": "iHeart / Triton",
    "omny.fm": "Omny / Triton",
    "omnycontent.com": "Omny / Triton",  # Omny's feed + CDN domain (www.omnycontent.com/d/playlist/...)
    "audioboom.com": "Audioboom",
    "transistor.fm": "Transistor",
    "blubrry.com": "Blubrry",
    "blubrry.net": "Blubrry",
    "redcircle.com": "RedCircle",
    "captivate.fm": "Captivate",
    "flightcast.audio": "Flightcast",
    "barstoolsports.com": "Barstool Sports",
    "fireside.fm": "Fireside",
    "rss.com": "RSS.com",
    "soundcloud.com": "SoundCloud",
    "pinecast.com": "Pinecast",
    "spreaker.com": "Spreaker",
    "podcastics.com": "Podcastics",
    "ausha.co": "Ausha",
    "zencast.fm": "ZenCast",
    "podcastpage.io": "Podcast Page",
    "castos.com": "Castos",
    "subsplash.com": "Subsplash",
    "podvine.com": "Podvine",
    "supportingcast.fm": "Supporting Cast",
    "whooshkaa.com": "Whooshkaa",
    "hearthis.at": "HearThis",
    "lscdn.net": "Libsyn",
    "traffic.libsyn.com": "Libsyn",
    # Verified additions beyond the base table (real platforms that showed
    # up as Other/Self-Hosted in live chart data)
    "flightcast.com": "Flightcast",
    "amperwave.net": "AmperWave (Audacy)",
    "amperwavepodcasting.com": "AmperWave (Audacy)",
    "substack.com": "Substack",
    "podetize.com": "Podetize",
    "publicfeeds.net": "PRX",
    "prxu.org": "PRX",
    "xyzfm.space": "Xiaoyuzhou FM",
    "simplecastaudio.com": "Simplecast",  # Simplecast's media CDN (npr.*, nbcnews.*)
    "riverside.fm": "Riverside",
    "api.riverside.com": "Riverside",
    "cohostpodcasting.com": "CoHost",
    "fountain.fm": "Fountain",
    "podtoo.com": "PodToo",
    "castbox.fm": "Castbox",
    "feed.pod.co": "Podcast.co",  # deliberately narrow: bare "pod.co" would match *pod.com domains
    "downloads.pod.co": "Podcast.co",
    "alitu.com": "Alitu",
    "cdnstream1.com": "SoundStack",
    "podcastai.com": "PodcastAI",
    "fusebox.fm": "Fusebox",
    "thisisdistorted.com": "This Is Distorted",
    # Self-hosting broadcasters / networks (labeled for clarity)
    "bbci.co.uk": "BBC",
    "bbc.co.uk": "BBC",
    "publicradio.org": "American Public Media",  # Marketplace, APM family
    "twit.tv": "TWiT",                           # also caught via pdrl.fm path embedding
    "abc.net.au": "ABC Australia",
    # International hosting platforms surfaced via leaderboard audit
    "ximalaya.com": "Ximalaya",
    "podigee.io": "Podigee",
    "ivoox.com": "iVoox",
    "audiomeans.fr": "Audiomeans",
    "wavlake.com": "Wavlake",
    "thmanyah.com": "Thmanyah",
    # Smaller platforms found in Other/Self-Hosted bucket
    "beamly.com": "Beamly",
    "kajabi.com": "Kajabi",
    "podcastle.ai": "Podcastle",
    "futurimedia.com": "FuturiMedia",
    "streamguys1.com": "StreamGuys",
    "svmaudio.com": "SVMAudio",
}

UNMATCHED = "Other/Self-Hosted"

# Tracking / measurement / streaming-ad-insertion "prefix" domains. These are
# prepended to the real media URL (e.g. dts.podtrac.com/redirect.mp3/<real
# host>/file.mp3); they are NOT hosting platforms. They must be ignored when
# detecting a host, otherwise the prefix masks where the audio actually lives.
# The real origin is the LAST host domain in the chain. Listed here so they are
# never mistaken for a host and so the rightmost-origin logic skips past them.
PREFIX_DOMAINS = (
    "podtrac.com",      # Podtrac (dts.podtrac.com)
    "pscrb.fm",         # Spotify / Megaphone streaming ad insertion
    "pdst.fm",          # Podsights
    "pdrl.fm",          # Podroll
    "claritaspod.com",  # Claritas
    "chrt.fm",          # Chartable
    # same family (analytics/measurement prefixes), not in the user's list but
    # identical in behavior — trim if unwanted:
    "chartable.com",
    "podscribe.com",    # verifi.podscribe.com
    "mgln.ai",          # Magellan AI
    "arttrk.com",       # ArtsAI
    "op3.dev",          # OP3 open analytics prefix
)

# domain-like tokens, including ones embedded in a redirect path
_DOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+")


def _domain(url: str) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def _match(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    for pattern, name in HOST_PATTERNS.items():
        if pattern in text:
            return name
    return ""


def _is_prefix(host: str) -> bool:
    return any(host == p or host.endswith("." + p) for p in PREFIX_DOMAINS)


def _origin_match(url: str) -> str:
    """Match a hosting platform from a URL, skipping tracking-prefix domains and
    preferring the rightmost (true origin) host in a redirect/prefix chain."""
    result = ""
    for token in _DOMAIN_RE.findall((url or "").lower()):
        if _is_prefix(token):
            continue
        name = _match(token)
        if name:
            result = name  # keep the last match: the real origin sits at the end
    return result


def detect_host(feed_url: str, media_url: str = None) -> str:
    """Return the canonical platform name for a show.

    Checks the feed URL first (most reliable), then the media URL. Both are
    scanned for every embedded domain so measurement prefixes like
    dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/... resolve to the real
    origin (Megaphone), never to the prefix. See PREFIX_DOMAINS.
    """
    return _origin_match(feed_url) or _origin_match(media_url) or UNMATCHED
