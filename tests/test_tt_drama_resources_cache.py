import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from features.tt_drama_resources import (
    ResourceStorageError,
    SQLiteResourceCache,
    validate_resource_cache_path,
)


CONTENT_ID = "Ag0rfr5F0F"


class _Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


def item(title="Her Beast"):
    return {
        "landing_id": 2049,
        "content_id": CONTENT_ID,
        "resolved_content_id": CONTENT_ID,
        "title": title,
        "description": "Description",
        "cover_url": "https://cdn.usrgrow.com/cover.jpg",
        "content_hash": "a" * 64,
        "fetched_at": "2026-07-27T00:00:00+00:00",
    }


class SQLiteResourceCacheTests(unittest.TestCase):
    def make_cache(self, directory, clock=None):
        cache = SQLiteResourceCache(
            Path(directory) / "resources.sqlite3",
            clock=clock,
            allow_test_path=True,
        )
        cache.warmup()
        return cache

    def test_test_path_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resources.sqlite3"
            with self.assertRaises(ResourceStorageError):
                SQLiteResourceCache(path).warmup()
            self.assertEqual(
                validate_resource_cache_path(path, allow_test_path=True),
                path,
            )
        with self.assertRaisesRegex(ResourceStorageError, "absolute"):
            validate_resource_cache_path(
                "relative.sqlite3",
                allow_test_path=True,
            )

    def test_cache_gate_checks_candidate_parent_not_mount_root_write_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "resources.sqlite3"
            with mock.patch(
                "features.tt_drama_resources.cache.os.access",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    ResourceStorageError,
                    "directory is not writable",
                ):
                    validate_resource_cache_path(
                        path,
                        allow_test_path=True,
                    )

    def test_production_gate_requires_write_on_cache_parent_not_mount_root(self):
        with tempfile.TemporaryDirectory() as directory:
            mount_root = Path(directory) / "mnt-data-disk"
            cache_parent = mount_root / "tt-drama-resource-cache" / "state"
            cache_parent.mkdir(parents=True)
            db_path = cache_parent / "resources.sqlite3"
            real_stat = os.stat
            access_calls = []

            def separate_mount_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if os.path.normpath(str(path)) == os.path.normpath(
                    str(mount_root)
                ):
                    fields = list(result)
                    fields[2] = int(result.st_dev) + 1
                    return os.stat_result(fields)
                return result

            def parent_only_access(path, mode):
                access_calls.append((os.path.normpath(str(path)), mode))
                return os.path.normpath(str(path)) == os.path.normpath(
                    str(cache_parent)
                )

            with (
                mock.patch(
                    "features.tt_drama_resources.cache.DATA_DISK_ROOT",
                    mount_root,
                ),
                mock.patch(
                    "features.tt_drama_resources.cache.sys.platform",
                    "linux",
                ),
                mock.patch(
                    "features.tt_drama_resources.cache.os.stat",
                    side_effect=separate_mount_stat,
                ),
                mock.patch(
                    "features.tt_drama_resources.cache.os.access",
                    side_effect=parent_only_access,
                ),
                mock.patch(
                    "features.tt_drama_resources.cache.shutil.disk_usage",
                    return_value=shutil._ntuple_diskusage(
                        10 * 1024**3,
                        1 * 1024**3,
                        9 * 1024**3,
                    ),
                ),
            ):
                validated = validate_resource_cache_path(
                    db_path,
                    mount_info_provider=lambda _path: {
                        "mountpoint": str(mount_root),
                        "uuid": (
                            "3e8ac4e8-7770-456d-9e89-2ec5dd405fa8"
                        ),
                    },
                )
            self.assertEqual(validated, db_path)
            self.assertEqual(
                {path for path, _mode in access_calls},
                {os.path.normpath(str(cache_parent))},
            )

    def test_schema_has_no_raw_html_or_link_payload_and_uses_composite_key(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory)
            connection = sqlite3.connect(str(cache.db_path))
            try:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(tt_drama_resource_cache)"
                    ).fetchall()
                ]
                primary_key = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(tt_drama_resource_cache)"
                    ).fetchall()
                    if row[5]
                ]
            finally:
                connection.close()
            self.assertEqual(primary_key, ["landing_id", "content_id"])
            self.assertNotIn("raw_html", columns)
            self.assertNotIn("link", columns)
            self.assertNotIn("episode_1", columns)
            self.assertNotIn("source_url", columns)

    def test_positive_stale_and_expired_semantics(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory, clock)
            cache.put_ready(
                2049,
                CONTENT_ID,
                item(),
                positive_ttl_seconds=10,
                stale_ttl_seconds=100,
            )
            fresh = cache.peek(2049, CONTENT_ID)
            self.assertTrue(fresh.found)
            self.assertEqual(fresh.cache_state, "DISK_HIT")
            self.assertEqual(fresh.item["country"], "")
            self.assertEqual(fresh.item["episode_count"], 0)

            clock.value += 11
            self.assertEqual(
                cache.peek(2049, CONTENT_ID).cache_state,
                "STALE",
            )
            self.assertIsNone(
                cache.peek(2049, CONTENT_ID, allow_stale=False)
            )

            clock.value += 90
            self.assertIsNone(cache.peek(2049, CONTENT_ID))

    def test_negative_ttl_has_no_stale_fallback(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory, clock)
            cache.put_negative(
                2049,
                CONTENT_ID,
                negative_ttl_seconds=10,
                updated_at="2026-07-27T00:00:00+00:00",
            )
            negative = cache.peek(2049, CONTENT_ID)
            self.assertFalse(negative.found)
            self.assertEqual(negative.cache_state, "NEGATIVE_HIT")
            clock.value += 11
            self.assertIsNone(cache.peek(2049, CONTENT_ID))

    def test_warmup_prunes_expired_rows_and_leases_but_keeps_stale_ready(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory, clock)
            cache.put_ready(
                2049,
                CONTENT_ID,
                item(),
                positive_ttl_seconds=10,
                stale_ttl_seconds=20,
            )
            cache.put_negative(
                2049,
                "NEGATIVE01",
                negative_ttl_seconds=10,
                updated_at="2026-07-27T00:00:00+00:00",
            )
            cache.acquire_lease(
                2049,
                "LEASE00001",
                "worker",
                lease_seconds=10,
            )
            clock.value += 11
            cache.warmup()
            self.assertEqual(
                cache.peek(2049, CONTENT_ID).cache_state,
                "STALE",
            )
            self.assertIsNone(cache.peek(2049, "NEGATIVE01"))
            connection = sqlite3.connect(cache.db_path)
            try:
                lease_count = connection.execute(
                    "SELECT COUNT(*) FROM tt_drama_resource_lease"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(lease_count, 0)

            clock.value += 10
            cache.warmup()
            connection = sqlite3.connect(cache.db_path)
            try:
                ready_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM tt_drama_resource_cache
                     WHERE content_id = ?
                    """,
                    (CONTENT_ID,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(ready_count, 0)

    def test_landing_id_prevents_cross_landing_cache_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory)
            first = item("Landing 2049")
            second = item("Landing 2050")
            cache.put_ready(
                2049,
                CONTENT_ID,
                first,
                positive_ttl_seconds=10,
                stale_ttl_seconds=20,
            )
            cache.put_ready(
                2050,
                CONTENT_ID,
                second,
                positive_ttl_seconds=10,
                stale_ttl_seconds=20,
            )
            self.assertEqual(
                cache.peek(2049, CONTENT_ID).item["title"],
                "Landing 2049",
            )
            self.assertEqual(
                cache.peek(2050, CONTENT_ID).item["title"],
                "Landing 2050",
            )

    def test_cross_process_lease_is_owned_and_expirable(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory, clock)
            self.assertTrue(
                cache.acquire_lease(
                    2049,
                    CONTENT_ID,
                    "worker-a",
                    lease_seconds=10,
                )
            )
            self.assertFalse(
                cache.acquire_lease(
                    2049,
                    CONTENT_ID,
                    "worker-b",
                    lease_seconds=10,
                )
            )
            cache.release_lease(2049, CONTENT_ID, "worker-b")
            self.assertFalse(
                cache.acquire_lease(
                    2049,
                    CONTENT_ID,
                    "worker-b",
                    lease_seconds=10,
                )
            )
            clock.value += 11
            self.assertTrue(
                cache.acquire_lease(
                    2049,
                    CONTENT_ID,
                    "worker-b",
                    lease_seconds=10,
                )
            )

    def test_permission_normalization_requests_group_write_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory)
            os.chmod(cache.db_path, 0o640)
            with mock.patch(
                "features.tt_drama_resources.cache.os.chmod"
            ) as chmod:
                cache.normalize_permissions()
            chmod.assert_any_call(str(cache.db_path), 0o660)
            cache.normalize_permissions()
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(cache.db_path.stat().st_mode),
                    0o660,
                )

    @unittest.skipIf(
        os.name == "nt",
        "POSIX mode preservation is validated on Linux",
    )
    def test_permission_normalization_does_not_chmod_an_already_safe_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory)
            os.chmod(cache.db_path, 0o660)
            with mock.patch(
                "features.tt_drama_resources.cache.os.chmod"
            ) as chmod:
                cache.normalize_permissions()
            chmod.assert_not_called()

    def test_close_rejects_future_access(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory)
            cache.close()
            with self.assertRaisesRegex(ResourceStorageError, "closed"):
                cache.peek(2049, CONTENT_ID)

    def test_ready_cache_rechecks_storage_device_before_each_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = self.make_cache(directory)
            cache._verified_device += 1
            with self.assertRaisesRegex(
                ResourceStorageError,
                "storage device changed",
            ):
                cache.peek(2049, CONTENT_ID)


if __name__ == "__main__":
    unittest.main(verbosity=2)
