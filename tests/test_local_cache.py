"""Local MP3 cache safety tests; no B2 or network needed."""
from app.local_cache import LocalAudioCache

MID = "a" * 32
E1 = "1" * 24
E2 = "2" * 24


def test_seed_and_range(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 16)
    src = tmp_path / "original"
    src.write_bytes(b"abcdefghijkl")
    assert cache.seed(MID, E1, str(src), 12)
    handle, size = cache.open(MID, E1)
    assert size == 12
    assert b"".join(cache.stream_open(handle, "bytes=4-7")) == b"efgh"
    assert src.read_bytes() == b"abcdefghijkl"


def test_incomplete_or_interrupted_stream_never_cached(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 16)
    assert b"".join(cache.tee(MID, E1, iter([b"abc"]), 6)) == b"abc"
    assert cache.open(MID, E1) is None
    stream = cache.tee(MID, E1, iter([b"abc", b"def"]), 6)
    assert next(stream) == b"abc"
    stream.close()
    assert cache.open(MID, E1) is None
    assert not list((tmp_path / "cache").glob("*/*.part"))


def test_complete_stream_cached_atomically(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 16)
    stream = cache.tee(MID, E1, iter([b"abc", b"def"]), 6)
    assert next(stream) == b"abc"
    assert cache.open(MID, E1) is None
    assert b"".join(stream) == b"def"
    handle, size = cache.open(MID, E1)
    assert size == 6
    assert b"".join(cache.stream_open(handle, None)) == b"abcdef"


def test_concurrent_fills_do_not_overwrite(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 16)
    first = cache.tee(MID, E1, iter([b"abc", b"def"]), 6)
    assert next(first) == b"abc"
    second = cache.tee(MID, E1, iter([b"abc", b"def"]), 6)
    assert b"".join(second) == b"abcdef"
    assert cache.open(MID, E1) is None
    assert b"".join(first) == b"def"
    assert cache.open(MID, E1) is not None


def test_eviction_preserves_open_handle(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 10)
    src = tmp_path / "original"
    src.write_bytes(b"aaaaaa")
    assert cache.seed(MID, E1, str(src), 6)
    retained, _ = cache.open(MID, E1)
    src.write_bytes(b"bbbbbb")
    assert cache.seed(MID, E2, str(src), 6)
    assert cache.open(MID, E1) is None
    assert b"".join(cache.stream_open(retained, None)) == b"aaaaaa"


def test_deletion_invalidates_inflight_fill(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 16)
    stream = cache.tee(MID, E1, iter([b"abc", b"def"]), 6)
    assert next(stream) == b"abc"
    cache.invalidate_mirror(MID)
    assert b"".join(stream) == b"def"
    assert cache.open(MID, E1) is None
    assert not list((tmp_path / "cache").glob("*/*.mp3"))


def test_disabled_cache_degrades_to_passthrough(tmp_path):
    cache = LocalAudioCache(str(tmp_path / "cache"), 0)
    assert b"".join(cache.tee(MID, E1, iter([b"abc"]), 3)) == b"abc"
    assert cache.open(MID, E1) is None
