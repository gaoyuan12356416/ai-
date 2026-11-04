"""Independent HEVC prefix/box metadata tests; synthetic NALs are not media."""
import struct
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.random_gpu.gpu_mux import CudaMP4, MuxError, Sample, Track
from scripts.test_gpu_mux_boundaries import find_box


def ue(value):
    bits = bin(value+1)[2:]
    return "0"*(len(bits)-1)+bits


def rbsp(bits):
    bits += "1"
    bits += "0"*(-len(bits) % 8)
    raw = bytes(int(bits[i:i+8], 2) for i in range(0, len(bits), 8))
    result, zeros = bytearray(), 0
    for byte in raw:
        if zeros >= 2 and byte <= 3:
            result.append(3)
            zeros = 0
        result.append(byte)
        zeros = zeros+1 if byte == 0 else 0
    return bytes(result)


def nal(kind, body, layer=0):
    return struct.pack(">H", (kind << 9) | (layer << 3) | 1)+body


PTL = bytes.fromhex("016000000090000000000078")


def parameter_sets(sub=0, vps_sub=None, chroma=1, depth=0, layer=0, max_layers=0):
    if vps_sub is None:
        vps_sub = sub
    ptl = "".join(format(v, "08b") for v in PTL)
    vps = nal(32, rbsp("000011"+format(max_layers, "06b")+format(vps_sub, "03b")+"1"+"1"*16+ptl), layer)
    prefix = "0000"+format(sub, "03b")+"1"+ptl
    if sub:
        # All sublayers include both PTL sections; use zero flags in their
        # payload to force EPB crossings inside the 88-bit profile fields.
        prefix += "11"*sub+"00"*(8-sub)
        prefix += ("0"*88+format(60, "08b"))*sub
    prefix += ue(0)+ue(chroma)+("0" if chroma == 3 else "")
    prefix += ue(720)+ue(1280)+"1"+ue(0)*4+ue(depth)+ue(depth)
    sps = nal(33, rbsp(prefix), layer)
    pps = nal(34, rbsp(ue(0)+ue(0)), layer)
    return [vps, sps, pps]


def sequence(units):
    return b"".join((b"\0\0\0\1" if i % 2 else b"\0\0\1")+unit
                    for i, unit in enumerate(units))


class HEVCBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mux = CudaMP4()

    def test_config_header_and_arrays_are_exact(self):
        units = parameter_sets()
        config = self.mux.hvcc_config(sequence(units))
        expected = (b"\x01"+PTL+b"\xf0\x00\xfc\xfd\xf8\xf8\x00\x00\x0f\x03"+
                    b"".join(bytes([128|kind])+struct.pack(">HH", 1, len(unit))+unit
                             for kind, unit in zip((32, 33, 34), units)))
        self.assertEqual(config, expected)

    def test_sub_layer_profile_level_flags_and_epb_and_conformance_window(self):
        units = parameter_sets(sub=2, chroma=3, depth=2)
        self.assertIn(b"\0\0\3", units[1])
        config = self.mux.hvcc_config(sequence(units))
        self.assertEqual(config[16:19], b"\xff\xfa\xfa")
        self.assertEqual((config[21] >> 3) & 7, 3)
        self.assertEqual(config[21] & 7, 7)

    def test_temporal_layers_include_vps_maximum(self):
        config = self.mux.hvcc_config(sequence(parameter_sets(sub=0, vps_sub=2)))
        self.assertEqual((config[21] >> 3) & 7, 3)

    def test_multilayer_is_outside_encoder_contract(self):
        for units in (parameter_sets(layer=1), parameter_sets(max_layers=1)):
            with self.subTest(units=units), self.assertRaises(MuxError):
                self.mux.hvcc_config(sequence(units))

    def test_missing_duplicate_truncated_and_oversized_parameter_sets(self):
        units = parameter_sets()
        invalid = [units[:2], units+[units[0]], [units[0], units[1][:15], units[2]],
                   [units[0], units[1], units[2][:2]],
                   [units[0], units[1], nal(34, b"\xff"*65535)]]
        for item in invalid:
            with self.subTest(lengths=list(map(len, item))), self.assertRaises(MuxError):
                self.mux.hvcc_config(sequence(item))

    def test_reserved_vps_temporal_layer_count_is_rejected(self):
        with self.assertRaises(MuxError):
            self.mux.hvcc_config(sequence(parameter_sets(vps_sub=7)))

    def test_annexb_strips_only_parameter_sets_and_aud_preserves_sei_and_irap(self):
        sei = nal(39, b"\0\0\3\1\x80")
        aud = nal(35, b"\x50")
        for kind, key in ((1, False), (16, True), (19, True), (21, True)):
            slice_data = nal(kind, b"\xa5\x80")
            data, actual_key = self.mux.annexb_sample(
                sequence(parameter_sets()+[aud, sei, slice_data]), codec="hevc", with_key=True)
            expected = b"".join(struct.pack(">I", len(n))+n for n in (sei, slice_data))
            self.assertEqual((data, actual_key), (expected, key))

    def test_annexb_rejects_parameter_only_access_unit(self):
        with self.assertRaises(MuxError):
            self.mux.annexb_sample(sequence(parameter_sets()), codec="hevc")

    def test_aac_esds_uses_actual_packet_average_not_fixed_192k(self):
        track = Track("audio", 48000, [Sample(b"a"*31, -1024, -1024, 1024),
                                      Sample(b"b"*33, 0, 0, 512)], b"\x11\x90")
        sample_entry = self.mux.sample_entry(track).get().tobytes()
        body = find_box(sample_entry, [b"mp4a"])
        esds = find_box(body[28:], [b"esds"])
        self.assertEqual(esds[9], 4)  # DecoderConfigDescriptor.
        self.assertEqual(struct.unpack(">II", esds[16:24]), (0, 16000))


if __name__ == "__main__":
    unittest.main()
