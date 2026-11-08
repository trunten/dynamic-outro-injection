import pytest
from app.security import validate_source_url, UnsafeSource
from app.main import _range_header
from fastapi import HTTPException

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/feed", "http://10.0.0.2/feed", "http://localhost/rss",
    "http://169.254.169.254/latest/meta-data/", "http://192.168.1.1",
    "http://[::1]/", "http://service.local/feed", "file:///etc/passwd",
    "http://username:password@example.com", "http://example.com:8080/rss",
])
def test_block_unsafe_urls(url):
    with pytest.raises(UnsafeSource):
        validate_source_url(url)

@pytest.mark.parametrize("url", [
    "https://www.example.org/podcasts/rss.xml", "http://www.example.org/abc",
    "https://public.example.com:443/podcast.xml",
])
def test_accept_public_urls(url):
    assert validate_source_url(url) == url


def test_single_range():
    spec, status, headers = _range_header("bytes=100-199", 1000)
    assert (spec, status, headers["Content-Length"], headers["Content-Range"]) == (
        "bytes=100-199", 206, "100", "bytes 100-199/1000")
    spec, status, headers = _range_header("bytes=-20", 1000)
    assert spec == "bytes=980-999"
    spec, status, headers = _range_header("bytes=20-", 1000)
    assert spec == "bytes=20-999"
    spec, status, headers = _range_header(None, 1000)
    assert status == 200 and headers["Content-Length"] == "1000"


@pytest.mark.parametrize("spec", ["bytes=1000-", "bytes=60-40", "bytes=0-1,3-5", "bytes=-0"])
def test_invalid_range(spec):
    with pytest.raises(HTTPException) as exc:
        _range_header(spec, 1000)
    assert exc.value.status_code == 416


def test_dns_resolver_blocks_private_ip():
    # Resolver checks the actual answer given to aiohttp, not only a preflight.
    import asyncio
    from app.security import PublicDNSResolver

    class FakeResolver:
        async def resolve(self, host, port, family):
            return [{"host": "10.0.0.1", "hostname": host, "port": port,
                     "family": family, "proto": 0, "flags": 0}]
        async def close(self):
            return None

    async def run():
        resolver = PublicDNSResolver()
        resolver._resolver = FakeResolver()
        with pytest.raises(UnsafeSource):
            await resolver.resolve("fake.example", 80)
        await resolver.close()

    asyncio.run(run())
