"""Public, on-demand RSS podcast mirror with private Backblaze B2 storage.

Small single-instance beta. The public service must be protected by quotas and
ideally an edge rate limiter before it is promoted beyond limited testing.
"""
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from botocore.exceptions import ClientError

from .audio import AudioError, append_outro, probe_audio
from .config import load_settings
from .rss import InvalidFeed, mirror_feed
from .security import UnsafeSource, download_episode, fetch_feed, validate_source_url
from .storage import MissingObject, Storage

logger = logging.getLogger("dynamic_outro")
logging.basicConfig(level=logging.INFO)
settings = load_settings()
app = FastAPI(title="Dynamic Outro Injection", docs_url=None, redoc_url=None)

MIRROR_RE = re.compile(r"^[a-f0-9]{32}$")
EPISODE_RE = re.compile(r"^[a-f0-9]{24}$")
mirror_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
episode_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
generation_gate = asyncio.Semaphore(1)
egress_gate = asyncio.Lock()
recent_feed_checks: dict[str, float] = {}
creation_times: dict[str, deque] = defaultdict(deque)
generations_today: dict[str, int] = defaultdict(int)
storage_instance: Storage | None = None

# Shared, administrator-provisioned audio. Kept private in B2 (not the public Git repo).
DEFAULT_OUTRO_KEY = "atp.mp3"
DEFAULT_OUTRO_LABEL = "Default (ATP End Theme)"


def storage() -> Storage:
    global storage_instance
    if not settings.storage_ready:
        raise HTTPException(status_code=503, detail="Storage is not configured yet")
    if storage_instance is None:
        storage_instance = Storage(settings)
    return storage_instance


async def s3(method, *args, **kwargs):
    return await asyncio.to_thread(method, *args, **kwargs)


def _id(value: str, pattern: re.Pattern):
    if not pattern.fullmatch(value):
        raise HTTPException(status_code=404, detail="Not found")
    return value


def _prefix(mid: str) -> str:
    return f"mirrors/{mid}/"


def _meta_key(mid: str) -> str:
    return _prefix(mid) + "meta.json"


def _base_url(request: Request) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    # Production deployments should configure PUBLIC_BASE_URL to prevent Host
    # header confusion. Fallback is useful before Hostless assigns a hostname.
    host = request.url.netloc
    if not host:
        raise HTTPException(400, "Could not determine the public hostname")
    proto = "http" if request.url.hostname in ("localhost", "127.0.0.1") else "https"
    return f"{proto}://{host}"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (InvalidFeed, UnsafeSource, AudioError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, MissingObject):
        return HTTPException(status_code=404, detail="Mirror or episode not found")
    logger.exception("Processing failed: %s", type(exc).__name__)
    return HTTPException(status_code=502, detail="Could not complete the upstream or storage operation")


async def _meta(mid: str) -> dict:
    try:
        return await s3(storage().get_json, _meta_key(mid))
    except MissingObject as exc:
        raise HTTPException(404, "Mirror does not exist") from exc


async def _index(mid: str) -> dict:
    try:
        return await s3(storage().get_json, _prefix(mid) + "episodes.json")
    except MissingObject as exc:
        raise HTTPException(404, "Episodes are unavailable") from exc


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html")


@app.get("/health")
async def health():
    return {"ok": True, "storage_configured": settings.storage_ready}


@app.get("/api/default-outro")
async def default_outro_status():
    """Tell the UI whether the privately stored shared outro is ready to use."""
    if not settings.storage_ready:
        return {"label": DEFAULT_OUTRO_LABEL, "available": False}
    try:
        record = await s3(storage().head, DEFAULT_OUTRO_KEY)
    except Exception as exc:
        raise _http_error(exc) from exc
    available = bool(record and 1024 <= int(record["ContentLength"]) <= settings.max_outro_bytes)
    return {"label": DEFAULT_OUTRO_LABEL, "available": available}


