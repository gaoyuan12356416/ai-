import concurrent.futures
import sys
import threading
import time
import types
import unittest
from unittest import mock

from features.tt_drama_resolver.service import (
    InvalidContentIdError,
    MySQLDramaRepository,
    ResolverUnavailableError,
    TTDramaResolver,
    TokenBucketRateLimiter,
    normalize_content_id,
    sanitize_cover_url,
)


class FakeClock:
    def __init__(self, value=0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def drama_item(content_id):
    return {
        "content_id": content_id,
        "title": "Example Drama",
        "description": "A short description.",
        "cover_url": "https://static-v1.mydramawave.com/cover.jpg",
        "country": "us",
        "language": "en",
        "episode_count": 80,
        "source_updated_at": "2026-07-27T00:00:00",
    }


class ResolverCacheTests(unittest.TestCase):
    def test_content_id_is_exact(self):
        self.assertEqual(normalize_content_id("l9rP6ey2CB"), "l9rP6ey2CB")
        for value in ("short", "l9rP6ey2CB!", " l9rP6ey2CB"):
            with self.assertRaises(InvalidContentIdError):
                normalize_content_id(value)

    def test_positive_and_negative_cache(self):
        clock = FakeClock()
        calls = []

        def loader(content_id):
            calls.append(content_id)
            return drama_item(content_id) if content_id.startswith("A") else None

        resolver = TTDramaResolver(
            loader,
            positive_ttl_seconds=10,
            negative_ttl_seconds=3,
            stale_ttl_seconds=30,
            max_entries=10,
            clock=clock,
        )
        first = resolver.resolve("AAAAAAAAAA")
        second = resolver.resolve("AAAAAAAAAA")
        self.assertTrue(first.found)
        self.assertEqual(first.cache_state, "MISS")
        self.assertEqual(second.cache_state, "HIT")
        self.assertEqual(calls, ["AAAAAAAAAA"])

        missing_first = resolver.resolve("BBBBBBBBBB")
        missing_second = resolver.resolve("BBBBBBBBBB")
        self.assertFalse(missing_first.found)
        self.assertEqual(missing_first.cache_state, "MISS")
        self.assertEqual(missing_second.cache_state, "NEGATIVE_HIT")
        self.assertEqual(calls.count("BBBBBBBBBB"), 1)
        clock.advance(4)
        resolver.resolve("BBBBBBBBBB")
        self.assertEqual(calls.count("BBBBBBBBBB"), 2)

    def test_source_error_is_not_negative_cached(self):
        calls = []

        def loader(content_id):
            calls.append(content_id)
            if len(calls) == 1:
                raise RuntimeError("database unavailable")
            return drama_item(content_id)

        resolver = TTDramaResolver(loader)
        with self.assertRaises(ResolverUnavailableError):
            resolver.resolve("AAAAAAAAAA")
        recovered = resolver.resolve("AAAAAAAAAA")
        self.assertTrue(recovered.found)
        self.assertEqual(len(calls), 2)

    def test_stale_positive_is_used_only_for_source_error(self):
        clock = FakeClock()
        fail = {"value": False}

        def loader(content_id):
            if fail["value"]:
                raise RuntimeError("database unavailable")
            return drama_item(content_id)

        resolver = TTDramaResolver(
            loader,
            positive_ttl_seconds=5,
            negative_ttl_seconds=2,
            stale_ttl_seconds=20,
            clock=clock,
        )
        resolver.resolve("AAAAAAAAAA")
        clock.advance(6)
        fail["value"] = True
        stale = resolver.resolve("AAAAAAAAAA")
        self.assertTrue(stale.found)
        self.assertEqual(stale.cache_state, "STALE")
        clock.advance(15)
        with self.assertRaises(ResolverUnavailableError):
            resolver.resolve("AAAAAAAAAA")

    def test_lru_capacity_is_bounded(self):
        calls = []

        def loader(content_id):
            calls.append(content_id)
            return drama_item(content_id)

        resolver = TTDramaResolver(loader, max_entries=2)
        resolver.resolve("AAAAAAAAAA")
        resolver.resolve("BBBBBBBBBB")
        resolver.resolve("AAAAAAAAAA")
        resolver.resolve("CCCCCCCCCC")
        resolver.resolve("BBBBBBBBBB")
        self.assertEqual(calls.count("BBBBBBBBBB"), 2)
        self.assertEqual(len(resolver._cache), 2)

    def test_same_key_concurrency_is_single_flight(self):
        calls = {"count": 0}
        lock = threading.Lock()

        def loader(content_id):
            with lock:
                calls["count"] += 1
            time.sleep(0.08)
            return drama_item(content_id)

        resolver = TTDramaResolver(loader, wait_timeout_seconds=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            outcomes = list(
                executor.map(lambda _index: resolver.resolve("AAAAAAAAAA"), range(20))
            )
        self.assertEqual(calls["count"], 1)
        self.assertTrue(all(outcome.found for outcome in outcomes))
        self.assertEqual(
            sum(outcome.cache_state == "MISS" for outcome in outcomes), 1
        )

    def test_same_key_source_failure_releases_all_followers(self):
        calls = {"count": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def loader(_content_id):
            with lock:
                calls["count"] += 1
            time.sleep(0.08)
            raise RuntimeError("database unavailable")

        def resolve_safely(_index):
            barrier.wait(timeout=2)
            try:
                resolver.resolve("AAAAAAAAAA")
            except Exception as exc:
                return type(exc)
            return None

        resolver = TTDramaResolver(loader, wait_timeout_seconds=2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            errors = list(executor.map(resolve_safely, range(20)))
        self.assertEqual(calls["count"], 1)
        self.assertTrue(
            all(error is ResolverUnavailableError for error in errors)
        )


class CoverAndRateLimitTests(unittest.TestCase):
    def test_cover_url_is_https_and_allowlisted(self):
        allowed = {"static-v1.mydramawave.com", "static-v2.mydramawave.com"}
        self.assertEqual(
            sanitize_cover_url(
                "https://static-v1.mydramawave.com/a/b.jpg?x=1#fragment",
                allowed,
            ),
            "https://static-v1.mydramawave.com/a/b.jpg?x=1",
        )
        self.assertEqual(
            sanitize_cover_url("http://static-v1.mydramawave.com/a.jpg", allowed),
            "",
        )
        self.assertEqual(
            sanitize_cover_url("https://evil.example/a.jpg", allowed), ""
        )
        self.assertEqual(
            sanitize_cover_url(
                "https://static-v1.mydramawave.com:not-a-port/a.jpg", allowed
            ),
            "",
        )

    def test_legacy_cover_host_is_normalized(self):
        self.assertEqual(
            sanitize_cover_url(
                "https://static.mydramawave.com/a.jpg",
                {"static.mydramawave.com"},
            ),
            "https://static-v1.mydramawave.com/a.jpg",
        )

    def test_token_bucket_refills(self):
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(
            limit_per_minute=2, max_keys=100, clock=clock
        )
        self.assertTrue(limiter.allow("client"))
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))
        clock.advance(30)
        self.assertTrue(limiter.allow("client"))


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        if self.connection.fail_queries:
            raise RuntimeError("query failed")
        self.connection.executions.append((sql, params))

    def fetchone(self):
        return dict(self.connection.row)

    def close(self):
        return None


class FakeConnection:
    def __init__(self, row, fail_queries=False):
        self.row = row
        self.executions = []
        self.closed = False
        self.fail_queries = fail_queries

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeRepository(MySQLDramaRepository):
    def __init__(self, row, max_concurrency=4):
        super().__init__(
            host="read-only.example",
            port=63350,
            user="reader",
            password="secret",
            database="kunlunads_dev",
            table="ads_drama_resource",
            app_id=1479,
            max_concurrency=max_concurrency,
            allowed_cover_hosts={"static-v1.mydramawave.com"},
        )
        self.row = row
        self.created = []

    def _create_connection(self):
        connection = FakeConnection(self.row)
        self.created.append(connection)
        return connection


class RepositoryTests(unittest.TestCase):
    def test_repository_warms_and_reuses_parameterized_connection(self):
        repository = FakeRepository(
            {
                "content_id": "l9rP6ey2CB",
                "title": "  Example   Drama ",
                "description": " A   description. ",
                "cover_url": "https://static-v1.mydramawave.com/cover.jpg",
                "country": "jp",
                "language": "ja",
                "episode_count": 150,
                "source_updated_at": "2026-07-27T00:00:00",
            }
        )
        self.assertTrue(repository.warmup())
        first = repository.lookup("l9rP6ey2CB")
        second = repository.lookup("l9rP6ey2CB")
        self.assertEqual(len(repository.created), 1)
        self.assertEqual(first, second)
        self.assertEqual(first["title"], "Example Drama")
        self.assertEqual(first["description"], "A description.")
        sql, params = repository.created[0].executions[0]
        self.assertNotIn("l9rP6ey2CB", sql)
        self.assertEqual(
            params,
            (
                "l9rP6ey2CB",
                "l9rP6ey2CB",
                "1479",
                "l9rP6ey2CB",
                "l9rP6ey2CB",
                "1479",
            ),
        )

    def test_repository_rejects_non_exact_canonical_content_id(self):
        repository = FakeRepository(
            {
                "content_id": "L9Rp6EY2cb",
                "title": "Wrong Case",
                "description": "Description",
                "cover_url": "",
                "country": "us",
                "language": "en",
                "episode_count": 80,
                "source_updated_at": "2026-07-27T00:00:00",
            }
        )
        self.assertIsNone(repository.lookup("l9rP6ey2CB"))

    def test_failed_reconnect_is_closed_and_not_returned_to_pool(self):
        repository = FakeRepository({})
        stale = FakeConnection({}, fail_queries=True)
        replacement = FakeConnection({}, fail_queries=True)
        repository._pool.put_nowait(stale)
        repository._create_connection = lambda: replacement

        with self.assertRaises(ResolverUnavailableError):
            repository.lookup("l9rP6ey2CB")

        self.assertTrue(stale.closed)
        self.assertTrue(replacement.closed)
        self.assertTrue(repository._pool.empty())

    def test_read_only_verification_failure_closes_new_connection(self):
        repository = MySQLDramaRepository(
            host="read-only.example",
            port=63350,
            user="reader",
            password="secret",
            database="kunlunads_dev",
            table="ads_drama_resource",
        )
        connection = FakeConnection({}, fail_queries=True)
        pymysql = types.SimpleNamespace(
            cursors=types.SimpleNamespace(DictCursor=object),
            connect=lambda **_kwargs: connection,
        )
        with mock.patch.dict(sys.modules, {"pymysql": pymysql}):
            with self.assertRaises(ResolverUnavailableError):
                repository._create_connection()
        self.assertTrue(connection.closed)

    def test_writable_endpoint_is_rejected_and_closed(self):
        repository = MySQLDramaRepository(
            host="writable.example",
            port=3306,
            user="reader",
            password="secret",
            database="kunlunads_dev",
            table="ads_drama_resource",
        )
        connection = FakeConnection({"read_only": 0})
        pymysql = types.SimpleNamespace(
            cursors=types.SimpleNamespace(DictCursor=object),
            connect=lambda **_kwargs: connection,
        )
        with mock.patch.dict(sys.modules, {"pymysql": pymysql}):
            with self.assertRaises(ResolverUnavailableError):
                repository._create_connection()
        self.assertTrue(connection.closed)

    def test_repository_bounds_different_key_query_concurrency(self):
        repository = FakeRepository(
            {
                "content_id": "AAAAAAAAAA",
                "title": "Example Drama",
                "description": "Description",
                "cover_url": "",
                "country": "us",
                "language": "en",
                "episode_count": 80,
                "source_updated_at": "2026-07-27T00:00:00",
            },
            max_concurrency=2,
        )
        active = {"value": 0, "maximum": 0}
        lock = threading.Lock()
        original_query = repository._query_on_connection

        def measured_query(connection, content_id):
            with lock:
                active["value"] += 1
                active["maximum"] = max(active["maximum"], active["value"])
            try:
                time.sleep(0.05)
                row = original_query(connection, content_id)
                if row:
                    row = dict(row)
                    row["content_id"] = content_id
                return row
            finally:
                with lock:
                    active["value"] -= 1

        repository._query_on_connection = measured_query
        content_ids = ["%010d" % index for index in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(repository.lookup, content_ids))
        self.assertTrue(all(result for result in results))
        self.assertLessEqual(active["maximum"], 2)
        self.assertEqual(active["maximum"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
