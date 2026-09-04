"""Real image fixtures: legacy JPEG, color conversion, corruption and isolation."""
import ast
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageCms
from features.drama_synthesis import intro_cover
from features.drama_synthesis.async_runtime import safe_error
from features.drama_synthesis.remote_client import _safe_error
from features.drama_synthesis.core import DramaSynthesisError
from features.drama_synthesis.local_checkpoint import file_fingerprint


class IntroCoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        names = {"freeze_intro_cover_source", "validate_intro_cover_color_contract"}
        cls.env = dict(os=os, tempfile=tempfile, shutil=shutil, file_fingerprint=file_fingerprint)
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names],
                                type_ignores=[]), "app-intro", "exec"), cls.env)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def image(self, mode="RGB", fmt="JPEG", **options):
        path = self.root / "input.jpg"  # detect bytes, not the extension
        Image.new(mode, (32, 18), 80).save(path, fmt, **options)
        return path

    def freeze(self, path):
        original = path.read_bytes()
        frozen, fingerprint, color = self.env["freeze_intro_cover_source"](str(path), str(self.root))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(fingerprint, file_fingerprint(frozen))
        self.assertEqual(color["transfer"], "iec61966-2-1")
        with Image.open(frozen) as result:
            result.load()
            self.assertEqual(result.mode, "RGB")
            self.assertIn("jfif", result.info)
            self.assertNotIn("icc_profile", result.info)
        return frozen

    def test_ffmpeg_style_jpeg_without_jfif_and_retry_preserve_source(self):
        path = self.image()
        data = path.read_bytes()
        self.assertEqual(data[2:4], b"\xff\xe0")
        path.write_bytes(data[:2] + data[4 + int.from_bytes(data[4:6], "big"):])
        with self.assertRaises(RuntimeError):
            self.env["validate_intro_cover_color_contract"](str(path))
        first, second = self.freeze(path), self.freeze(path)
        self.assertEqual(Path(first).read_bytes(), Path(second).read_bytes())

    def test_jfif_grayscale_png_and_webp(self):
        for mode, fmt in [("RGB", "JPEG"), ("L", "JPEG"), ("RGB", "PNG"), ("RGB", "WEBP")]:
            with self.subTest(mode=mode, fmt=fmt):
                self.freeze(self.image(mode, fmt))

    def test_valid_icc_is_converted_and_removed(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        self.freeze(self.image(icc_profile=profile))

    def test_alpha_composited_white(self):
        path = self.root / "transparent.jpg"
        Image.new("RGBA", (32, 18), (255, 0, 0, 0)).save(path, "PNG")
        with Image.open(self.freeze(path)) as result:
            self.assertEqual(result.getpixel((0, 0)), (255, 255, 255))

    def test_exif_orientation_applied(self):
        exif = Image.Exif(); exif[274] = 6
        with Image.open(self.freeze(self.image(exif=exif))) as result:
            self.assertEqual(result.size, (18, 32))
            self.assertFalse(result.getexif())

    def test_corrupt_truncated_ambiguous_and_invalid_icc_fail_with_no_residue(self):
        for case in ["corrupt", "truncated", "cmyk", "icc"]:
            with self.subTest(case=case):
                path = self.image("CMYK" if case == "cmyk" else "RGB",
                                  **({"icc_profile": b"invalid-profile"} if case == "icc" else {}))
                if case == "corrupt": path.write_bytes(b"not an image")
                if case == "truncated": path.write_bytes(path.read_bytes()[:-20])
                original = path.read_bytes()
                with self.assertRaises(DramaSynthesisError) as caught:
                    self.env["freeze_intro_cover_source"](str(path), str(self.root))
                expected = "drama_intro_cover_color_unsupported" if case in {"cmyk", "icc"} else "drama_intro_cover_invalid"
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(_safe_error(safe_error(caught.exception))["code"], expected)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(list(self.root.glob(".intro-cover-*")))

    def test_size_limits(self):
        for setting in ["MAX_COVER_BYTES", "MAX_COVER_PIXELS"]:
            with self.subTest(setting=setting), mock.patch.object(intro_cover, setting, 1):
                with self.assertRaises(DramaSynthesisError): self.freeze(self.image())

    def test_source_changed_during_copy(self):
        path = self.image()
        real = shutil.copyfileobj
        def change(source, target, **kwargs):
            real(source, target, **kwargs)
            path.write_bytes(b"changed")
        with mock.patch.object(shutil, "copyfileobj", side_effect=change):
            with self.assertRaises(DramaSynthesisError) as caught:
                self.env["freeze_intro_cover_source"](str(path), str(self.root))
        self.assertEqual(caught.exception.code, "drama_intro_cover_source_changed")
        self.assertFalse(list(self.root.glob(".intro-cover-*")))


if __name__ == "__main__":
    unittest.main()
