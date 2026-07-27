import concurrent.futures
from pathlib import Path
import tempfile
import threading
import time
import unittest

from features.tt_drama_resources import (
    ResourceContentMismatchError,
    ResourceSourceError,
    ResourceStorageError,
    SQLiteResourceCache,
    W2AResourceService,
)


CONTENT_ID = "Ag0rfr5F0F"


class _Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class _Client:
    landing_id = 2049

    def __init__(self):
        self.calls = 0
        self.title = "Her Beast"
        self.error = None
        self.delay = 0
        self.lock = threading.Lock()

    def fetch(self, content_id):
        with self.lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return {
            "landing_id": 2049,
            "content_id": content_id,
            "resolved_content_id": content_id,
            "title": self.title,
            "description": "Description",
            "cover_url": "https://cdn.usrgrow.com/cover.jpg",
            "content_hash": "b" * 64,
            "country": "",
            "language": "",
            "episode_count": 0,
        }


class W2AResourceServiceTests(unittest.TestCase):
    def make_service(self, directory, clock=None, client=None, **kwargs):
        clock = clock or _Clock()
        cache = SQLiteResourceCache(
            Path(directory) / "resources.sqlite3",
            clock=clock,
            allow_test_path=True,
        )
        service = W2AResourceService(
            cache=cache,
            client=client or _Client(),
            clock=clock,
            positive_ttl_seconds=10,
            negative_ttl_seconds=5,
            stale_ttl_seconds=100,
            **kwargs,
        )
        service.warmup()
        return service

    def test_origin_fill_then_disk_hit_with_compatible_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _Client()
            service = self.make_service(directory, client=client)
            first = service.resolve(CONTENT_ID)
            second = service.resolve(CONTENT_ID)
            self.assertEqual(first.cache_state, "ORIGIN_FILL")
            self.assertEqual(second.cache_state, "DISK_HIT")
            self.assertEqual(client.calls, 1)
            self.assertEqual(second.item["country"], "")
            self.assertEqual(second.item["language"], "")
            self.assertEqual(second.item["episode_count"], 0)
            self.assertEqual(
                second.item["source_updated_at"],
                second.item["fetched_at"],
            )

    def test_expired_positive_refreshes_and_source_error_uses_stale(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            client = _Client()
            service = self.make_service(directory, clock=clock, client=client)
            service.resolve(CONTENT_ID)
            clock.value += 11
            client.title = "Updated"
            refreshed = service.resolve(CONTENT_ID)
            self.assertEqual(refreshed.cache_state, "ORIGIN_FILL")
            self.assertEqual(refreshed.item["title"], "Updated")
            self.assertEqual(client.calls, 2)

            clock.value += 11
            client.error = ResourceSourceError("injected")
            stale = service.resolve(CONTENT_ID)
            self.assertEqual(stale.cache_state, "STALE")
            self.assertEqual(stale.item["title"], "Updated")

    def test_mismatch_is_negative_cached_not_source_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _Client()
            client.error = ResourceContentMismatchError(
                CONTENT_ID,
                "Different1",
            )
            service = self.make_service(directory, client=client)
            first = service.resolve(CONTENT_ID)
            second = service.resolve(CONTENT_ID)
            self.assertFalse(first.found)
            self.assertEqual(first.cache_state, "NEGATIVE_FILL")
            self.assertFalse(second.found)
            self.assertEqual(second.cache_state, "NEGATIVE_HIT")
            self.assertEqual(client.calls, 1)

    def test_transient_source_error_does_not_create_negative_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _Client()
            client.error = ResourceSourceError("injected")
            service = self.make_service(directory, client=client)
            with self.assertRaises(ResourceSourceError):
                service.resolve(CONTENT_ID)
            client.error = None
            outcome = service.resolve(CONTENT_ID)
            self.assertTrue(outcome.found)
            self.assertEqual(client.calls, 2)

    def test_per_id_single_flight_fetches_source_once(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _Client()
            client.delay = 0.08
            service = self.make_service(
                directory,
                client=client,
                wait_timeout_seconds=2,
            )
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=8
            ) as executor:
                outcomes = list(
                    executor.map(
                        service.resolve,
                        [CONTENT_ID] * 8,
                    )
                )
            self.assertEqual(client.calls, 1)
            self.assertTrue(all(outcome.found for outcome in outcomes))
            self.assertEqual(
                sum(
                    outcome.cache_state == "ORIGIN_FILL"
                    for outcome in outcomes
                ),
                1,
            )

    def test_force_refresh_bypasses_fresh_positive_and_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _Client()
            service = self.make_service(directory, client=client)
            service.resolve(CONTENT_ID)
            client.title = "Forced"
            refreshed = service.resolve(CONTENT_ID, force_refresh=True)
            self.assertEqual(refreshed.item["title"], "Forced")
            self.assertEqual(client.calls, 2)

    def test_close_is_explicit_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(directory)
            service.close()
            with self.assertRaisesRegex(ResourceStorageError, "closed"):
                service.resolve(CONTENT_ID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
