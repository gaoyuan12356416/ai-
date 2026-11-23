import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from features.random_gpu import gpu_audio


class InvalidPacket(Exception):pass
class CompatibilityReached(Exception):pass
MUX=SimpleNamespace(PacketFile=object,Sample=object,Track=object)


class AudioPacketTests(unittest.TestCase):
    def test_invalid_compressed_packet_uses_existing_timeline_compatibility(self):
        fake=SimpleNamespace(error=SimpleNamespace(InvalidDataError=InvalidPacket))
        with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules',{'av':fake,'numpy':SimpleNamespace(),'features.random_gpu.gpu_mux':MUX}), \
                patch.object(gpu_audio,'decode_contiguous_audio',side_effect=InvalidPacket), \
                patch.object(gpu_audio,'_decode_cpu_dsp_compatibility',side_effect=CompatibilityReached) as compat:
            with self.assertRaises(CompatibilityReached):
                gpu_audio.prepare_audio_track('frozen-source.mp4',temp,1,timeline_origin_seconds=0)
            self.assertEqual(compat.call_args.args[3],48000)
            self.assertTrue(compat.call_args.kwargs['explicit_origin'])

    def test_other_decode_errors_are_not_masked(self):
        fake=SimpleNamespace(error=SimpleNamespace(InvalidDataError=InvalidPacket))
        with tempfile.TemporaryDirectory() as temp, patch.dict('sys.modules',{'av':fake,'numpy':SimpleNamespace(),'features.random_gpu.gpu_mux':MUX}), \
                patch.object(gpu_audio,'decode_contiguous_audio',side_effect=RuntimeError('unexpected')), \
                patch.object(gpu_audio,'_decode_cpu_dsp_compatibility') as compat:
            with self.assertRaisesRegex(RuntimeError,'unexpected'):
                gpu_audio.prepare_audio_track('frozen-source.mp4',temp,1,timeline_origin_seconds=0)
            compat.assert_not_called()


if __name__=='__main__':unittest.main()
