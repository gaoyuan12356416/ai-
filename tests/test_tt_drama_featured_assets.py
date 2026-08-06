import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock

from PIL import Image

from features.tt_drama_featured_assets import (
    AssetConfig,
    FeaturedAssetError,
    FeaturedAssetValidationError,
    ThumbnailBuildError,
    build_featured_assets,
    download_cover_bytes,
    encode_cover_webp,
    write_locale_snapshots,
)


THUMBNAIL_PATTERN = re.compile(
    r"^/tt-featured-covers/[a-f0-9]{64}\.webp$"
)


def jpeg_bytes(color=(120, 30, 90)):
    output = io.BytesIO()
    Image.new("RGB", (640, 960), color).save(
        output,
        format="JPEG",
        quality=88,
    )
    return output.getvalue()


def language_items(language):
    prefix = language.replace("-", "").upper()[:4]
    return [
        {
            "content_id": (prefix + ("%010d" % index))[:12],
            "title": "%s title %d" % (language, index),
            "cover_url": (
                "https://cdn.usrgrow.com/storage/covers/%s-%d.jpg"
                % (language, index)
            ),
            "language": language,
            "episode_count": 80 + index,
        }
        for index in range(5)
    ]


def bundle(rankings=None):
    return {
        "schema_version": 2,
        "source_date": "2026-08-05",
        "generated_at": "2026-08-06T15:30:00+08:00",
        "default_language": "en",
        "rankings": rankings or {
            "en": language_items("en"),
            "es": language_items("es"),
        },
    }


