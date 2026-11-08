"""Bounded outbound HTTP fetching with validated DNS at socket connection time.

Do not replace this resolver with a preflight-only hostname check: DNS can change
between a preflight lookup and the actual HTTP connection (DNS rebinding).
"""
from contextlib import asynccontextmanager
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL


class UnsafeSource(ValueError):
    pass


def validate_source_url(url: str) -> str:
    if not url or len(url) > 2048:
        raise UnsafeSource("A valid public source URL is required")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port  # Raises ValueError for malformed ports.
    except ValueError as exc:
        raise UnsafeSource("Malformed source URL") from exc
    if parsed.scheme not in ("https", "http") or not host:
        raise UnsafeSource("Only public HTTP(S) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeSource("Password-protected source URLs are not supported")
    if port not in (None, 80, 443):
        raise UnsafeSource("Custom source ports are not supported")
    hostname = host.rstrip(".").lower()
    if hostname in ("localhost",) or hostname.endswith((".localhost", ".local", ".internal", ".test", ".onion")):
        raise UnsafeSource("Local network addresses are not supported")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not ip.is_global:
            raise UnsafeSource("Private or reserved IP addresses are not supported")
    return url


class PublicDNSResolver(AbstractResolver):
    """Validate the actual A/AAAA result used by the HTTP client."""

    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        results = await self._resolver.resolve(host, port, family=family)
        if not results or any(not ipaddress.ip_address(entry["host"]).is_global for entry in results):
            raise UnsafeSource("Source resolves to a non-public network address")
        return results

    async def close(self):
        await self._resolver.close()


def new_session(total_timeout: int = 120) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicDNSResolver(), use_dns_cache=False, limit=4),
        timeout=aiohttp.ClientTimeout(total=total_timeout, connect=12, sock_read=35),
        trust_env=False,
        headers={"User-Agent": "DynamicOutroInjection/0.1 (+podcast RSS mirror)", "Accept-Encoding": "identity"},
        auto_decompress=False,
    )


@asynccontextmanager
async def open_public_url(session: aiohttp.ClientSession, url: str):
    current = validate_source_url(url)
    for _ in range(6):
        # Signed CDN redirect URLs are byte-sensitive: ordinary aiohttp URL
        # canonicalisation changes audio%2Fmpeg into audio/mpeg and can
        # invalidate the signature. Keep original escapes unchanged.
        # Public IP validation still runs on every redirect hop.
        async with session.get(URL(current, encoded=True), allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                target = response.headers.get("Location")
                if not target:
                    raise UnsafeSource("Source redirect is missing its destination")
                current = validate_source_url(urljoin(current, target))
                continue
            response.raise_for_status()
            yield response
            return
    raise UnsafeSource("Too many redirects")


async def fetch_feed(url: str, max_bytes: int = 16 * 1024 * 1024) -> bytes:
    async with new_session(35) as session:
        async with open_public_url(session, url) as response:
            if int(response.headers.get("Content-Length", "0")) > max_bytes:
                raise ValueError("RSS feed is too large")
            output = bytearray()
            async for piece in response.content.iter_chunked(65536):
                output.extend(piece)
                if len(output) > max_bytes:
                    raise ValueError("RSS feed is too large")
            return bytes(output)


async def download_episode(url: str, path: str, max_bytes: int) -> int:
    async with new_session(360) as session:
        async with open_public_url(session, url) as response:
            if int(response.headers.get("Content-Length", "0")) > max_bytes:
                raise ValueError("Source episode exceeds the allowed size")
            count = 0
            with open(path, "wb") as out:
                async for piece in response.content.iter_chunked(131072):
                    count += len(piece)
                    if count > max_bytes:
                        raise ValueError("Source episode exceeds the allowed size")
                    out.write(piece)
            if count < 1024:
                raise ValueError("Source episode was empty or too small")
            return count
