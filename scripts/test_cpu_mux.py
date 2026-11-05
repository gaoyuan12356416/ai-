"""Run the existing container boundary suite on the CPU backend."""
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.cpu_mux import CpuMP4
from features.random_gpu.gpu_mux import MuxError
from scripts import test_gpu_mux_boundaries as boundaries


class CpuMuxTests(boundaries.MuxBoundaryTests):
    @classmethod
    def setUpClass(cls):
        cls.muxer = CpuMP4()

    def track_bytes(self, track, offsets=(100, 102), wide=False):
        return self.muxer.track_box(track, 1, 3840,
            self.muxer.cp.asarray(offsets, dtype=self.muxer.cp.uint64), wide).tobytes()

    def test_hevc_keeps_sei_and_slice_bytes(self):
        nals = [bytes([k << 1, 1, 0, 0, 3, 1, 128]) for k in (32,33,34,35,39,19,1)]
        stream = b''.join(b'\0\0\0\1'+nal for nal in nals)
        expected = b''.join(struct.pack('>I', len(nal))+nal for nal in nals[4:])
        self.assertEqual(self.muxer.annexb_sample(stream, codec='hevc', with_key=True), (expected, True))

    def test_leading_zero_prefix_matches_cuda_start_detection(self):
        nal = b'\x65\x88\0\0'
        self.assertEqual(self.muxer.annexb_sample(b'\0\0\0\0\1'+nal), struct.pack('>I',len(nal))+nal)


if __name__ == '__main__':
    unittest.main(verbosity=2)
