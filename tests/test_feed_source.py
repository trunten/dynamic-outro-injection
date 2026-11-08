"""Public Apple Podcasts directory links should be as easy as direct RSS URLs."""
import asyncio
import json

import pytest

from app import feed_source
from app.rss import InvalidFeed
from app.security import UnsafeSource

SHOW = "https://podcasts.apple.com/gb/podcast/accidental-tech-podcast/id617416468"
RSS = "https://cdn.atp.fm/rss/public?wtvryzdm"


def test_direct_rss_is_not_looked_up(monkeypatch):
    async def fail(*args, **kwargs):
        pytest.fail("Unexpected network lookup for a direct feed")
    monkeypatch.setattr(feed_source, "fetch_feed", fail)
    assert asyncio.run(feed_source.resolve_source_feed_url(RSS)) == RSS


def test_apple_show_link_is_resolved(monkeypatch):
    async def fake_fetch(url, max_bytes):
        assert url == "https://itunes.apple.com/lookup?id=617416468&entity=podcast"
        assert max_bytes == 128 * 1024
        return json.dumps({"resultCount": 1, "results": [{"collectionId": 617416468, "feedUrl": RSS}]}).encode()
    monkeypatch.setattr(feed_source, "fetch_feed", fake_fetch)
    for link in (SHOW, SHOW + "?uo=4", SHOW + "/"):
        assert asyncio.run(feed_source.resolve_source_feed_url(link)) == RSS


def test_apple_url_with_no_show_id_gets_helpful_error(monkeypatch):
    async def fail(*args, **kwargs):
        pytest.fail("Malformed show URLs must not trigger network fetch")
    monkeypatch.setattr(feed_source, "fetch_feed", fail)
    with pytest.raises(InvalidFeed, match="not a recognised show URL"):
        asyncio.run(feed_source.resolve_source_feed_url("https://podcasts.apple.com/gb/genre/technology"))


def test_missing_public_feed_is_explained(monkeypatch):
    async def fake_fetch(*args, **kwargs):
        return json.dumps({"results": [{"collectionId": 617416468}]}).encode()
    monkeypatch.setattr(feed_source, "fetch_feed", fake_fetch)
    with pytest.raises(InvalidFeed, match="does not provide a public RSS feed"):
        asyncio.run(feed_source.resolve_source_feed_url(SHOW))


def test_invalid_public_feed_link_is_blocked(monkeypatch):
    async def fake_fetch(*args, **kwargs):
        return json.dumps({"results": [{"collectionId": 617416468, "feedUrl": "http://127.0.0.1/private"}]}).encode()
    monkeypatch.setattr(feed_source, "fetch_feed", fake_fetch)
    with pytest.raises(InvalidFeed, match="inaccessible RSS address"):
        asyncio.run(feed_source.resolve_source_feed_url(SHOW))


def test_lookup_error_is_understandable(monkeypatch):
    async def fail(*args, **kwargs):
        raise TimeoutError("downstream timeout")
    monkeypatch.setattr(feed_source, "fetch_feed", fail)
    with pytest.raises(InvalidFeed, match="Could not look up"):
        asyncio.run(feed_source.resolve_source_feed_url(SHOW))


def test_non_public_urls_still_rejected():
    with pytest.raises(UnsafeSource):
        asyncio.run(feed_source.resolve_source_feed_url("http://localhost/feed.xml"))
