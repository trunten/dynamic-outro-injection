"""Runtime configuration. Never log credentials or persist them in Git."""
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    bucket: str
    endpoint: str
    key_id: str
    secret: str = field(repr=False)
    public_base_url: str
    max_mirrors: int
    max_outro_bytes: int
    max_source_bytes: int
    max_cache_bytes: int
    max_monthly_egress_bytes: int
    max_generations_per_day: int
    feed_refresh_seconds: int
    cache_ttl_days: int
    admin_password: str = field(default="", repr=False)

    @property
    def storage_ready(self) -> bool:
        return bool(self.bucket and self.endpoint and self.key_id and self.secret)


def load_settings() -> Settings:
    endpoint = os.getenv("B2_ENDPOINT", "").strip().rstrip("/")
    if endpoint:
        url = urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or not url.hostname.endswith(".backblazeb2.com"):
            raise RuntimeError("B2_ENDPOINT must be an HTTPS Backblaze S3 endpoint")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base_url and not public_base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise RuntimeError("PUBLIC_BASE_URL must use HTTPS (except localhost)")
    return Settings(
        bucket=os.getenv("B2_BUCKET", "").strip(),
        endpoint=endpoint,
        key_id=os.getenv("B2_KEY_ID", "").strip(),
        secret=os.getenv("B2_APPLICATION_KEY", "").strip(),
        public_base_url=public_base_url,
        max_mirrors=_positive_int("MAX_MIRRORS", 20),
        max_outro_bytes=_positive_int("MAX_OUTRO_BYTES", 8 * 1024 * 1024),
        max_source_bytes=_positive_int("MAX_SOURCE_BYTES", 150 * 1024 * 1024),
        max_cache_bytes=_positive_int("MAX_CACHE_BYTES", 2 * 1024 * 1024 * 1024),
        max_monthly_egress_bytes=_positive_int("MAX_MONTHLY_EGRESS_BYTES", 1024 * 1024 * 1024),
        max_generations_per_day=_positive_int("MAX_GENERATIONS_PER_DAY", 20),
        feed_refresh_seconds=_positive_int("FEED_REFRESH_SECONDS", 900, minimum=60),
        cache_ttl_days=_positive_int("CACHE_TTL_DAYS", 14),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
    )
