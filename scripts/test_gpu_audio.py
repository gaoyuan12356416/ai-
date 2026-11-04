"""Timeline and channel-contract tests; actual CUDA signal tests live separately."""
import unittest
from features.random_gpu.gpu_audio import AudioQualificationError, AudioTimeline, CudaAudioDsp


class AudioContracts(unittest.TestCase):
    def test_nearest_sample_duration_and_origin(self):
        value = AudioTimeline.create(input_rate=44100, input_samples=44100,
                                     duration_seconds="1.00001", start_seconds="0.25")
        self.assertEqual(value.output_samples, 48000)
        self.assertEqual(value.start_output_sample, 12000)
        value = AudioTimeline.create(input_rate=44100, input_samples=44100,
                                     duration_seconds="1.00002")
        self.assertEqual(value.output_samples, 48001)

    def test_negative_origin_does_not_shift_video_clock(self):
        value = AudioTimeline.create(input_rate=48000, input_samples=48000,
                                     duration_seconds=1, start_seconds="-0.1")
        self.assertEqual(value.start_output_sample, -4800)
        self.assertEqual(value.output_samples, 48000)

    def test_unknown_layout_cannot_silently_downmix(self):
        with self.assertRaises(AudioQualificationError):
            CudaAudioDsp.channel_matrix(6)
        self.assertEqual(CudaAudioDsp.channel_matrix(2), ((1.0, 0.0), (0.0, 1.0)))

    def test_invalid_timeline(self):
        for change in ({"input_rate":0}, {"input_rate":True}, {"input_samples":-1},
                       {"duration_seconds":"nan"}, {"duration_seconds":0}):
            args = dict(input_rate=48000, input_samples=48000, duration_seconds=1)
            args.update(change)
            with self.subTest(change=change), self.assertRaises(AudioQualificationError):
                AudioTimeline.create(**args)


if __name__ == "__main__":
    unittest.main()
