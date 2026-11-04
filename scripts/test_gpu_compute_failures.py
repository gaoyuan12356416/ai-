"""Tiny isolated GPU QA checks for digest state and process ownership.

Run with the evaluation interpreter, never as a worker or against job files.
The entire suite hashes less than 2 KiB. Faults are injected only around the
test object's calls; no device reset, service signal, or persistent state edit.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.gpu_compute import CudaSHA256, cupy_runtime, sha256_file


class DigestFailureTests(unittest.TestCase):
    def test_allocation_failure_cannot_be_retried_on_same_digest(self):
        value = bytes(range(128))
        digest = CudaSHA256()
        with patch.object(digest.cp, "asarray", side_effect=MemoryError("qa_only")):
            with self.assertRaises(MemoryError):
                digest.update(value)
        try:
            digest.update(value)
            actual = digest.hexdigest()
        except RuntimeError:
            return
        self.fail("failed digest remained usable: actual=%s expected=%s" %
                  (actual, hashlib.sha256(value).hexdigest()))

    def test_unknown_final_kernel_completion_cannot_be_retried(self):
        value = b"qa-finalization"
        digest = CudaSHA256().update(value)
        real_kernel = digest.kernel

        def completed_then_failed(*args, **kwargs):
            real_kernel(*args, **kwargs)
            digest.cp.cuda.get_current_stream().synchronize()
            raise RuntimeError("qa_unknown_completion")

        digest.kernel = completed_then_failed
        with self.assertRaises(RuntimeError):
            digest.hexdigest()
        digest.kernel = real_kernel
        try:
            actual = digest.hexdigest()
        except RuntimeError:
            return
        self.fail("unknown finalization was replayed: actual=%s expected=%s" %
                  (actual, hashlib.sha256(value).hexdigest()))

    def test_digest_rejects_inherited_process_before_cuda_use(self):
        digest = CudaSHA256()
        parent_pid = os.getpid()
        # Simulate ownership change without forking a process holding CUDA.
        with patch("features.random_gpu.gpu_compute.os.getpid", return_value=parent_pid + 1):
            with self.assertRaises(RuntimeError):
                digest.update(b"child")
            with self.assertRaises(RuntimeError):
                digest.hexdigest()

    def test_runtime_cache_rejects_inherited_process(self):
        cupy_runtime()
        parent_pid = os.getpid()
        with patch("features.random_gpu.gpu_compute.os.getpid", return_value=parent_pid + 1):
            with self.assertRaises(RuntimeError):
                cupy_runtime()

    def test_state_initialization_is_ready_across_nonblocking_streams(self):
        import numpy as np
        cp = cupy_runtime()
        # Warm the digest kernel before introducing a bounded stream delay.
        CudaSHA256().update(b"warm").hexdigest()
        delay = cp.RawKernel(r'''
            extern "C" __global__ void qa_delay(unsigned long long cycles) {
                unsigned long long start = clock64();
                while (clock64() - start < cycles) { }
            }
        ''', "qa_delay")
        creator_stream = cp.cuda.Stream(non_blocking=True)
        with creator_stream:
            delay((1,), (1,), (np.uint64(50_000_000),))
            digest = CudaSHA256()
        try:
            actual = digest.update(b"different-stream").hexdigest()
        finally:
            creator_stream.synchronize()
        self.assertEqual(actual, hashlib.sha256(b"different-stream").hexdigest())

    def test_independent_files_and_threads_keep_separate_states(self):
        values = [b"", b"a" * 129, bytes(range(256))]

        def compute(data):
            digest = CudaSHA256()
            for pos in range(0, len(data), 17):
                digest.update(data[pos:pos + 17])
            result = digest.hexdigest()
            self.assertEqual(result, digest.hexdigest())
            return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(compute, values)),
                             [hashlib.sha256(v).hexdigest() for v in values])

    def test_file_wrapper_odd_chunks_and_empty_file(self):
        with tempfile.TemporaryDirectory(prefix="gpu-sha-qa-") as directory:
            path = Path(directory) / "input.bin"
            for value in (b"", b"content" * 19):
                path.write_bytes(value)
                actual, size = sha256_file(path, chunk_bytes=65)
                self.assertEqual(size, len(value))
                self.assertEqual(actual, hashlib.sha256(value).hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