def _limit_creation(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = creation_times[ip]
    while attempts and attempts[0] < now - 3600:
        attempts.popleft()
    if len(attempts) >= 6:
        raise HTTPException(429, "Too many mirrors created from this connection in the last hour")
    attempts.append(now)


@app.post("/api/mirrors", status_code=201)
async def create_mirror(request: Request):
    """Create only small metadata files plus the outro. No episodes are fetched."""
    content_length = int(request.headers.get("content-length", "0") or "0")
    if content_length > settings.max_outro_bytes + 1_000_000:
        raise HTTPException(413, "Uploaded file is too large")
    _limit_creation(request)
    try:
        form = await request.form(max_files=1, max_fields=4)
    except Exception as exc:
        raise HTTPException(400, "Invalid upload form") from exc
    feed_url = str(form.get("feed_url", "")).strip()
    agreed = str(form.get("permission", "")).lower() in ("on", "yes", "true", "1")
    choice = str(form.get("outro_choice", "custom")).strip().lower()
    outro = form.get("outro")
    if not agreed:
        raise HTTPException(422, "Please confirm that you have permission to modify and host the audio")
    if choice not in ("default", "custom"):
        raise HTTPException(422, "Select the default outro or upload your own MP3")
    if choice == "custom":
        if not outro or not hasattr(outro, "read"):
            raise HTTPException(422, "Choose an MP3 outro file")
        if not (getattr(outro, "filename", "") or "").lower().endswith(".mp3"):
            raise HTTPException(422, "The outro must be an MP3 file")
    try:
        validate_source_url(feed_url)
    except UnsafeSource as exc:
        raise HTTPException(422, str(exc)) from exc

    b2 = storage()
    existing = await s3(b2.list_current, "mirrors/")
    if sum(entry["Key"].endswith("/meta.json") for entry in existing) >= settings.max_mirrors:
        raise HTTPException(503, "This beta has reached its mirror limit")

    with tempfile.TemporaryDirectory(prefix="outro-", dir="/tmp") as folder:
        outro_path = os.path.join(folder, "outro.mp3")
        if choice == "default":
            try:
                record = await s3(b2.head, DEFAULT_OUTRO_KEY)
                if not record:
                    raise HTTPException(503, "Default outro is not configured yet; please upload your own MP3")
                size = int(record["ContentLength"])
                if not 1024 <= size <= settings.max_outro_bytes:
                    raise HTTPException(503, "Default outro is not a valid size")
                await s3(b2.download_file, DEFAULT_OUTRO_KEY, outro_path)
            except HTTPException:
                raise
            except Exception as exc:
                raise _http_error(exc) from exc
        else:
            size = 0
            with open(outro_path, "wb") as file:
                while chunk := await outro.read(64 * 1024):
                    size += len(chunk)
                    if size > settings.max_outro_bytes:
                        raise HTTPException(413, "Outro exceeds the 8 MB limit")
                    file.write(chunk)
            if size < 1024:
                raise HTTPException(422, "Outro file is empty")
        try:
            info = await asyncio.to_thread(probe_audio, outro_path)
            if info["codec_name"] != "mp3":
                raise AudioError("The outro must contain MP3 audio")
            if info["duration"] > 600:
                raise AudioError("Outros must be no longer than 10 minutes")
            raw = await fetch_feed(feed_url)
            mid = uuid.uuid4().hex
            base = _base_url(request)
            rendered, index, title = mirror_feed(raw, mid, base, size, round(info["duration"]))
            token = secrets.token_urlsafe(32)
            prefix = _prefix(mid)
            meta = {
                "feed_url": feed_url,
                "title": title,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "outro_bytes": size,
                "outro_seconds": round(info["duration"]),
                "outro_key": DEFAULT_OUTRO_KEY if choice == "default" else prefix + "outro.mp3",
                "outro_choice": choice,
                "base_url": base,
                "manage_hash": hashlib.sha256(token.encode()).hexdigest(),
            }
            if choice == "custom":
                await s3(b2.upload_file, prefix + "outro.mp3", outro_path)
            await s3(b2.put_bytes, prefix + "original.xml", raw, "application/xml")
            await s3(b2.put_json, prefix + "episodes.json", index)
            await s3(b2.put_bytes, prefix + "feed.xml", rendered, "application/rss+xml")
            await s3(b2.put_json, prefix + "meta.json", meta)
            recent_feed_checks[mid] = time.monotonic()
            return {
                "id": mid, "title": title,
                "episodes": sum(1 for _ in index["episodes"]),
                "feed_url": f"{base}/feed/{mid}.xml",
                "manage_token": token,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _http_error(exc) from exc


async def _refresh_feed(mid: str) -> bytes:
    prefix = _prefix(mid)
    b2 = storage()
    async with mirror_locks[mid]:
        cached = await s3(b2.get_bytes, prefix + "feed.xml", 2 * 1024 * 1024 + 512 * 1024)
        if time.monotonic() - recent_feed_checks.get(mid, 0) < settings.feed_refresh_seconds:
            return cached
        meta = await _meta(mid)
        try:
            raw = await fetch_feed(meta["feed_url"])
            raw_hash = hashlib.sha256(raw).hexdigest()
            base = settings.public_base_url or meta["base_url"]
            if raw_hash != meta["original_sha256"] or base != meta["base_url"]:
                prior = await _index(mid)
                rendered, index, title = mirror_feed(raw, mid, base,
                                                     meta["outro_bytes"], meta["outro_seconds"], prior)
                await s3(b2.put_bytes, prefix + "original.xml", raw, "application/xml")
                await s3(b2.put_json, prefix + "episodes.json", index)
                await s3(b2.put_bytes, prefix + "feed.xml", rendered, "application/rss+xml")
                meta.update(original_sha256=raw_hash, base_url=base, title=title)
                await s3(b2.put_json, prefix + "meta.json", meta)
                cached = rendered
            recent_feed_checks[mid] = time.monotonic()
        except Exception as exc:
            # A temporary outage of the source feed must not break subscriptions.
            logger.warning("RSS refresh failed for %s: %s", mid, type(exc).__name__)
            recent_feed_checks[mid] = time.monotonic()
        return cached


@app.get("/feed/{mid}.xml")
async def get_mirror_feed(mid: str):
    _id(mid, MIRROR_RE)
    try:
        result = await _refresh_feed(mid)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _http_error(exc) from exc
    return Response(content=result, media_type="application/rss+xml",
                    headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/mirrors/{mid}")
async def mirror_status(mid: str):
    _id(mid, MIRROR_RE)
    meta = await _meta(mid)
    index = await _index(mid)
    return {"id": mid, "title": meta["title"], "created_at": meta["created_at"],
            "episodes": len(index["episodes"]),
            "processed": sum(1 for x in index["episodes"].values() if x.get("cached_size", 0) > 0)}


async def _ensure_generated(mid: str, eid: str):
    b2 = storage()
    output_key = f"cache/{mid}/{eid}.mp3"
    found = await s3(b2.head, output_key)
    if found is not None:
        return output_key, found

    async with episode_locks[f"{mid}:{eid}"]:
        found = await s3(b2.head, output_key)
        if found is not None:
            return output_key, found
        async with generation_gate:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if generations_today[day] >= settings.max_generations_per_day:
                raise HTTPException(429, "Daily audio generation limit reached. Try again tomorrow")
            meta = await _meta(mid)
            index = await _index(mid)
            episode = index["episodes"].get(eid)
            if episode is None:
                raise HTTPException(404, "Episode not found in the mirrored feed")
            generations_today[day] += 1
            with tempfile.TemporaryDirectory(prefix="episode-", dir="/tmp") as directory:
                src_path = os.path.join(directory, "original.audio")
                outro_path = os.path.join(directory, "outro.mp3")
                output_path = os.path.join(directory, "finished.mp3")
                try:
                    await download_episode(episode["url"], src_path, settings.max_source_bytes)
                    source_details = await asyncio.to_thread(probe_audio, src_path)
                    if source_details["duration"] > 6 * 3600:
                        raise AudioError("Episodes longer than six hours are not supported")
                    # Pre-existing mirrors have no outro_key: keep their original custom file.
                    outro_key = meta.get("outro_key") or _prefix(mid) + "outro.mp3"
                    await s3(b2.download_file, outro_key, outro_path)
                    method = await append_outro(src_path, outro_path, output_path)
                    output_size = os.path.getsize(output_path)
                    if not output_size:
                        raise AudioError("FFmpeg produced an empty audio file")
                    if output_size > settings.max_cache_bytes:
                        raise AudioError("Processed episode is too large for this cache")
                    # Remove old audio before storing the new file. Versions are
                    # permanently removed, not merely marked deleted.
                    await s3(b2.expire_cache, "cache/", settings.cache_ttl_days,
                             settings.max_cache_bytes, output_size)
                    await s3(b2.upload_file, output_key, output_path)
                    logger.info("Generated %s/%s via %s (%d bytes)", mid, eid, method, output_size)
                except Exception as exc:
                    if isinstance(exc, HTTPException):
                        raise
                    raise _http_error(exc) from exc
            found = await s3(b2.head, output_key)
            # Cache now works even if feed metadata update is interrupted.
            try:
                index["episodes"][eid]["cached_size"] = output_size
                raw = await s3(b2.get_bytes, _prefix(mid) + "original.xml", 2 * 1024 * 1024 + 1024)
                rendered, index, _ = mirror_feed(raw, mid,
                    settings.public_base_url or meta["base_url"], meta["outro_bytes"],
                    meta["outro_seconds"], index)
                await s3(b2.put_json, _prefix(mid) + "episodes.json", index)
                await s3(b2.put_bytes, _prefix(mid) + "feed.xml", rendered, "application/rss+xml")
            except Exception:
                logger.exception("Generated episode saved, but failed to update RSS length")
            if found is None:
                raise HTTPException(502, "The processed episode was not saved correctly")
            return output_key, found


def _range_header(header: str | None, total: int) -> tuple[str | None, int, dict]:
    base = {"Accept-Ranges": "bytes", "Content-Type": "audio/mpeg",
            "Cache-Control": "public, max-age=3600"}
    if not header:
        return None, 200, {**base, "Content-Length": str(total)}
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()):
        raise HTTPException(416, "Unsupported byte range", headers={"Content-Range": f"bytes */{total}"})
    begin_text, end_text = match.groups()
    if not begin_text:
        length = int(end_text)
        if not length:
            raise HTTPException(416, "Invalid range", headers={"Content-Range": f"bytes */{total}"})
        start = max(0, total - length)
        end = total - 1
    else:
        start = int(begin_text)
        end = min(int(end_text), total - 1) if end_text else total - 1
    if start >= total or start > end:
        raise HTTPException(416, "Range not satisfiable", headers={"Content-Range": f"bytes */{total}"})
    return f"bytes={start}-{end}", 206, {
        **base, "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(end - start + 1),
    }


async def _reserve_monthly_egress(amount: int) -> None:
    """Persist a conservative approximate MP3 egress budget across restarts.

    One Hostless instance is required: the S3 read/modify/write is not an atomic
    distributed counter. It limits application traffic, not provider charges.
    """
    if amount <= 0:
        return
    key = "system/egress-" + datetime.now(timezone.utc).strftime("%Y-%m") + ".json"
    async with egress_gate:
        b2 = storage()
        try:
            record = await s3(b2.get_json, key)
        except MissingObject:
            record = {"bytes": 0}
        used = int(record.get("bytes", 0))
        if used + amount > settings.max_monthly_egress_bytes:
            raise HTTPException(429, "The monthly listening bandwidth limit has been reached")
        await s3(b2.put_json, key, {"bytes": used + amount})


@app.api_route("/episode/{mid}/{eid}.mp3", methods=["GET", "HEAD"])
async def episode_audio(mid: str, eid: str, request: Request):
    _id(mid, MIRROR_RE)
    _id(eid, EPISODE_RE)
    try:
        key, object_info = await _ensure_generated(mid, eid)
        total = int(object_info["ContentLength"])
        byte_range, status, headers = _range_header(request.headers.get("range"), total)
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)
        await _reserve_monthly_egress(int(headers["Content-Length"]))
        return StreamingResponse(storage().stream(key, byte_range), status_code=status,
                                 media_type="audio/mpeg", headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@app.delete("/api/mirrors/{mid}")
async def delete_mirror(mid: str, request: Request):
    _id(mid, MIRROR_RE)
    meta = await _meta(mid)
    provided = request.headers.get("x-manage-token", "")
    hashed = hashlib.sha256(provided.encode()).hexdigest()
    if not provided or not hmac.compare_digest(hashed, meta["manage_hash"]):
        raise HTTPException(403, "Invalid management token")
    b2 = storage()
    try:
        for prefix in (_prefix(mid), f"cache/{mid}/"):
            entries = await s3(b2.list_current, prefix)
            # Delete metadata last. Backblaze file versions are permanent-deleted.
            for entry in sorted(entries, key=lambda e: e["Key"] == _meta_key(mid)):
                await s3(b2.delete_versions, entry["Key"])
    except Exception as exc:
        raise _http_error(exc) from exc
    recent_feed_checks.pop(mid, None)
    return {"deleted": True}
