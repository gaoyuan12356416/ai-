"""Bounded, process-shared source downloads; every hit revalidates its ETag.

The caller still owns URL admission, the frozen recipe and the job lease. Cache
errors degrade to the ordinary download. No publication state is read or changed.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid


class SourceDownloadError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(path, check):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            check()
            result.update(chunk)
    return result.hexdigest()


@contextmanager
def entry_lock(path, check, *, wait=True):
    """Lock only the same source; waiting remains cancellable by its job lease."""
    with Path(path).open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            def lock():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def unlock():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def lock():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def unlock():
                fcntl.flock(handle, fcntl.LOCK_UN)
        while True:
            check()
            try:
                lock()
                break
            except (BlockingIOError, PermissionError):
                if not wait:
                    yield False
                    return
                time.sleep(.05)
        try:
            yield True
        finally:
            unlock()


class SourceCache:
    def __init__(self, root, *, max_bytes=32 * 1024**3, reserve_bytes=40 * 1024**3):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes

    def fetch(self, url, destination, *, session_factory, max_source_bytes,
              check=lambda: None):
        started = time.monotonic()
        key = hashlib.sha256(url.encode()).hexdigest()
        target = Path(destination)
        folder = self.root / key[:2]
        try:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = entry_lock(folder / (key + '.lock'), check)
            lock.__enter__()
        except OSError:
            result = self._download(url, target, session_factory, max_source_bytes,
                                    check, started, key)
            result.pop('_etag', None)
            result['cache_note'] = 'cache_storage_unavailable'
            return result
        try:
            blob, manifest = folder / (key + '.mp4'), folder / (key + '.json')
            old = None
            try:
                old = json.loads(manifest.read_text(encoding='utf-8'))
                if (old.get('url_sha256') != key or not self._etag(old.get('etag'))
                        or not blob.is_file() or blob.is_symlink()
                        or blob.stat().st_size != old.get('size')
                        or not 0 < old['size'] <= max_source_bytes):
                    old = None
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                old = None
            result = self._download(url, target, session_factory, max_source_bytes,
                check, started, key, old=old, blob=blob)
            if result['cache_hit']:
                old['last_used'] = time.time()
                self._write_json(manifest, old)
            elif self._etag(result.get('_etag')):
                try:
                    if shutil.disk_usage(self.root).free >= self.reserve_bytes:
                        self._store(target, blob)
                        self._write_json(manifest, dict(url_sha256=key,
                            sha256=result['sha256'], size=result['size'],
                            etag=result['_etag'], last_used=time.time()))
                    else:
                        result['cache_note'] = 'cache_skipped_disk_reserve'
                    self._prune(exclude=key, check=check)
                except OSError:
                    result['cache_note'] = 'cache_storage_unavailable'
            result.pop('_etag', None)
            return result
        finally:
            lock.__exit__(None, None, None)

    @staticmethod
    def _etag(value):
        return (isinstance(value, str) and 2 <= len(value) <= 512
                and value.startswith('"') and value.endswith('"')
                and '\r' not in value and '\n' not in value)

    @staticmethod
    def _write_json(path, value):
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as handle:
                json.dump(value, handle, sort_keys=True)
            os.replace(temp, path)
        except OSError:
            pass  # An auxiliary cache must never invalidate downloaded media.
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _store(source, destination):
        temp = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            try:
                os.link(source, temp)
            except OSError:
                shutil.copyfile(source, temp)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def _download(self, url, target, session_factory, limit, check, started, key,
                  *, old=None, blob=None):
        session = session_factory()
        session.trust_env = False
        headers = {'If-None-Match': old['etag']} if old else {}
        try:
            check()
            response = session.get(url, headers=headers, stream=True,
                                   timeout=120, allow_redirects=False)
            try:
                if response.status_code == 304 and old:
                    if digest(blob, check) == old['sha256']:
                        try:
                            os.link(blob, target)
                        except FileExistsError:
                            raise
                        except OSError:
                            with blob.open('rb') as source, target.open('xb') as output:
                                for chunk in iter(lambda: source.read(1024*1024), b''):
                                    check()
                                    output.write(chunk)
                        return dict(sha256=old['sha256'], size=old['size'],
                            cache_hit=True, network_bytes=0,
                            elapsed_seconds=round(time.monotonic()-started, 6),
                            source_key=key, validation_status=304)
                    # Corrupt local bytes require a fresh body, not a new recipe.
                    response.close()
                    response = session.get(url, stream=True, timeout=120,
                                           allow_redirects=False)
                if response.status_code != 200:
                    raise SourceDownloadError('fb_gpu_source_download_failed')
                size, sha = 0, hashlib.sha256()
                try:
                    with target.open('xb') as handle:
                        for chunk in response.iter_content(1024 * 1024):
                            check()
                            size += len(chunk)
                            if size > limit:
                                raise SourceDownloadError('fb_gpu_source_too_large')
                            sha.update(chunk)
                            handle.write(chunk)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                if not size:
                    target.unlink(missing_ok=True)
                    raise SourceDownloadError('fb_gpu_source_download_failed')
                return dict(sha256=sha.hexdigest(), size=size, cache_hit=False,
                    network_bytes=size, elapsed_seconds=round(time.monotonic()-started, 6),
                    source_key=key, validation_status=200,
                    _etag=response.headers.get('ETag'))
            finally:
                response.close()
        finally:
            session.close()

    def _prune(self, *, exclude, check):
        """Evict only this cache's unlinked idle blobs; jobs keep their own links."""
        try:
            with entry_lock(self.root / '.gc.lock', check, wait=False) as acquired:
                if not acquired:
                    return
                entries, total = [], 0
                for path in self.root.glob('*/*.json'):
                    try:
                        data = json.loads(path.read_text(encoding='utf-8'))
                        blob = path.with_suffix('.mp4')
                        size = blob.stat().st_size
                        total += size
                        entries.append((float(data.get('last_used', 0)), path, blob, size))
                    except (OSError, ValueError, TypeError, AttributeError):
                        continue
                for _, manifest, blob, size in sorted(entries):
                    if (total <= self.max_bytes
                            and shutil.disk_usage(self.root).free >= self.reserve_bytes):
                        break
                    if manifest.stem == exclude or blob.stat().st_nlink > 1:
                        continue
                    with entry_lock(manifest.with_suffix('.lock'), check, wait=False) as acquired:
                        if acquired and blob.exists() and blob.stat().st_nlink == 1:
                            blob.unlink()
                            manifest.unlink(missing_ok=True)
                            total -= size
        except OSError:
            pass


def observation(root):
    try:
        return json.loads((Path(root)/'source-cache.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