def write_bundle(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def config_for(root, value=None, workers=2):
    root = Path(root)
    source = root / "current-by-language.json"
    locale_dir = root / "by-language"
    cover_dir = root / "covers"
    locale_dir.mkdir()
    cover_dir.mkdir()
    write_bundle(source, value or bundle())
    return AssetConfig(
        input_path=source,
        locale_output_dir=locale_dir,
        cover_output_dir=cover_dir,
        workers=workers,
    )


class FeaturedAssetTests(unittest.TestCase):
    def test_success_builds_strict_schema3_and_exact_webp(self):
        source_image = jpeg_bytes()

        def downloader(_url, **_kwargs):
            return source_image

        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            result = build_featured_assets(config, downloader=downloader)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["language_count"], 2)
            self.assertEqual(result["thumbnail_success_count"], 10)
            self.assertEqual(result["thumbnail_failure_count"], 0)
            self.assertEqual(
                sorted(path.name for path in config.locale_output_dir.iterdir()),
                ["en.json", "es.json"],
            )
            locale = json.loads(
                (config.locale_output_dir / "en.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(locale),
                {
                    "schema_version",
                    "source_date",
                    "generated_at",
                    "language",
                    "items",
                },
            )
            self.assertEqual(locale["schema_version"], 3)
            self.assertEqual(locale["language"], "en")
            self.assertEqual(len(locale["items"]), 5)
            self.assertNotIn(
                "spend",
                json.dumps(locale, ensure_ascii=False).lower(),
            )
            for item in locale["items"]:
                self.assertEqual(
                    set(item),
                    {
                        "content_id",
                        "title",
                        "cover_url",
                        "thumbnail_url",
                        "language",
                        "episode_count",
                    },
                )
                self.assertRegex(item["thumbnail_url"], THUMBNAIL_PATTERN)

            covers = list(config.cover_output_dir.glob("*.webp"))
            self.assertEqual(len(covers), 1)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(covers[0].stat().st_mode), 0o644)
            with Image.open(covers[0]) as image:
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (236, 338))

    def test_one_cover_failure_is_best_effort_and_keeps_original_url(self):
        source_image = jpeg_bytes((15, 90, 120))
        failing_url = language_items("en")[0]["cover_url"]

        def downloader(url, **_kwargs):
            if url == failing_url:
                raise ThumbnailBuildError("controlled failure")
            return source_image

        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory, bundle({"en": language_items("en")}))
            result = build_featured_assets(config, downloader=downloader)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["thumbnail_failure_count"], 1)
            locale = json.loads(
                (config.locale_output_dir / "en.json").read_text(encoding="utf-8")
            )
            failed = next(
                item for item in locale["items"] if item["cover_url"] == failing_url
            )
            self.assertEqual(failed["thumbnail_url"], "")
            self.assertEqual(failed["cover_url"], failing_url)
            self.assertTrue(
                all(
                    item["thumbnail_url"]
                    for item in locale["items"]
                    if item["cover_url"] != failing_url
                )
            )

    def test_runtime_or_total_thumbnail_failure_preserves_locale_lkg(self):
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory)
            lkg = config.locale_output_dir / "en.json"
            lkg.write_text("previous-lkg\n", encoding="utf-8")
            with mock.patch(
                "features.tt_drama_featured_assets.service."
                "validate_image_runtime",
                side_effect=FeaturedAssetError("WebP unavailable"),
            ):
                with self.assertRaisesRegex(FeaturedAssetError, "WebP"):
                    build_featured_assets(config)
            self.assertEqual(lkg.read_text(encoding="utf-8"), "previous-lkg\n")

            def fail_all(_url, **_kwargs):
                raise ThumbnailBuildError("network unavailable")

            with self.assertRaisesRegex(FeaturedAssetError, "all featured"):
                build_featured_assets(config, downloader=fail_all)
            self.assertEqual(lkg.read_text(encoding="utf-8"), "previous-lkg\n")

    def test_invalid_private_spend_input_preserves_last_known_good(self):
        invalid = bundle({"en": language_items("en")})
        invalid["rankings"]["en"][0]["spend"] = 99
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory, invalid)
            target = config.locale_output_dir / "en.json"
            target.write_bytes(b"last-known-good\n")
            before = target.read_bytes()
            with self.assertRaisesRegex(
                FeaturedAssetValidationError,
                "fields|spend",
            ):
                build_featured_assets(
                    config,
                    downloader=lambda *_args, **_kwargs: jpeg_bytes(),
                )
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(config.cover_output_dir.iterdir()), [])

    def test_atomic_replace_failure_preserves_existing_locale(self):
        items = [
            {**item, "thumbnail_url": ""}
            for item in language_items("en")
        ]
        snapshot = {
            "schema_version": 3,
            "source_date": "2026-08-05",
            "generated_at": "2026-08-06T15:30:00+08:00",
            "language": "en",
            "items": items,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            target = output / "en.json"
            target.write_bytes(b"previous\n")
            with mock.patch(
                "features.tt_drama_featured_assets.service.os.replace",
                side_effect=OSError("controlled"),
            ):
                with self.assertRaises(FeaturedAssetError):
                    write_locale_snapshots(output, {"en": snapshot})
            self.assertEqual(target.read_bytes(), b"previous\n")
            self.assertFalse(any(output.glob("*.tmp")))

    def test_stale_owned_locale_file_is_pruned_only_after_valid_write(self):
        snapshots = {}
        for language in ("en", "es"):
            snapshots[language] = {
                "schema_version": 3,
                "source_date": "2026-08-05",
                "generated_at": "2026-08-06T15:30:00+08:00",
                "language": language,
                "items": [
                    {**item, "thumbnail_url": ""}
                    for item in language_items(language)
                ],
            }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "fr.json").write_text("stale", encoding="utf-8")
            (output / "operator-note.txt").write_text("keep", encoding="utf-8")
            result = write_locale_snapshots(output, snapshots)
            self.assertEqual(result["pruned"], 1)
            self.assertFalse((output / "fr.json").exists())
            self.assertTrue((output / "operator-note.txt").exists())

    def test_download_revalidates_final_url_and_rejects_other_host(self):
        class Response:
            status = 200
            headers = {
                "Content-Type": "image/jpeg",
                "Content-Length": "12",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def getcode(self):
                return 200

            def geturl(self):
                return "https://evil.example/cover.jpg"

            def read(self, _limit):
                return b"not-an-image"

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        with self.assertRaisesRegex(ThumbnailBuildError, "not allowlisted"):
            download_cover_bytes(
                "https://cdn.usrgrow.com/storage/cover.jpg",
                opener=Opener(),
            )

    def test_language_path_traversal_is_rejected_before_any_write(self):
        invalid = bundle({"../en": language_items("en")})
        for item in invalid["rankings"]["../en"]:
            item["language"] = "../en"
        with tempfile.TemporaryDirectory() as directory:
            config = config_for(directory, invalid)
            with self.assertRaisesRegex(
                FeaturedAssetValidationError,
                "language",
            ):
                build_featured_assets(
                    config,
                    downloader=lambda *_args, **_kwargs: jpeg_bytes(),
                )
            self.assertEqual(list(config.locale_output_dir.iterdir()), [])

    def test_systemd_and_hashed_requirement_contract(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "deploy" / "tt-drama-featured-assets.service").read_text(
            encoding="utf-8"
        )
        path_unit = (root / "deploy" / "tt-drama-featured-assets.path").read_text(
            encoding="utf-8"
        )
        requirements = (
            root / "deploy" / "tt-drama-featured-assets.requirements.txt"
        ).read_text(encoding="utf-8")
        fixed_python = (
            "/mnt/data-disk/tt-code-performance/"
            "venv-pillow-11.3.0/bin/python"
        )
        self.assertIn("User=tt-drama-featured", service)
        self.assertIn("Group=tt-drama-featured", service)
        self.assertIn("ExecStart=" + fixed_python, service)
        self.assertIn(
            "WorkingDirectory=/mnt/data-disk/tt-code-performance/current",
            service,
        )
        self.assertIn(
            "/mnt/data-disk/tt-code-performance/current/scripts/"
            "refresh_tt_drama_featured_assets.py",
            service,
        )
        self.assertNotIn("tt-drama-resource-cache/current", service)
        self.assertIn("ExecStartPre=+/usr/bin/install -d -m 0755", service)
        self.assertNotIn("tt-drama-featured.service", service)
        self.assertIn(
            "PathChanged=/mnt/data-disk/tt-drama-featured/public/"
            "current-by-language.json",
            path_unit,
        )
        self.assertIn("Unit=tt-drama-featured-assets.service", path_unit)
        self.assertIn("--require-hashes", requirements)
        self.assertIn("Pillow==11.3.0", requirements)
        self.assertIn(
            "6359a3bc43f57d5b375d1ad54a0074318a0844d11b76abccf478c37c986d3cfc",
            requirements,
        )
        self.assertIn(
            "cadc9e0ea0a2431124cde7e1697106471fc4c1da01530e679b2391c37d3fbb3a",
            requirements,
        )

    def test_encoder_rejects_pixel_limit(self):
        with self.assertRaisesRegex(ThumbnailBuildError, "dimensions|conversion"):
            encode_cover_webp(jpeg_bytes(), maximum_pixels=100_000)


if __name__ == "__main__":
    unittest.main()
