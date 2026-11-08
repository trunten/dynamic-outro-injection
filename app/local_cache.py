"""Best-effort, bounded, ephemeral cache for fully generated podcast MP3s.

Backblaze is authoritative. This cache never publishes partially written files,
never blocks playback when the cache filesystem is unavailable, and is safe for
one Hostless/Uvicorn worker with concurrent streaming threads.
"""
import logging
import os
from pathlib import Path
import shutil
import threading
import uuid

logger = logging.getLogger('dynamic_outro.local_cache')


class LocalAudioCache:
    def __init__(self, root: str = '/app/.cache/episodes', max_bytes: int = 1536 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.pending: dict[tuple[str, str], int] = {}
        self.invalidated: set[str] = set()
        self.enabled = max_bytes > 0
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                # These are never valid cache entries. Clean up after a local crash.
                for partial in self.root.glob('*/*.part'):
                    partial.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning('Local audio cache disabled: %s', exc)
                self.enabled = False

    def _file(self, mid: str, eid: str) -> Path:
        return self.root / mid / (eid + '.mp3')

    def open(self, mid: str, eid: str):
        """Return an already-open file and its size, or None. Caller owns the fd."""
        if not self.enabled:
            return None
        with self.lock:
            if mid in self.invalidated:
                return None
            path = self._file(mid, eid)
            try:
                handle = path.open('rb')
                size = os.fstat(handle.fileno()).st_size
                if not 0 < size <= self.max_bytes:
                    handle.close()
                    return None
                try:
                    os.utime(path, None)  # Least-recently-accessed eviction.
                except OSError:
                    pass  # The already-open handle can still serve the file.
                return handle, size
            except OSError:
                return None

    @staticmethod
    def stream_open(handle, byte_range: str | None):
        """Stream only the requested bytes; an open fd survives LRU unlink."""
        try:
            remaining = None
            if byte_range:
                start_text, end_text = byte_range.removeprefix('bytes=').split('-', 1)
                start, end = int(start_text), int(end_text)
                handle.seek(start)
                remaining = end - start + 1
            while remaining is None or remaining > 0:
                data = handle.read(min(128 * 1024, remaining) if remaining is not None else 128 * 1024)
                if not data:
                    break
                yield data
                if remaining is not None:
                    remaining -= len(data)
        finally:
            handle.close()

    def _stored(self):
        entries = []
        for path in self.root.glob('*/*.mp3'):
            try:
                st = path.stat()
                entries.append((st.st_mtime_ns, st.st_size, path))
            except OSError:
                continue
        return entries

    def _prepare(self, mid: str, eid: str, size: int):
        if not self.enabled or size <= 0 or size > self.max_bytes:
            return None
        with self.lock:
            key = (mid, eid)
            if mid in self.invalidated or key in self.pending or self._file(mid, eid).exists():
                return None
            try:
                folder = self.root / mid
                folder.mkdir(parents=True, exist_ok=True)
                if sum(self.pending.values()) + size > self.max_bytes:
                    return None
                files = sorted(self._stored())
                used = sum(item[1] for item in files) + sum(self.pending.values())
                while files and used + size > self.max_bytes:
                    _, old_size, old_path = files.pop(0)
                    try:
                        old_path.unlink()
                        used -= old_size
                    except OSError:
                        pass
                if used + size > self.max_bytes:
                    return None
                self.pending[key] = size
                return folder / (eid + '.' + uuid.uuid4().hex + '.part')
            except OSError as exc:
                logger.warning('Cannot reserve local audio cache space: %s', exc)
                return None

    def _finish(self, mid: str, eid: str, partial: Path, valid: bool):
        with self.lock:
            try:
                if valid and mid not in self.invalidated:
                    os.replace(partial, self._file(mid, eid))
                else:
                    partial.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning('Could not finalise local audio cache entry: %s', exc)
                partial.unlink(missing_ok=True)
            finally:
                self.pending.pop((mid, eid), None)

    def seed(self, mid: str, eid: str, source_path: str, expected: int) -> bool:
        """Cache the newly generated file only AFTER the B2 upload succeeds."""
        partial = self._prepare(mid, eid, expected)
        if partial is None:
            return False
        good = False
        try:
            with open(source_path, 'rb') as src, partial.open('wb') as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            good = partial.stat().st_size == expected
            return good
        except OSError as exc:
            logger.warning('Could not seed local audio cache: %s', exc)
            return False
        finally:
            self._finish(mid, eid, partial, good)

    def tee(self, mid: str, eid: str, source, expected: int):
        """Forward a complete B2 download immediately, caching only if complete.

        Call for full-body requests only, never small Range requests. A second
        concurrent request streams B2 independently rather than waiting on a
        listener whose download may have stalled or been cancelled.
        """
        partial = self._prepare(mid, eid, expected)
        dst = None
        copied = 0
        healthy = partial is not None
        completed = False
        try:
            if partial is not None:
                try:
                    dst = partial.open('wb')
                except OSError:
                    healthy = False
            for chunk in source:
                if dst is not None and healthy:
                    try:
                        dst.write(chunk)
                        copied += len(chunk)
                        if copied > expected:
                            healthy = False
                    except OSError:
                        healthy = False
                yield chunk
            completed = healthy and dst is not None and copied == expected
        finally:
            if dst is not None:
                try:
                    dst.close()
                except OSError:
                    completed = False
            if partial is not None:
                self._finish(mid, eid, partial, completed)
            close = getattr(source, 'close', None)
            if close is not None:
                close()

    def invalidate_mirror(self, mid: str):
        """Prevent in-flight fills from recreating a deleted mirror's cache."""
        with self.lock:
            self.invalidated.add(mid)
            folder = self.root / mid
            if not self.enabled:
                return
            for path in folder.glob('*'):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # Open files can survive unlink on Linux.
            try:
                folder.rmdir()
            except OSError:
                pass
