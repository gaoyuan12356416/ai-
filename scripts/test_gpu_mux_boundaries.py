"""Tiny CUDA mux metadata/file lifecycle checks; no production media or services.

Synthetic packets exercise container structure only, not codec acceptance.
Real codec/decode equivalence is checked by validate_gpu_mux.py separately.
"""
from dataclasses import replace
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.gpu_mux import CudaMP4, MuxError, PacketFile, Sample, Track


def boxes(data):
    result = []
    offset = 0
    while offset < len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        if size < header or offset + size > len(data):
            raise AssertionError(('invalid_box_size', kind, size, len(data)-offset))
        result.append((kind, data[offset+header:offset+size]))
        offset += size
    return result


def find_box(data, path):
    for name in path:
        found = [body for kind, body in boxes(data) if kind == name]
        if len(found) != 1:
            raise AssertionError(('box_count', name, len(found)))
        data = found[0]
    return data


def video(samples=None):
    return Track('video', 25, samples or [Sample(b'V0', 0, 0, 1, True),
                                         Sample(b'V1', 1, 1, 1)],
                 b'\x01\x42\x00\x1f\xff\xe1\x00\x01g\x01\x00\x01h',
                 width=16, height=16)


class MuxBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.muxer = CudaMP4()

    def track_bytes(self, track, offsets=(100, 102), wide=False):
        cp = self.muxer.cp
        return self.muxer.track_box(track, 1, 3840,
            cp.asarray(offsets, dtype=cp.uint64), wide).get().tobytes()

    def table(self, data, kind):
        return find_box(data, [b'trak', b'mdia', b'minf', b'stbl', kind])

    def test_annexb_mixed_prefixes_preserves_escaped_payload_and_key(self):
        sps, pps, aud = b'\x67\x64\x00\x1f', b'\x68\x80', b'\x09\xf0'
        sei, idr, p_slice = b'\x06\x00\x00\x03\x01\x80', b'\x65\x88', b'\x41\x99'
        nals = [sps, pps, aud, sei, idr, p_slice]
        annexb = b''.join((b'\0\0\0\1' if n % 2 else b'\0\0\1')+nal for n, nal in enumerate(nals))
        expected = b''.join(struct.pack('>I', len(nal))+nal for nal in [sei, idr, p_slice])
        self.assertEqual(self.muxer.annexb_sample(annexb, with_key=True), (expected, True))
        self.assertEqual(self.muxer.annexb_sample(b'\0\0\1'+p_slice, with_key=True), (b'\0\0\0\2'+p_slice, False))

    def test_annexb_rejects_missing_and_empty_units(self):
        for data in [b'no-start', b'\0\0\1\0\0\1\x65', b'\0\0\1\x09\xf0']:
            with self.subTest(data=data), self.assertRaises(MuxError):
                self.muxer.annexb_sample(data)

    def test_high_avcc_sps_fields_and_emulation_prevention(self):
        def ue(value):
            code = bin(value+1)[2:]
            return '0'*(len(code)-1)+code
        def escaped(bits):
            bits += '1'
            bits += '0'*(-len(bits) % 8)
            raw = bytes(int(bits[n:n+8], 2) for n in range(0, len(bits), 8))
            output = bytearray(); zeros = 0
            for byte in raw:
                if zeros >= 2 and byte <= 3:
                    output.append(3); zeros = 0
                output.append(byte)
                zeros = zeros+1 if byte == 0 else 0
            return bytes(output)
        # Large sps_id intentionally makes an EPB cross the Exp-Golomb prefix.
        # The parser supports a prefix contract; these are not complete SPSes.
        for sid, chroma, luma, color in [(0,1,0,0), (1,3,2,2), (2**23-1,2,4,4)]:
            prefix = ue(sid)+ue(chroma)+('0' if chroma == 3 else '')+ue(luma)+ue(color)
            sps = b'\x67\x64\0\x1f'+escaped(prefix)
            pps = b'\x68\x80'
            sequence = b'\0\0\0\1'+sps+b'\0\0\1'+pps
            actual = self.muxer.avcc_config(sequence)
            expected = b'\x01\x64\0\x1f\xff\xe1'+struct.pack('>H',len(sps))+sps+b'\x01'+struct.pack('>H',len(pps))+pps+bytes([252|chroma,248|luma,248|color,0])
            self.assertEqual(actual, expected)

    def test_real_nvenc_high_sps_config_tail(self):
        sequence = bytes.fromhex('000000016764001fac2ca502d0286c04400000fa00003a983800000302ab9800001ab3f0bbcb83e38532c00000000168eb8f2c')
        actual = self.muxer.avcc_config(sequence)
        self.assertEqual(actual[-4:], b'\xfd\xf8\xf8\0')

    def test_high_avcc_truncated_prefix_is_rejected(self):
        sequence = b'\0\0\1\x67\x64\0\x1f\0\0\1\x68\x80'
        with self.assertRaisesRegex(MuxError, 'invalid_high_profile_sps'):
            self.muxer.avcc_config(sequence)

    def test_video_only_structure_and_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'out.mp4'
            report = self.muxer.mux([video()], output, '0.08')
            data = output.read_bytes()
            self.assertEqual([kind for kind, _ in boxes(data)], [b'ftyp', b'moov', b'mdat'])
            self.assertEqual(find_box(data, [b'mdat']), b'V0V1')
            self.assertEqual(report['tracks'], 1)
            stco = find_box(data, [b'moov', b'trak', b'mdia', b'minf', b'stbl', b'stco'])
            self.assertEqual(struct.unpack_from('>I', stco, 4)[0], 2)
            offsets = struct.unpack_from('>II', stco, 8)
            self.assertEqual([data[p:p+2] for p in offsets], [b'V0', b'V1'])

    def test_two_tracks_interleave_with_exact_dts_order(self):
        audio = Track('audio', 48000,
                      [Sample(b'A0', -1024, -1024, 1024),
                       Sample(b'A1', 0, 0, 1024),
                       Sample(b'A2', 1024, 1024, 1024)], b'\x11\x90')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'out.mp4'
            self.muxer.mux([video(), audio], output, '0.08')
            self.assertEqual(find_box(output.read_bytes(), [b'mdat']), b'A0V0A1A2V1')

    def test_co64_values_cross_uint32_boundary(self):
        track = video([Sample(b'a', n, n, 1, n == 0) for n in range(3)])
        values = (2**32-1, 2**32, 2**32+11)
        table = self.table(self.track_bytes(track, values, True), b'co64')
        self.assertEqual(struct.unpack('>IIQQQ', table), (0, 3, *values))
        # This validates table serialization, not a real 4-GiB write or the
        # complete muxer's automatic stco->co64 branch.

    def test_automatic_co64_rebuild_updates_metadata_base(self):
        captured = []
        class HeaderWriter(io.BytesIO):
            def __exit__(self, *args):
                captured.append(self.getvalue())
                self.close()
        def open_capture(fd, mode):
            os.close(fd)
            return HeaderWriter()
        def stop_before_payload():
            raise InterruptedError('metadata_only')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base/'source'; source.write_bytes(b'placeholder')
            track = video([Sample(PacketFile(source, 0, 2**32-1), 0, 0, 1, True),
                           Sample(b'x', 1, 1, 1)])
            with patch('features.random_gpu.gpu_mux.os.fdopen', side_effect=open_capture):
                with self.assertRaisesRegex(InterruptedError, 'metadata_only'):
                    self.muxer.mux([track], base/'out.mp4', '0.08', check=stop_before_payload)
            header = captured[0]
            # Header ends with an mdat prefix declaring >4 GiB, so parse only
            # the complete ftyp+moov boxes, excluding that 16-byte prefix.
            table = find_box(header[:-16], [b'moov', b'trak', b'mdia', b'minf', b'stbl', b'co64'])
            version, count, first, second = struct.unpack('>IIQQ', table)
            self.assertEqual((version, count, first, second), (0, 2, len(header), len(header)+2**32-1))
            self.assertEqual(struct.unpack('>I4sQ', header[-16:]), (1, b'mdat', 2**32+16))
            self.assertFalse((base/'out.mp4').exists())

    def test_negative_ctts_uses_signed_version_one(self):
        track = video([Sample(b'a', -1, 0, 2, True), Sample(b'b', 3, 2, 2)])
        table = self.table(self.track_bytes(track), b'ctts')
        self.assertEqual(struct.unpack('>II Ii Ii', table), (0x01000000, 2, 1, -1, 1, 1))

    def test_duration_limits_are_checked(self):
        for duration in (0, -1, 2**32):
            with self.subTest(duration=duration), self.assertRaisesRegex(MuxError, 'invalid_sample_duration'):
                self.track_bytes(video([Sample(b'a', 0, 0, duration, True)]), (100,))
        track = video([Sample(b'a', 0, 0, 2**32-1, True)])
        stts = self.table(self.track_bytes(track, (100,)), b'stts')
        self.assertEqual(struct.unpack('>IIII', stts), (0, 1, 1, 2**32-1))

    def test_decode_gap_positive_origin_and_ctts_overflow_rejected(self):
        for samples, message in [
            ([Sample(b'a', 0, 0, 1, True), Sample(b'b', 2, 2, 1)], 'non_contiguous'),
            ([Sample(b'a', 1, 1, 1, True)], 'positive_decode_origin'),
            ([Sample(b'a', 2**31, 0, 1, True)], 'composition_offset')]:
            with self.subTest(message=message), self.assertRaisesRegex(MuxError, message):
                self.track_bytes(video(samples), tuple(100+n for n in range(len(samples))))

    def test_packet_file_matches_in_memory_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base/'packets'; source.write_bytes(b'xxV0--V1yy')
            reference, actual = base/'reference.mp4', base/'actual.mp4'
            track = video()
            self.muxer.mux([track], reference, '0.08')
            disk = replace(track, samples=[replace(track.samples[0], data=PacketFile(source, 2, 2)),
                                           replace(track.samples[1], data=PacketFile(source, 6, 2))])
            self.muxer.mux([disk], actual, '0.08')
            self.assertEqual(actual.read_bytes(), reference.read_bytes())

    def test_truncation_and_cancel_preserve_output_and_foreign_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base/'out.mp4'; output.write_bytes(b'previous-good-output')
            foreign = base/'out.mp4.mux-tmp'; foreign.write_bytes(b'other-writer')
            source = base/'packets'; source.write_bytes(b'x')
            track = video([Sample(PacketFile(source, 0, 4), 0, 0, 1, True)])
            with self.assertRaisesRegex(MuxError, 'packet_source_truncated'):
                self.muxer.mux([track], output, '0.08')
            def cancel():
                raise InterruptedError('qa_cancel')
            with self.assertRaisesRegex(InterruptedError, 'qa_cancel'):
                self.muxer.mux([video()], output, '0.08', check=cancel)
            self.assertEqual(output.read_bytes(), b'previous-good-output')
            self.assertEqual(foreign.read_bytes(), b'other-writer')
            self.assertEqual(list(base.glob('.out.mp4-*.mux-tmp')), [])


if __name__ == '__main__':
    unittest.main(verbosity=2, failfast=True)
