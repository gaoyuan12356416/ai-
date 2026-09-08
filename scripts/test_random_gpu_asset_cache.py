import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from features.random_gpu.asset_cache import VERSION, cached_path, replace_asset_inputs, sha256_file
from features.random_gpu.compositor import fuse_command


class AssetCacheTests(unittest.TestCase):
    def setup_cache(self, root):
        source = root / 'assets'; source.mkdir()
        cache = root / 'cache'; cache.mkdir()
        paths = []
        for name in ('border.png', 'opacity.webm', 'corners.webm', 'tint.png'):
            path = source / name; path.write_text(name)
            key = sha256_file(path); raw = cache / (key + '.nut'); raw.write_bytes(b'cached-' + name.encode())
            record = {'version': VERSION, 'source_sha256': key, 'rgba_frames_verified': True,
                      'size': raw.stat().st_size, 'mtime_ns': raw.stat().st_mtime_ns}
            (cache / (key + '.json')).write_text(json.dumps(record))
            paths.append(str(path))
        return cache, paths

    def test_preserves_source_audio_options_and_loop_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache, paths = self.setup_cache(root)
            command = ['ffmpeg', '-y', '-i', 'source.mp4', '-loop', '1', '-i', paths[0],
                       '-stream_loop', '-1', '-c:v', 'libvpx-vp9', '-i', paths[1],
                       '-stream_loop', '-1', '-c:v', 'libvpx-vp9', '-i', paths[2],
                       '-loop', '1', '-i', paths[3], '-f', 'lavfi', '-i', 'anullsrc',
                       '-filter_complex', 'null', '-map', '[v]', '-map', '5:a:0', '-c:v', 'hevc_nvenc', str(root / 'out.mp4')]
            with patch.dict(os.environ, {'RANDOM_GPU_ASSET_CACHE_ROOT': str(cache)}):
                result = fuse_command(command, {'rotation_millidegrees': 0, 'scale_bp': 10000, 'tint_opacity_bp': 100}, root / 'out.mp4')
            self.assertEqual(command[command.index('-map'):], result[result.index('-map'):])
            inputs = [result[i + 1] for i, item in enumerate(result) if item == '-i']
            self.assertEqual(inputs[0], 'source.mp4')
            self.assertEqual(inputs[5], 'anullsrc')
            self.assertTrue(all(p.endswith('.nut') for p in inputs[1:5]))
            self.assertNotIn('-framerate', result)
            self.assertNotIn('libvpx-vp9', result)
            self.assertEqual(result.count('-stream_loop'), 4)
            graph = result[result.index('-filter_complex') + 1]
            self.assertIn('eval=frame', graph)
            self.assertIn('program_opencl', graph)

    def test_missing_cache_does_not_select_original_decoder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache, paths = self.setup_cache(root)
            (cache / (sha256_file(paths[0]) + '.json')).unlink()
            with self.assertRaises(FileNotFoundError):
                replace_asset_inputs(['ffmpeg', '-i', 'source', '-loop', '1', '-i', paths[0]], cache)

    def test_modified_cache_or_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache, paths = self.setup_cache(root)
            target = cached_path(paths[0], cache); target.write_bytes(b'bad')
            with self.assertRaises(ValueError): cached_path(paths[0], cache)
            Path(paths[1]).write_text('new asset')
            with self.assertRaises(FileNotFoundError): cached_path(paths[1], cache)

    def test_unconfigured_cache_keeps_original_inputs(self):
        command = ['ffmpeg', '-i', 'source']
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(replace_asset_inputs(command), command)


if __name__ == '__main__': unittest.main()
