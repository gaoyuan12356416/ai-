import sqlite3
import unittest

from features.pipeline_dispatch import media_decision as media


SOURCE = (
    "https://advertising-1306474899.cos.ap-hongkong.myqcloud.com/custom/source/v1/a.mp4"
)
NOW = "2026-11-19T07:00:00.000+00:00"


class PreparedMediaTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        media.ensure_schema(self.c)
        self.addCleanup(self.c.close)

    def job(self, channel, number, source=SOURCE, mode="random_overlay", **options):
        j = {"source_key": f"{channel}:queue:{number}", "channel": channel}
        c = media.build_context(source, mode=mode, **options)
        media.register_context(self.c, j["source_key"], channel, c)
        return j

    def produce(
        self, job, digit, profile="tt-post-random-overlay-h264-720x1280-v3", **updates
    ):
        a = {
            "profile": profile,
            "url": f"https://files.example/{digit}.mp4",
            "sha256": digit * 64,
            "size": 50000,
            "duration_seconds": 60,
            "mime": "video/mp4",
            **updates,
        }
        return media.record_artifact(self.c, job["source_key"], job["channel"], a, NOW)

    def pick(self, job, due=False, **kwargs):
        return media.select(self.c, job, at_cutoff=due, now=NOW, **kwargs)

    def test_variant_use_counted_for_destination_platform_and_original_producer(self):
        fa = self.job("fb", 1)
        self.assertTrue(self.produce(fa, "a"))
        self.assertIsNone(self.pick(self.job("fb", 2)))
        x = self.job("x", 1)
        chosen = self.pick(x)
        self.assertEqual(chosen["producer_source_key"], fa["source_key"])
        self.assertEqual(self.pick(x), chosen)
        self.assertIsNone(self.pick(self.job("x", 2)))
        self.assertFalse(self.produce(x, "a"))
        xb = self.job("x", 3)
        self.assertTrue(self.produce(xb, "b"))
        fb = self.job("fb", 3)
        self.assertEqual(self.pick(fb)["artifact"]["sha256"], "b" * 64)
        self.assertIsNone(self.pick(self.job("fb", 4)))
        self.assertEqual(self.pick(self.job("fb", 5), True)["kind"], "deadline_reuse")

    def test_cutoff_only_uses_same_platform_else_original(self):
        f = self.job("fb", 1)
        self.assertTrue(self.produce(f, "a"))
        self.assertEqual(self.pick(self.job("x", 2), True)["kind"], "deadline_original")
        f_next = self.job("fb", 3, mode="direct_clean", accepted_codecs=["h264"])
        self.assertIsNone(self.pick(f_next))
        self.assertEqual(
            self.pick(f_next, True)["producer_source_key"], f["source_key"]
        )
        self.assertEqual(
            self.pick(self.job("fb", 4), True, excluded=["a" * 64])["kind"],
            "deadline_original",
        )

    def test_same_verified_source_only_and_compatible_output(self):
        good = self.job("fb", 1, source_sha256="a" * 64)
        self.produce(good, "1", profile="tt-post-random-overlay-hevc-720x1280-v3")
        self.assertIsNone(self.pick(self.job("x", 2, accepted_codecs=["h264"])))
        self.assertIsNone(self.pick(self.job("tt", 3, source_sha256="b" * 64)))
        self.assertIsNone(
            self.pick(self.job("tt", 4, contract={"trim_tail_seconds": 4.0}))
        )
        self.assertIsNone(self.pick(self.job("tt", 5, source=SOURCE + "?versionId=2")))
        self.assertIsNone(self.pick(self.job("tt", 6, source=SOURCE, max_bytes=100)))
        d = self.pick(
            self.job(
                "tt",
                7,
                source=SOURCE.replace(
                    "advertising-1306474899.cos.ap-hongkong.myqcloud.com",
                    "ads-cdn.yingliang.tech",
                )
                + "?q-signature=12345",
            )
        )
        self.assertEqual(d["producer_source_key"], good["source_key"])
        other = self.job(
            "tt", 8, source="https://elsewhere.example/b.mp4", source_sha256="a" * 64
        )
        self.assertIsNone(self.pick(other))
        media.release(self.c, "tt:queue:7")
        self.assertIsNotNone(self.pick(other))

    def test_no_raw_or_invalid_output_enters_index(self):
        j = self.job("fb", 1)
        self.assertFalse(self.produce(j, "b", profile="source"))
        self.assertFalse(self.produce(j, "b", mime="image/jpeg"))
        self.assertFalse(
            self.produce(j, "b", url="https://example.test/a?token=secret")
        )
        self.assertEqual(
            self.pick(self.job("fb", 2), True)["kind"], "deadline_original"
        )

    def test_schema_and_reservation_rollback_is_atomic(self):
        self.c.commit()
        self.c.execute("BEGIN IMMEDIATE")
        media.ensure_schema(self.c)
        f = self.job("fb", 1)
        self.produce(f, "a")
        self.assertIsNotNone(self.pick(self.job("tt", 1)))
        self.c.rollback()
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM pipeline_media_use").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
