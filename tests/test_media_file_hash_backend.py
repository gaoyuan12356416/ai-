"""Media hashing preserves streaming SHA-256 without creating a CUDA context."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.random_gpu import gpu_compute
from features.fb_gpu.random_overlay import sha256_file as fb_sha256_file


class MediaFileHashTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            'RANDOM_GPU_COMPUTE': '1', 'PIPELINE_RENDER_BACKEND': 'gpu',
        })
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('RANDOM_GPU_FILE_HASH_BACKEND', None)

    def test_default_gpu_worker_uses_cpu_without_disabling_video_compute(self):
        with patch.object(gpu_compute, 'CudaSHA256', side_effect=AssertionError('CUDA entered')):
            for size in (0, 55, 56, 63, 64, 65, 1048576 + 17):
                data = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
                value = gpu_compute.file_digest()
                for offset in range(0, len(data), 63):
                    value.update(memoryview(data)[offset:offset + 63])
                self.assertEqual(value.hexdigest(), hashlib.sha256(data).hexdigest())
                self.assertEqual(value.hexdigest(), value.hexdigest())
            self.assertTrue(gpu_compute.enabled())

    def test_production_file_wrapper_rehashes_changed_bytes_and_returns_size(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(gpu_compute, 'CudaSHA256', side_effect=AssertionError('CUDA entered')):
            path = Path(directory) / 'media.mp4'
            for data in (b'a' * (1048576 + 65), b'b' * (1048576 + 65), b''):
                path.write_bytes(data)
                self.assertEqual(fb_sha256_file(path), (hashlib.sha256(data).hexdigest(), len(data)))

    def test_explicit_cuda_request_still_propagates_failure(self):
        with patch.dict(os.environ, {'RANDOM_GPU_FILE_HASH_BACKEND': 'cuda'}), \
                patch.object(gpu_compute, 'CudaSHA256', side_effect=RuntimeError('cuda unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'cuda unavailable'):
                gpu_compute.file_digest()

    def test_cpu_node_does_not_enter_cuda_and_invalid_backend_is_rejected(self):
        with patch.dict(os.environ, {'PIPELINE_RENDER_BACKEND': 'cpu', 'RANDOM_GPU_FILE_HASH_BACKEND': 'cuda'}), \
                patch.object(gpu_compute, 'CudaSHA256', side_effect=AssertionError('CUDA entered')):
            self.assertEqual(gpu_compute.file_digest().hexdigest(), hashlib.sha256(b'').hexdigest())
        with patch.dict(os.environ, {'RANDOM_GPU_FILE_HASH_BACKEND': 'typo'}):
            with self.assertRaisesRegex(ValueError, 'invalid_file_hash_backend'):
                gpu_compute.file_digest()


if __name__ == '__main__':
    unittest.main()
