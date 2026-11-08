"""Safe RSS mirror rewriting. Episode metadata is never downloaded here."""
import hashlib
from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET
from urllib.parse import urlsplit

from .security import validate_source_url, UnsafeSource

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"

for prefix, uri in {
    "itunes": ITUNES,
    "atom": ATOM,
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "podcast": "https://podcastindex.org/namespace/1.0",
}.items():
    ET.register_namespace(prefix, uri)


class InvalidFeed(ValueError):
    pass


def _length(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0


def _duration_seconds(text: str) -> int | None:
    text = text.strip()
    try:
        parts = [int(x) for x in text.split(":")]
        if len(parts) > 3 or not parts or any(x < 0 for x in parts):
            return None
        result = 0
        for part in parts:
            result = result * 60 + part
        return result
    except ValueError:
        return None


def _duration_display(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def mirror_feed(raw: bytes, mirror_id: str, base_url: str, outro_bytes: int,
                outro_seconds: int, previous: dict | None = None) -> tuple[bytes, dict, str]:
    if len(raw) > 16 * 1024 * 1024:
        raise InvalidFeed("RSS feed exceeds 16 MB")
    try:
        root = SafeET.fromstring(raw)
    except (ET.ParseError, ValueError) as exc:
        raise InvalidFeed("Not a valid RSS XML document") from exc
    if root.tag != "rss":
        raise InvalidFeed("Only RSS 2.0 podcast feeds are supported")
    channel = root.find("channel")
    if channel is None:
        raise InvalidFeed("RSS feed is missing its channel")
    title = (channel.findtext("title") or "Untitled podcast").strip()[:160]
    items = channel.findall("item")
    if len(items) > 1000:
        raise InvalidFeed("Feeds with more than 1,000 episodes are not supported")
    title_node = channel.find("title")
    if title_node is not None:
        title_node.text = f"{title} - Custom Outro"
    self_url = f"{base_url}/feed/{mirror_id}.xml"
    for link in channel.findall(f"{{{ATOM}}}link"):
        if link.get("rel") == "self":
            link.set("href", self_url)
    itunes_block = channel.find(f"{{{ITUNES}}}block")
    if itunes_block is None:
        itunes_block = ET.SubElement(channel, f"{{{ITUNES}}}block")
    itunes_block.text = "Yes"  # Keeps personal mirrors out of podcast search directories.

    old_episodes = (previous or {}).get("episodes", {})
    episodes = dict(old_episodes)
    eligible = 0
    for item in items:
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        origin_url = enclosure.get("url", "").strip()
        mime = enclosure.get("type", "").strip().lower()
        if mime and not (mime.startswith("audio/") or mime == "application/octet-stream"):
            continue
        try:
            validate_source_url(origin_url)
        except UnsafeSource:
            # Do not publish remote pointers to unsafe targets.
            channel.remove(item)
            continue
        raw_guid = (item.findtext("guid") or origin_url).strip()
        eid = hashlib.sha256((raw_guid + "\n" + origin_url).encode()).hexdigest()[:24]
        old = episodes.get(eid, {})
        cached_size = max(0, int(old.get("cached_size", 0)))
        source_size = _length(enclosure.get("length"))
        episodes[eid] = {
            "url": origin_url,
            "source_size": source_size,
            "cached_size": cached_size,
            "mime": mime,
        }
        enclosure.set("url", f"{base_url}/episode/{mirror_id}/{eid}.mp3")
        enclosure.set("type", "audio/mpeg")
        # Estimated until audio is generated. Podcast clients are tested separately.
        enclosure.set("length", str(cached_size or max(1, source_size + outro_bytes)))
        guid = item.find("guid")
        if guid is None:
            guid = ET.SubElement(item, "guid")
        guid.text = f"dynamic-outro:{mirror_id}:{eid}"
        guid.set("isPermaLink", "false")
        duration = item.find(f"{{{ITUNES}}}duration")
        if duration is not None and duration.text:
            seconds = _duration_seconds(duration.text)
            if seconds is not None:
                duration.text = _duration_display(seconds + outro_seconds)
        eligible += 1

    if eligible == 0:
        raise InvalidFeed("No supported public audio enclosures were found")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), {"episodes": episodes}, title
