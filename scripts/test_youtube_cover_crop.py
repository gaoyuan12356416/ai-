"""Offline pixel-preservation and runtime-to-review-asset regression tests."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from features.youtube_auto_publish.images import decode_cover, normalize_generated_cover
from features.youtube_auto_publish.runtime import generate_cover_factory
from features.youtube_auto_publish.service import YouTubeWorkflow
from features.youtube_auto_publish.templates import WorkflowError


def picture(size=(1672, 941), fmt='PNG'):
    image = Image.new('RGB', size, '#38658b')
    # Distinct edge markers catch wrong offsets, stretching or replacement pixels.
    for x, y, color in ((4, 2, 'red'), (size[0]-5, size[1]-4, 'green'),
                        (size[0]//2, size[1]//2, 'yellow')):
        image.putpixel((x, y), Image.new('RGB', (1, 1), color).getpixel((0, 0)))
    out = io.BytesIO(); image.save(out, format=fmt)
    return out.getvalue()


class CropCase(unittest.TestCase):
    def test_reported_native_size_keeps_all_selected_pixels(self):
        raw = picture()
        result, audit = normalize_generated_cover(raw)
        self.assertEqual(audit['source_size'], [1672, 941])
        self.assertEqual(audit['output_size'], [1664, 936])
        self.assertEqual(audit['removed_edges'], [4, 2, 4, 3])
        decoded = decode_cover(result, generated=True)
        self.assertEqual(decoded.width * 9, decoded.height * 16)
        expected = decode_cover(raw, generated=True).crop((4, 2, 1668, 938))
        self.assertEqual(decoded.tobytes(), expected.tobytes())
        self.assertEqual(audit['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(audit['output_sha256'], hashlib.sha256(result).hexdigest())

    def test_exact_ratio_is_unchanged_for_png_and_jpeg(self):
        for fmt in ('PNG', 'JPEG'):
            for size in ((1536, 864), (1920, 1080), (320, 180)):
                with self.subTest(fmt=fmt, size=size):
                    raw = picture(size, fmt)
                    result, audit = normalize_generated_cover(raw)
                    self.assertEqual(result, raw)
                    self.assertFalse(audit['cropped'])

    def test_small_wide_and_tall_deviations_center_crop(self):
        for size, edges in (((1936, 1080), [8, 0, 8, 0]), ((1920, 1090), [0, 5, 0, 5])):
            with self.subTest(size=size):
                result, audit = normalize_generated_cover(picture(size))
                self.assertEqual(audit['removed_edges'], edges)
                self.assertEqual(decode_cover(result, generated=True).size, (1920, 1080))

    def test_large_crop_or_non_horizontal_input_is_rejected(self):
        for size, code in (((335, 189), 'generated_cover_crop_excessive'),
                           ((640, 400), 'generated_cover_ratio'),
                           ((941, 1672), 'generated_cover_ratio'),
                           ((100, 100), 'generated_cover_dimensions')):
            with self.subTest(size=size), self.assertRaises(WorkflowError) as ctx:
                normalize_generated_cover(picture(size))
            self.assertEqual(ctx.exception.code, code)

    def test_corrupt_output_is_not_repaired_by_crop(self):
        with self.assertRaises(WorkflowError) as ctx:
            normalize_generated_cover(picture()[:-20])
        self.assertEqual(ctx.exception.code, 'generated_cover_corrupt')

    def test_existing_decoder_and_manual_upload_semantics_are_unchanged(self):
        raw = picture()
        self.assertEqual(decode_cover(raw, generated=True).size, (1672, 941))
        self.assertEqual(decode_cover(picture((640, 400))).size, (640, 400))

    def test_runtime_preserves_original_and_stores_exact_review_asset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); (root/'assets').mkdir()
            original = picture((240, 360), 'JPEG'); asset_id = 'a'*32
            (root/'assets'/(asset_id+'.jpg')).write_bytes(original)
            task = {'id': 'b'*32, 'material': {'macro_name': 'Test', 'macro_desc': 'Synopsis'},
                    'requirements': 'Keep title left and people right', 'versions': [],
                    'reference_cover': {'asset_id': asset_id, 'sha256': hashlib.sha256(original).hexdigest()}}
            raw = picture()
            def generate(command, **kwargs):
                work = Path(command[command.index('-C')+1])
                (work/'cover.png').write_bytes(raw)
                self.assertIn('always save the actual generated image', kwargs['input'])
                self.assertNotIn('exactly 1536x864 or 1920x1080', kwargs['input'])
                self.assertIn('Never alter PNG IHDR', kwargs['input'])
                return SimpleNamespace(returncode=0)
            with patch('features.youtube_auto_publish.runtime.subprocess.run', side_effect=generate):
                output = generate_cover_factory(root)(task, {'number': 1})
            work = next((root/'generation'/task['id']).iterdir())
            self.assertEqual((work/'cover.png').read_bytes(), raw)
            self.assertEqual((work/'cover-normalized.png').read_bytes(), output)
            audit = json.loads((work/'generation-output.json').read_text())
            self.assertEqual(audit['output_size'], [1664, 936])
            if os.name != 'nt':
                for name in ('cover-normalized.png', 'generation-output.json'):
                    self.assertEqual((work/name).stat().st_mode & 0o777, 0o600)
            service = YouTubeWorkflow(root/'jobs.sqlite3', root/'assets', Mock(), Mock(), Mock(), Mock())
            actor = {'tenant_key': 'test', 'user_id': 'test'}
            stored = service._store_image(actor, output, generated=True)
            asset = service.asset(actor, stored['id'])
            reviewed = decode_cover(Path(asset['path']).read_bytes(), generated=True)
            self.assertEqual(reviewed.size, (1664, 936))


if __name__ == '__main__':
    unittest.main(verbosity=2)
