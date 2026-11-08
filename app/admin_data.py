"""Private, aggregate-only report data for the administrator interface.

Never expose management hashes, source URL query strings or outro object keys.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
from urllib.parse import urlsplit

from .activity import combine_metrics
from .storage import MissingObject


def source_id(url: str) -> str:
    return hashlib.sha256((url or "").strip().encode()).hexdigest()[:24]


def source_host(url: str) -> str:
    return urlsplit(url or "").hostname or "unknown"


def _date(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value or "")
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def last_activity(stats: dict) -> str | None:
    return max(str(stats.get("last_feed_at") or ""), str(stats.get("last_audio_at") or "")) or None


async def overview(b2, tracker, max_mirrors: int) -> dict:
    # One account, one Hostless replica. Flush queued activity before taking a
    # consistent snapshot. A brief restart can lose at most the unflushed window.
    await tracker.flush(b2)
    all_activity = await tracker.read_all(b2)
    active_keys = sorted(
        item["Key"] for item in await asyncio.to_thread(b2.list_current, "mirrors/")
        if item["Key"].endswith("/meta.json")
    )
    deleted_keys = sorted(
        item["Key"] for item in await asyncio.to_thread(b2.list_current, "system/deleted/")
        if item["Key"].endswith(".json")
    )

    async def read_active(key):
        mid = key.split("/")[1]
        meta = await asyncio.to_thread(b2.get_json, key)
        try:
            episodes = await asyncio.to_thread(b2.get_json, f"mirrors/{mid}/episodes.json")
        except MissingObject:
            episodes = {}
        stats = all_activity.get(mid, combine_metrics(None, None))
        values = (episodes.get("episodes") or {}).values()
        return {
            "id": mid,
            "title": str(meta.get("title", "Untitled"))[:160],
            "source_id": source_id(meta.get("feed_url", "")),
            "source_host": source_host(meta.get("feed_url", "")),
            "created_at": meta.get("created_at"),
            "outro_choice": meta.get("outro_choice", "custom"),
            "episode_count": len(episodes.get("episodes") or {}),
            "generated_episode_count": sum(1 for x in values if x.get("cached_size", 0) > 0),
            "activity": stats,
            "last_activity_at": last_activity(stats),
        }

    async def read_deleted(key):
        return await asyncio.to_thread(b2.get_json, key)

    # The public beta caps active mirrors at 20. Bound IO concurrency on the
    # admin page and avoid potentially hundreds of simultaneous B2 requests.
    semaphore = asyncio.Semaphore(5)

    async def bounded(fn, item):
        async with semaphore:
            return await fn(item)

    live = await asyncio.gather(*(bounded(read_active, k) for k in active_keys))
    archive = await asyncio.gather(*(bounded(read_deleted, k) for k in deleted_keys))
    active_ids = {item["id"] for item in live}
    deleted = []
    pending = []
    for item in archive:
        if item.get("status") == "deleted" or (item.get("status") == "pending" and item.get("id") not in active_ids):
            # Include a history entry when deletion completed but a final
            # archive-status update was interrupted.
            deleted.append(item)
        elif item.get("status") == "pending":
            pending.append(item)

    groups: dict[str, dict] = defaultdict(dict)
    for status, records in (("active", live), ("deleted", deleted)):
        for item in records:
            sid = item.get("source_id") or source_id(item.get("title", ""))
            if sid not in groups:
                groups[sid] = {
                    "title": item.get("title", "Untitled"),
                    "source_host": item.get("source_host", "unknown"),
                    "active": 0, "deleted": 0, "total": 0,
                    "custom_outros": 0, "default_outros": 0,
                }
            group = groups[sid]
            group[status] += 1
            group["total"] += 1
            group["custom_outros" if item.get("outro_choice") == "custom" else "default_outros"] += 1

    today = datetime.now(timezone.utc)
    recent_threshold = today - timedelta(days=30)
    active_recently = sum(1 for item in live if
                          (when := _date(item.get("last_activity_at"))) is not None and when >= recent_threshold)
    month = today.strftime("%Y-%m")
    try:
        egress = await asyncio.to_thread(b2.get_json, f"system/egress-{month}.json")
        monthly_bandwidth = max(0, int(egress.get("bytes", 0)))
    except MissingObject:
        monthly_bandwidth = 0
    summary = {
        "active_feeds": len(live),
        "deleted_feeds": len(deleted),
        "pending_deletions": len(pending),
        "active_last_30_days": active_recently,
        "rss_requests": sum(item.get("activity", {}).get("rss_requests", 0) for item in live + deleted),
        "audio_requests": sum(item.get("activity", {}).get("audio_requests", 0) for item in live + deleted),
        "monthly_bandwidth_bytes": monthly_bandwidth,
        "max_mirrors": max_mirrors,
    }
    return {
        "summary": summary,
        "shows": sorted(groups.values(), key=lambda x: (-x["total"], x["title"].casefold())),
        "active": sorted(live, key=lambda x: x.get("created_at") or "", reverse=True),
        "deleted": sorted(deleted, key=lambda x: x.get("deleted_at") or "", reverse=True),
        "pending": pending,
        "tracking_note": "Deleted-feed history and activity tracking start with this admin update; older deletions cannot be reconstructed. Audio requests include downloads and podcast-app prefetches, not confirmed listens. Counts may omit activity immediately before an abrupt restart.",
    }
