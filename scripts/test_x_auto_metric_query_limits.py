"""Exercise large metric reads against the production SQLite bind limit."""

import contextlib
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from features.x_auto_posts.core import XAutoPostStore, XAutoPostStoreError


class LegacyLimitConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        # Also enforce the old limit on Python/SQLite builds without setlimit.
        if len(parameters) > 999:
            raise sqlite3.OperationalError("too many SQL variables")
        return super().execute(sql, parameters)


class MetricQueryLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metrics.sqlite3"
        self.store = XAutoPostStore(self.path)
        self.generation_index = 0

    @contextlib.contextmanager
    def limited_reader(self):
        with contextlib.closing(sqlite3.connect(
            str(self.path), isolation_level=None, factory=LegacyLimitConnection,
        )) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            if hasattr(conn, "setlimit"):
                conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            yield conn

    def generation(self, day, ids, spend="0.100000000000000001", product="Dramawave", platform=0):
        self.generation_index += 1
        generation = self.store.record_metric_generation(
            platform=platform, metric_date=day, product=product,
            rows=[{
                "content_id": value, "material_id": "m-" + value,
                "spend": spend, "af_revenue0": "0.033333333333333333",
            } for value in ids],
            refreshed_at_utc="2026-10-10T00:00:%02d+00:00" % self.generation_index,
        )
        self.store.activate_metric_generation(generation.id)

    def test_large_content_filter_keeps_active_rows_precision_order_and_scope(self):
        ids = ["drama-%04d" % i for i in range(1205)]
        days = ["2026-10-08", "2026-10-09"]
        self.generation(days[0], ids, "999")  # Superseded cache generation.
        for day in days:
            self.generation(day, ids)
        self.generation(days[0], ids[:1], "666", product="Other")
        self.generation(days[0], ids[:1], "777", platform=1)
        requested = list(reversed(ids)) + ids[:3] + ["absent"]
        with mock.patch.object(self.store, "_reader", self.limited_reader):
            rows = list(self.store.iter_ready_metric_rows(
                0, list(reversed(days)) + days[:1], requested, product="Dramawave",
            ))
            unfiltered = list(self.store.iter_ready_metric_rows(
                0, days, product="Dramawave",
            ))
            empty = list(self.store.iter_ready_metric_rows(
                0, days, [], product="Dramawave",
            ))
        self.assertEqual(rows, unfiltered)
        self.assertEqual(empty, [])
        self.assertEqual(len(rows), 2410)
        self.assertEqual(
            [(r["metric_date"], r["content_id"]) for r in rows],
            [(day, key) for day in days for key in ids],
        )
        self.assertEqual({r["spend"] for r in rows}, {"0.100000000000000001"})
        self.assertEqual({r["af_revenue0"] for r in rows}, {"0.033333333333333333"})

    def test_large_date_and_content_filters_share_the_bind_budget(self):
        days = [(date(2023, 1, 1) + timedelta(days=i)).isoformat() for i in range(1005)]
        ids = ["drama-%04d" % i for i in range(1105)]
        # Seed READY empty days efficiently; sparse facts cross both boundaries.
        with self.store._transaction() as conn:
            for index, day in enumerate(days):
                cur = conn.execute(
                    "INSERT INTO x_auto_metric_generation "
                    "(generation_key,platform,metric_date,product,status,created_at,updated_at) "
                    "VALUES(?,0,?,'Dramawave','ready','fixture','fixture')",
                    ("fixture-" + day, day),
                )
                gid = cur.lastrowid
                conn.execute(
                    "INSERT INTO x_auto_metric_active_pointer "
                    "(platform,metric_date,product,generation_id,activated_at_utc) "
                    "VALUES(0,?,'Dramawave',?,'fixture')", (day, gid),
                )
                if index in (0, 600, 1004):
                    conn.execute(
                        "INSERT INTO x_auto_metric_daily "
                        "(generation_id,content_id,material_id,spend,af_revenue0) "
                        "VALUES(?,?,?,'1','0.5')", (gid, ids[index], "m" + str(index)),
                    )
        with mock.patch.object(self.store, "_reader", self.limited_reader):
            rows = list(self.store.iter_ready_metric_rows(
                0, list(reversed(days)), list(reversed(ids)), product="Dramawave",
            ))
        self.assertEqual(
            [(r["metric_date"], r["content_id"]) for r in rows],
            [(days[i], ids[i]) for i in (0, 600, 1004)],
        )

    def test_large_filter_still_rejects_incomplete_windows(self):
        self.generation("2026-10-09", ["drama-1"])
        with mock.patch.object(self.store, "_reader", self.limited_reader):
            with self.assertRaises(XAutoPostStoreError) as caught:
                list(self.store.iter_ready_metric_rows(
                    0, ["2026-10-08", "2026-10-09"],
                    ["drama-%d" % i for i in range(2000)], product="Dramawave",
                ))
        self.assertEqual(caught.exception.code, "x_auto_metric_window_incomplete")


if __name__ == "__main__":
    unittest.main()
