"""CPU-only regression for immutable-asset digest reuse and invalidation.

The CUDA constructor is replaced with a counting SHA oracle. These tests
verify routing/cache semantics, not device SHA correctness or performance.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.random_gpu import gpu_compute as compute, asset_cache
from features.fb_gpu import random_overlay as fb
from features.tt_gpu import random_overlay as tt


class ImmutableAssetDigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'asset.webm'
        self.file.write_bytes(b'original asset')
        compute._IMMUTABLE_ASSET_DIGESTS.clear()
        self.addCleanup(compute._IMMUTABLE_ASSET_DIGESTS.clear)
        self.env = patch.dict(os.environ, {'RANDOM_GPU_COMPUTE': '1', 'PIPELINE_RENDER_BACKEND': 'gpu'})
        self.env.start(); self.addCleanup(self.env.stop)
        cuda_patch = patch.object(compute, 'CudaSHA256', side_effect=lambda: hashlib.sha256())
        self.cuda = cuda_patch.start(); self.addCleanup(cuda_patch.stop)

    def digest(self):
        return compute.immutable_asset_sha256_file(self.file)

    def catalog(self):
        rows = {}
        for category in fb.ASSET_CATEGORIES:
            path = self.root / (category + '.png')
            value = category.encode(); path.write_bytes(value)
            rows[category] = [dict(name=path.name, media_type='image/png', size=len(value),
                                  sha256=hashlib.sha256(value).hexdigest())]
        data = json.dumps(dict(version=1, categories=rows)).encode()
        (self.root / 'manifest.json').write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_cross_fb_tt_and_rgba_cache_reads_each_unique_file_once(self):
        fingerprint = self.catalog()
        assets = fb.load_asset_set(self.root, fingerprint)
        tt.load_asset_set(self.root, fingerprint)
        cache = self.root / 'rgba'; cache.mkdir()
        for category in fb.CATEGORIES:
            asset = assets['categories'][category][0]; sha = asset['sha256']
            target = cache / (sha + '.nut'); target.write_bytes(b'verified cache fixture')
            (cache / (sha + '.json')).write_text(json.dumps(dict(version=asset_cache.VERSION,
                source_sha256=sha, rgba_frames_verified=True, size=target.stat().st_size,
                mtime_ns=target.stat().st_mtime_ns)))
            self.assertEqual(asset_cache.cached_path(asset['path'], cache), target)
        fb.load_asset_set(self.root, fingerprint); tt.load_asset_set(self.root, fingerprint)
        self.assertEqual(self.cuda.call_count, 1 + len(fb.ASSET_CATEGORIES))
        with self.assertRaises(fb.RandomOverlayError): fb.load_asset_set(self.root, '0' * 64)
        with self.assertRaises(tt.RandomOverlayError): tt.load_asset_set(self.root, '0' * 64)

    def test_same_size_same_mtime_ctime_change_rehashes(self):
        original = self.file.stat(); generation = [0]
        real_lstat, real_fstat = Path.lstat, os.fstat
        def changed_ctime(value):
            # Windows exposes creation time as st_ctime on older Python;
            # explicitly model Linux change-time so this unit test is portable.
            return SimpleNamespace(**{key: getattr(value, key) for key in
                ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns')},
                st_ctime_ns=value.st_ctime_ns + generation[0])
        with patch.object(Path, 'lstat', lambda path, *a, **kw: changed_ctime(real_lstat(path, *a, **kw))), \
             patch.object(os, 'fstat', side_effect=lambda fd: changed_ctime(real_fstat(fd))):
            self.digest()
            self.file.write_bytes(b'modified asset'); os.utime(self.file, ns=(original.st_atime_ns, original.st_mtime_ns))
            generation[0] += 1
            self.assertEqual(self.digest(), (hashlib.sha256(b'modified asset').hexdigest(), original.st_size))
        self.assertEqual(self.cuda.call_count, 2)

    def test_atomic_replace_with_same_size_and_mtime_rehashes(self):
        self.digest(); before = self.file.stat()
        replacement = self.root / 'new.webm'; replacement.write_bytes(b'modified asset')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns)); replacement.replace(self.file)
        self.assertEqual(self.digest()[0], hashlib.sha256(b'modified asset').hexdigest())
        self.assertEqual(self.cuda.call_count, 2)

    def test_cpu_disabled_gpu_is_never_used_and_backend_does_not_share_cache(self):
        with patch.dict(os.environ, {'PIPELINE_RENDER_BACKEND': 'cpu'}), \
             patch.object(compute, 'CudaSHA256', side_effect=AssertionError('CPU entered CUDA')):
            self.assertEqual(self.digest(), self.digest())
        self.assertEqual(self.cuda.call_count, 0)
        self.digest(); self.digest()
        self.assertEqual({key[1] for key in compute._IMMUTABLE_ASSET_DIGESTS}, {'cpu', 'cuda'})
        self.assertEqual(self.cuda.call_count, 1)

    def test_pid_change_does_not_use_parent_cache(self):
        self.digest(); pid = os.getpid()
        with patch.object(os, 'getpid', return_value=pid + 1): self.digest()
        self.assertEqual(self.cuda.call_count, 2)

    def test_gpu_failure_is_not_cached_or_fallen_back(self):
        self.cuda.side_effect = RuntimeError('CUDA failed')
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'CUDA failed'): self.digest()
        self.assertFalse(compute._IMMUTABLE_ASSET_DIGESTS)
        self.assertEqual(self.cuda.call_count, 2)

    def test_finalization_failure_does_not_cache_partial_result(self):
        class FailedDigest:
            def update(self, block): pass
            def hexdigest(self): raise RuntimeError('finalize failed')
        self.cuda.side_effect = FailedDigest
        with self.assertRaisesRegex(RuntimeError, 'finalize failed'): self.digest()
        self.assertFalse(compute._IMMUTABLE_ASSET_DIGESTS)
        self.cuda.side_effect = hashlib.sha256
        self.assertEqual(self.digest()[0], hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_change_during_read_is_rejected_and_not_cached(self):
        path = self.file
        class ChangingDigest:
            def __init__(self): self.value = hashlib.sha256()
            def update(self, block):
                self.value.update(block); path.write_bytes(b'new longer asset content')
            def hexdigest(self): return self.value.hexdigest()
        self.cuda.side_effect = ChangingDigest
        with self.assertRaisesRegex(ValueError, 'immutable_asset_changed'): self.digest()
        self.assertFalse(compute._IMMUTABLE_ASSET_DIGESTS)
        self.cuda.side_effect = hashlib.sha256
        self.assertEqual(self.digest()[0], hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_replacement_between_lstat_and_open_rejects_cached_hit(self):
        self.digest(); real_open = os.open
        replacement = self.root / 'replacement'; replacement.write_bytes(b'modified asset')
        def replace_before_open(path, flags, *args, **kwargs):
            replacement.replace(self.file)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(os, 'open', side_effect=replace_before_open):
            with self.assertRaisesRegex(ValueError, 'immutable_asset_changed'): self.digest()
        self.assertEqual(self.cuda.call_count, 1)
        self.assertEqual(self.digest()[0], hashlib.sha256(b'modified asset').hexdigest())

    def test_public_source_and_output_hash_functions_are_not_cached(self):
        for loader in (fb, tt):
            for _ in range(2): self.assertEqual(loader.sha256_file(self.file)[0], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(self.cuda.call_count, 4)
        self.assertFalse(compute._IMMUTABLE_ASSET_DIGESTS)

    def test_concurrent_callers_share_one_completed_digest(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            values = list(executor.map(lambda _: self.digest(), range(8)))
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(self.cuda.call_count, 1)

    def test_cached_catalog_still_rejects_symlink_metadata(self):
        fingerprint = self.catalog(); assets = fb.load_asset_set(self.root, fingerprint)
        selected = assets['categories']['border'][0]['path']
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == selected or original(path)):
            for loader in (fb, tt):
                with self.assertRaises(loader.RandomOverlayError): loader.load_asset_set(self.root, fingerprint)
        # Also prove the shared helper rejects lstat symlinks before opening,
        # on hosts where creating a real symlink requires extra privileges.
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFLNK)):
            with self.assertRaisesRegex(ValueError, 'immutable_asset_not_regular'): self.digest()

    def test_symlink_is_rejected_even_after_original_was_cached(self):
        self.digest(); other = self.root / 'other.webm'; other.write_bytes(b'other')
        self.file.unlink()
        try: self.file.symlink_to(other)
        except OSError as exc: self.skipTest('host cannot create symlinks: ' + str(exc))
        with self.assertRaisesRegex(ValueError, 'immutable_asset_not_regular'): self.digest()
        for loader in (fb, tt):
            with self.assertRaises(loader.RandomOverlayError): loader.load_asset_set(self.file, '0' * 64)


if __name__ == '__main__': unittest.main()
