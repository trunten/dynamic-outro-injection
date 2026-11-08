"""Resolve public Apple Podcasts show links to the publisher's actual RSS URL.

Only the fixed Apple lookup endpoint is contacted for Apple directory links.
The resolved URL still goes through the application's existing public-URL
validation and RSS/audio-enclosure checks. Direct RSS URLs are unchanged.
"""
import json
import re
from urllib.parse import urlsplit

from .rss import InvalidFeed
from .security import fetch_feed, validate_source_url

# Apple show links normally contain a country, /podcast/, an optional show
# slug, and a numeric ID. Do not interpret arbitrary Apple URLs as a feed.
APPLE_SHOW_PATH = re.compile(r"^/(?:[A-Za-z]{2}/)?podcast/(?:[^/]+/)?id([1-9][0-9]{0,18})/?$")


async def resolve_source_feed_url(url: str) -> str:
    """Return a validated public RSS URL, resolving Apple show URLs if needed.

    An Apple Podcasts directory page is HTML, not an RSS document. The public
    iTunes Lookup API exposes the podcast's original feedUrl for many shows.
    """
    url = validate_source_url(url)
    parsed = urlsplit(url)
    if (parsed.hostname or "").rstrip(".").lower() != "podcasts.apple.com":
        return url

    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise InvalidFeed("Apple Podcasts show links must use HTTPS")
    match = APPLE_SHOW_PATH.fullmatch(parsed.path)
    if not match:
        raise InvalidFeed(
            "This Apple Podcasts link is not a recognised show URL. "
            "Paste the show's page link or its direct RSS feed address."
        )
    show_id = match.group(1)
    endpoint = f"https://itunes.apple.com/lookup?id={show_id}&entity=podcast"
    try:
        body = await fetch_feed(endpoint, max_bytes=128 * 1024)
        payload = json.loads(body)
        results = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list):
            raise ValueError("Unexpected Apple lookup result")
    except Exception as exc:
        raise InvalidFeed(
            "Could not look up this show in Apple Podcasts. "
            "Try pasting its direct RSS feed address instead."
        ) from exc

    for item in results:
        if not isinstance(item, dict):
            continue
        result_id = item.get("collectionId", item.get("trackId"))
        if str(result_id) != show_id:
            continue
        feed = item.get("feedUrl")
        if not isinstance(feed, str) or not feed.strip():
            break
        try:
            return validate_source_url(feed.strip())
        except ValueError as exc:
            raise InvalidFeed(
                "Apple returned an inaccessible RSS address for this show. "
                "Please use a public, direct RSS feed address."
            ) from exc

    raise InvalidFeed(
        "Apple Podcasts does not provide a public RSS feed for this show. "
        "Try entering its direct RSS feed address instead."
    )
