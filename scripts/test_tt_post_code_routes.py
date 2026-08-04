import json
import contextlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from features.tt_posts.code_routes import (
    TTCodeRouteError,
    TTCodeRouteResolver,
    allocate_code_route,
    ensure_code_route_storage,
)
from features.tt_posts.core import (
    TTPostError,
    ensure_storage,
    render_caption_template,
)
from features.tt_posts.links import build_w2a_url
from features.tt_posts.service import TTPostHTTPServer


INTERNAL_TOKEN = "test-internal-token-that-is-long-enough-123456"


def tracking_url(queue_id, content_id="DRAMA100", channel="TT"):
    return build_w2a_url(
        {
            "username": "creator_101",
            "timestamp": 1784736000 + int(queue_id),
            "material_language": "en",
            "drama_name": "A Bride & A Contract",
            "tag": "romance",
            "link_id": queue_id,
            "page_name": "DramaWave Reels",
            "page_id": "101",
            "material_name": "Episode %s" % queue_id,
            "material_id": str(9000 + int(queue_id)),
            "queue_id": queue_id,
            "content_id": content_id,
            "channel": channel,
        }
    )


def route_values(queue_id, *, content_id="DRAMA100", created_at=None):
    target = tracking_url(queue_id, content_id)
    query = dict(
        urllib.parse.parse_qsl(
            urllib.parse.urlsplit(target).query,
            keep_blank_values=True,
        )
    )
    timestamp = created_at or "2026-08-04T00:00:%02dZ" % int(queue_id)
    return {
        "content_id": content_id,
        "c": query["c"],
        "af_adset": query["af_adset"],
        "af_adset_id": query["af_adset_id"],
        "af_ad": query["af_ad"],
        "af_ad_id": query["af_ad_id"],
        "af_channel": "TT",
        "af_c_id": query["af_c_id"],
        "long_url": target,
        "state": "scheduled",
        "created_at": timestamp,
        "published_at": "",
        "updated_at": timestamp,
    }


class FakeRedis:
    def __init__(self, *, fail=False, fail_delete=False):
        self.values = {}
        self.ttls = {}
        self.fail = fail
        self.fail_delete = fail_delete
        self.get_calls = 0
        self.set_calls = 0

    def get(self, key):
        self.get_calls += 1
        if self.fail:
            raise OSError("redis unavailable")
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.set_calls += 1
        if self.fail:
            raise OSError("redis unavailable")
        self.values[key] = value
        self.ttls[key] = ttl

    def delete(self, key):
        if self.fail_delete or self.fail:
            raise OSError("redis unavailable")
        self.values.pop(key, None)


class CodeRouteCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "tt-post.sqlite3"
        ensure_storage(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def allocate(self, queue_id, **kwargs):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return allocate_code_route(
                conn,
                queue_id,
                route_values(queue_id),
                **kwargs,
            )

    def reset_code_table(self, code_length):
        with self.connection() as conn:
            conn.execute("DROP TRIGGER IF EXISTS trg_tt_post_queue_code_route_state")
            conn.execute("DROP TABLE tt_post_code_route")
            ensure_code_route_storage(conn, code_length=code_length)


class AllocatorTests(CodeRouteCase):
    def test_collision_falls_back_and_queue_is_idempotent(self):
        self.reset_code_table(2)
        always_a = lambda _alphabet: "A"
        first = self.allocate(
            1,
            alphabet="AB",
            code_length=2,
            choice_fn=always_a,
        )
        replay = self.allocate(
            1,
            alphabet="AB",
            code_length=2,
            choice_fn=always_a,
        )
        second = self.allocate(
            2,
            alphabet="AB",
            code_length=2,
            choice_fn=always_a,
        )
        self.assertEqual("AA", first["code"])
        self.assertEqual(first["code"], replay["code"])
        self.assertEqual("AB", second["code"])
        with self.connection() as conn:
            self.assertEqual(
                2,
                conn.execute("SELECT COUNT(*) FROM tt_post_code_route").fetchone()[0],
            )

    def test_full_small_space_recycles_oldest_with_code_tiebreak(self):
        self.reset_code_table(1)
        always_a = lambda _alphabet: "A"
        created = "2026-08-04T00:00:00Z"
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            first = allocate_code_route(
                conn,
                1,
                route_values(1, created_at=created),
                alphabet="AB",
                code_length=1,
                choice_fn=always_a,
            )
            second = allocate_code_route(
                conn,
                2,
                route_values(2, created_at=created),
                alphabet="AB",
                code_length=1,
                choice_fn=always_a,
            )
            recycled = allocate_code_route(
                conn,
                3,
                route_values(3, created_at="2026-08-04T00:01:00Z"),
                alphabet="AB",
                code_length=1,
                choice_fn=always_a,
            )
        self.assertEqual("A", first["code"])
        self.assertEqual("B", second["code"])
        self.assertEqual("A", recycled["code"])
        with self.connection() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM tt_post_code_route WHERE queue_id=1"
                ).fetchone()
            )

    def test_database_rejects_lowercase_semantic_duplicate(self):
        allocated = self.allocate(1)
        with self.connection() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE tt_post_code_route SET code=? WHERE code=?",
                    (allocated["code"].lower(), allocated["code"]),
                )

    def test_concurrent_same_queue_returns_one_code(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(3)
                results.append(self.allocate(7)["code"])
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _item in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len(set(results)))


