from xml.etree import ElementTree as ET
import pytest
from app.rss import InvalidFeed, mirror_feed, ITUNES, ATOM

SAMPLE = b'''<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
<channel><title>The Example Podcast</title><description>My original description.</description>
<itunes:image href="https://cdn.example.com/cover.jpg"/>
<atom:link rel="self" href="https://example.com/old.xml"/>
<item><title>Episode 1</title><guid>ep-1</guid><itunes:duration>1:30</itunes:duration>
<enclosure url="https://cdn.example.com/episode1.mp3" length="1000" type="audio/mpeg"/></item>
<item><title>Episode 2</title><guid>ep-2</guid>
<enclosure url="https://cdn.example.com/episode2.m4a" length="500" type="audio/mp4"/></item>
</channel></rss>'''


def test_rss_rewrites_only_audio_and_preserves_other_metadata():
    mid = "a" * 32
    result, index, title = mirror_feed(SAMPLE, mid, "https://pod.example.com", 200, 10)
    root = ET.fromstring(result)
    channel = root.find("channel")
    assert title == "The Example Podcast"
    assert channel.findtext("title") == "The Example Podcast - Custom Outro"
    assert channel.findtext("description") == "My original description."
    assert channel.find(f"{{{ITUNES}}}image").get("href") == "https://cdn.example.com/cover.jpg"
    assert channel.find(f"{{{ITUNES}}}block").text == "Yes"
    assert channel.find(f"{{{ATOM}}}link").get("href") == f"https://pod.example.com/feed/{mid}.xml"
    episodes = channel.findall("item")
    assert len(episodes) == 2
    assert episodes[0].find("enclosure").get("url").startswith(f"https://pod.example.com/episode/{mid}/")
    assert episodes[0].find("enclosure").get("type") == "audio/mpeg"
    assert episodes[0].find("enclosure").get("length") == "1200"
    assert episodes[0].find(f"{{{ITUNES}}}duration").text == "0:01:40"
    assert episodes[0].find("guid").text.startswith(f"dynamic-outro:{mid}:")
    assert episodes[0].find("guid").get("isPermaLink") == "false"
    assert len(index["episodes"]) == 2


def test_known_cache_size_used_and_episode_id_stable():
    mid = "b" * 32
    result1, index, _ = mirror_feed(SAMPLE, mid, "https://pod.example.com", 200, 10)
    eid = next(iter(index["episodes"]))
    index["episodes"][eid]["cached_size"] = 12345
    result2, updated, _ = mirror_feed(SAMPLE, mid, "https://pod.example.com", 200, 10, index)
    root = ET.fromstring(result2)
    assert root.find("channel").findall("item")[0].find("enclosure").get("length") == "12345"
    assert updated["episodes"][eid]["cached_size"] == 12345


def test_private_enclosure_removed():
    raw = b'''<rss version="2.0"><channel><title>T</title><item><guid>a</guid><enclosure type="audio/mpeg" length="1" url="http://127.0.0.1/admin"/></item></channel></rss>'''
    with pytest.raises(InvalidFeed):
        mirror_feed(raw, "a" * 32, "https://service.example", 100, 2)


def test_invalid_or_non_rss_rejected():
    with pytest.raises(InvalidFeed):
        mirror_feed(b"<html></html>", "a" * 32, "https://service.example", 100, 1)
    with pytest.raises(Exception):
        mirror_feed(b"<!DOCTYPE a [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><rss>&e;</rss>",
                    "a" * 32, "https://service.example", 100, 1)


def test_feed_larger_than_two_mib_still_accepted():
    # Long-running shows with detailed notes can have large valid RSS documents.
    sample = SAMPLE.replace(b"My original description.", b"A" * (3 * 1024 * 1024))
    xml, index, title = mirror_feed(sample, "a" * 32, "https://pod.example.com", 100, 10)
    assert title == "The Example Podcast"
    assert len(index["episodes"]) == 2
    assert len(xml) > 2 * 1024 * 1024


def test_feed_over_sixteen_mib_is_rejected():
    sample = SAMPLE.replace(b"My original description.", b"A" * (16 * 1024 * 1024))
    with pytest.raises(InvalidFeed, match="16 MB"):
        mirror_feed(sample, "a" * 32, "https://pod.example.com", 100, 10)


def test_preserves_linked_rss_chapters_and_transcripts():
    from defusedxml import ElementTree as SafeET
    feed = b'''<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel><title>Linked resources</title><item><guid>1</guid>
<enclosure url="https://audio.example.org/test.mp3" type="audio/mpeg" length="1000" />
<podcast:chapters url="https://audio.example.org/chapters.json" type="application/json+chapters" />
<podcast:transcript url="https://audio.example.org/episode.vtt" type="text/vtt" language="en" />
</item></channel></rss>'''
    result, _, _ = mirror_feed(feed, "c" * 32, "https://mirror.example.org", 2000, 60)
    root = SafeET.fromstring(result)
    item = root.find("channel/item")
    ns = "{https://podcastindex.org/namespace/1.0}"
    assert item.find(f"{ns}chapters").get("url") == "https://audio.example.org/chapters.json"
    transcript = item.find(f"{ns}transcript")
    assert transcript.get("url") == "https://audio.example.org/episode.vtt"
    assert transcript.get("type") == "text/vtt"
    assert transcript.get("language") == "en"
