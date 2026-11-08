"""Batched, best-effort podcast activity counters in one private Backblaze object.

Counts are requests, *not* unique listeners or confirmed completed plays.
Using one combined file dramatically reduces B2 operations versus one write per
feed on every batch. A single Hostless replica is required for correctness.
"""
import asyncio
from datetime import datetime, timezone
import logging

from .storage import MissingObject

logger = logging.getLogger("dynamic_outro.activity")
ACTIVITY_KEY = "system/activity.json"
METRIC_NAMES = ("rss_requests", "audio_requests", "audio_bytes_requested", "generations")
LAST_NAMES = ("last_feed_at", "last_audio_at", "last_generation_at")


def combine_metrics(previous: dict | None, additions: dict | None) -> dict:
    a = previous or {}
    b = additions or {}
    combined = {key: max(0, int(a.get(key, 0))) + max(0, int(b.get(key, 0))) for key in METRIC_NAMES}
    for key in LAST_NAMES:
        combined[key] = max(str(a.get(key) or ""), str(b.get(key) or "")) or None
    return combined


class ActivityTracker:
    def __init__(self):
        self.pending: dict[str, dict] = {}
        self.flush_lock = asyncio.Lock()

    def record(self, mid: str, kind: str, bytes_requested: int = 0) -> None:
        if kind not in ("rss", "audio", "generation"):
            raise ValueError("Unknown activity type")
        now = datetime.now(timezone.utc).isoformat()
        rec = self.pending.setdefault(mid, {})
        if kind == "rss":
            rec["rss_requests"] = rec.get("rss_requests", 0) + 1
            rec["last_feed_at"] = now
        elif kind == "audio":
            rec["audio_requests"] = rec.get("audio_requests", 0) + 1
            rec["audio_bytes_requested"] = rec.get("audio_bytes_requested", 0) + max(0, bytes_requested)
            rec["last_audio_at"] = now
        else:
            rec["generations"] = rec.get("generations", 0) + 1
            rec["last_generation_at"] = now

    async def _stored(self, b2) -> dict:
        try:
            value = await asyncio.to_thread(b2.get_json, ACTIVITY_KEY)
        except MissingObject:
            return {}
        return value.get("feeds", {}) if isinstance(value, dict) else {}

    async def flush(self, b2, mid: str | None = None) -> None:
        # Flush all pending records together, regardless of the optional mid.
        # This is one B2 GET and one PUT rather than N writes per busy mirror.
        async with self.flush_lock:
            if not self.pending:
                return
            snapshot = self.pending
            self.pending = {}
            try:
                feeds = await self._stored(b2)
                for feed_id, delta in snapshot.items():
                    feeds[feed_id] = combine_metrics(feeds.get(feed_id), delta)
                await asyncio.to_thread(b2.put_json, ACTIVITY_KEY, {"feeds": feeds})
            except Exception:
                for feed_id, delta in snapshot.items():
                    self.pending[feed_id] = combine_metrics(delta, self.pending.get(feed_id))
                logger.warning("Could not persist podcast activity", exc_info=True)
                raise

    async def read_all(self, b2) -> dict:
        feeds = await self._stored(b2)
        return {mid: combine_metrics(feeds.get(mid), self.pending.get(mid))
                for mid in set(feeds) | set(self.pending)}

    async def read(self, b2, mid: str) -> dict:
        records = await self.read_all(b2)
        return records.get(mid, combine_metrics(None, None))

    async def purge(self, b2, mid: str) -> None:
        """Remove a deleted feed's live counters after they're archived."""
        async with self.flush_lock:
            feeds = await self._stored(b2)
            if mid in feeds:
                del feeds[mid]
                await asyncio.to_thread(b2.put_json, ACTIVITY_KEY, {"feeds": feeds})
            self.pending.pop(mid, None)