class CaptionCodeTests(unittest.TestCase):
    def test_exact_code_macro_renders_and_can_be_deferred(self):
        template = "Drama ID: {{content_id}}\nCode: {code}"
        self.assertEqual(
            "Drama ID: DRAMA100\nCode: AB12",
            render_caption_template(template, "DRAMA100", code="AB12"),
        )
        self.assertEqual(
            template.replace("{{content_id}}", "DRAMA100"),
            render_caption_template(template, "DRAMA100", defer_code=True),
        )

    def test_code_macro_rejects_lowercase_and_unknown_macro(self):
        with self.assertRaises(TTPostError) as lowercase:
            render_caption_template(
                "Drama {{content_id}} {code}",
                "DRAMA100",
                code="ab12",
            )
        self.assertEqual("caption_code_required", lowercase.exception.code)
        with self.assertRaises(TTPostError) as unknown:
            render_caption_template(
                "Drama {{content_id}} {Code}",
                "DRAMA100",
                code="AB12",
            )
        self.assertEqual("caption_placeholder_invalid", unknown.exception.code)


class ResolverTests(CodeRouteCase):
    def test_code_exact_latest_published_clone_and_generic_fallback(self):
        old = self.allocate(1)
        latest = self.allocate(2)
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE tt_post_code_route
                SET state='published',published_at='2026-08-04T01:00:00Z'
                WHERE code=?
                """,
                (old["code"],),
            )
            conn.execute(
                """
                UPDATE tt_post_code_route
                SET state='published',published_at='2026-08-04T02:00:00Z'
                WHERE code=?
                """,
                (latest["code"],),
            )
        resolver = TTCodeRouteResolver(self.db_path)

        exact = resolver.resolve(latest["code"].lower(), "Search")["item"]
        self.assertEqual("code_exact", exact["route_mode"])
        self.assertEqual("TT", exact["af_channel"])
        exact_query = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(exact["target_url"]).query)
        )
        self.assertEqual("TT", exact_query["af_channel"])

        cloned = resolver.resolve("DRAMA100", "Featured")["item"]
        self.assertEqual("published_clone", cloned["route_mode"])
        self.assertEqual(latest["code"], cloned["code"])
        cloned_query = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(cloned["target_url"]).query)
        )
        self.assertEqual("Featured", cloned_query["af_channel"])
        self.assertEqual(latest["c"], cloned_query["c"])

        fallback = resolver.resolve("NO_HISTORY_9", "Search")["item"]
        self.assertEqual("generic_fallback", fallback["route_mode"])
        fallback_query = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(fallback["target_url"]).query)
        )
        self.assertEqual(
            {
                "af_dp": "NO_HISTORY_9",
                "c": "TTpost",
                "af_c_id": "0001",
                "af_channel": "Search",
            },
            fallback_query,
        )

    def test_four_character_code_miss_is_404(self):
        resolver = TTCodeRouteResolver(self.db_path)
        with self.assertRaises(TTCodeRouteError) as caught:
            resolver.resolve("ab12", "Search")
        self.assertEqual(404, caught.exception.status)
        self.assertEqual("tt_code_not_found", caught.exception.code)

    def test_redis_positive_negative_and_failure_fallback(self):
        row = self.allocate(1)
        redis = FakeRedis()
        resolver = TTCodeRouteResolver(
            self.db_path,
            redis_client=redis,
            cache_namespace="test",
        )
        resolver.resolve(row["code"], "Search")
        first_get_count = redis.get_calls
        resolver.resolve(row["code"], "Search")
        self.assertGreater(redis.set_calls, 0)
        self.assertGreater(redis.get_calls, first_get_count)
        self.assertIn(24 * 60 * 60, redis.ttls.values())
        with self.assertRaises(TTCodeRouteError):
            resolver.resolve("ZZZZ", "Search")
        negative_keys = [
            key
            for key, raw in redis.values.items()
            if json.loads(raw) == {"missing": True}
        ]
        self.assertTrue(negative_keys)
        self.assertTrue(all(redis.ttls[key] == 30 for key in negative_keys))

        unavailable = TTCodeRouteResolver(
            self.db_path,
            redis_client=FakeRedis(fail=True),
        )
        self.assertEqual(
            row["code"],
            unavailable.resolve(row["code"], "Featured")["item"]["code"],
        )

    def test_malformed_or_identity_mismatched_cache_falls_back_to_sqlite(self):
        row = self.allocate(1)
        redis = FakeRedis()
        resolver = TTCodeRouteResolver(
            self.db_path,
            redis_client=redis,
            cache_namespace="test",
        )
        key = resolver._key("code", row["code"])
        redis.values[key] = json.dumps({"long_url": "https://invalid.example/"})
        first = resolver.resolve(row["code"], "Search")["item"]
        self.assertEqual(row["content_id"], first["content_id"])

        poisoned = dict(row)
        poisoned["content_id"] = "OTHER_DRAMA"
        redis.values[key] = json.dumps(poisoned)
        second = resolver.resolve(row["code"], "Featured")["item"]
        self.assertEqual(row["content_id"], second["content_id"])
        self.assertEqual("code_exact", second["route_mode"])

    def test_delete_failure_rotates_process_namespace(self):
        redis = FakeRedis(fail_delete=True)
        resolver = TTCodeRouteResolver(
            self.db_path,
            redis_client=redis,
            cache_namespace="before",
        )
        resolver.invalidate_code("AB12")
        after_code = resolver.cache_namespace
        self.assertNotEqual("before", after_code)
        resolver.invalidate_latest("DRAMA100")
        self.assertNotEqual(after_code, resolver.cache_namespace)


class PublicHTTPTests(unittest.TestCase):
    class Facade:
        class Gates:
            @staticmethod
            def as_dict():
                return {}

        gates = Gates()

        @staticmethod
        def resolve_code_route(query, source):
            return {
                "found": True,
                "item": {
                    "content_id": query,
                    "target_url": "https://www.dramawavew2a.com/ads/101/2250/view",
                    "query_type": "content_id",
                    "route_mode": "generic_fallback",
                    "source": source,
                },
            }

        @staticmethod
        def accounts():
            return {"items": []}

    def setUp(self):
        self.server = TTPostHTTPServer(
            ("127.0.0.1", 0),
            self.Facade(),
            INTERNAL_TOKEN,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_public_route_needs_no_bearer_but_admin_still_does(self):
        with urllib.request.urlopen(
            self.base
            + "/api/public/tt-code/resolve?query=DRAMA100&source=Search"
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["found"])
        self.assertEqual("DRAMA100", payload["item"]["content_id"])
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.base + "/api/admin/tt-posts/accounts")
        self.assertEqual(403, denied.exception.code)
        denied.exception.close()


if __name__ == "__main__":
    unittest.main()
