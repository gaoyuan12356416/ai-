import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import x_tag_metric_refresh as metrics


class RefreshTests(unittest.TestCase):
    def test_missing_days_precede_older_repairs(self):
        days = ["11", "12", "13", "14"]
        self.assertEqual(metrics.schedule(days, {"11": 1, "12": 2}, 2), ["14", "13", "11"])
        self.assertEqual(metrics.schedule(["14"], {}, 5), ["14"])
        ready = dict.fromkeys(days, 1)
        self.assertEqual({metrics.schedule(days, ready, h)[1] for h in range(3)}, set(days[:-1]))

    def test_half_open_ranges_include_late_rows_and_empty_date(self):
        size = metrics.BUCKET
        self.assertEqual(list(metrics.ranges([])), [(0, None)])
        self.assertEqual(list(metrics.ranges([(5, size), (8, size), (12, 5)])),
                         [(0, 9 * size), (9 * size, None)])

    def test_complete_snapshot_merges_cross_slice_identities_exactly(self):
        day = (datetime.now(timezone(timedelta(hours=8))).date() - timedelta(days=1)).isoformat()
        boundaries = [b"1\t32768", b"2\t32768"]
        partials = [b"02\t42\t1\t7.00\t0.11", b"01\t61\t2\t0.10\t0.05",
                    b"01\t41\t2\t0.20\t0.07"]
        with patch.object(metrics, "mysql", side_effect=[boundaries, partials]) as query:
            self.assertEqual(metrics.fetch_day(day),
                             [["A", "2", "0.30", "0.12"], ["B", "1", "7.00", "0.11"]])
            self.assertEqual(query.call_count, 2)
            snapshot = query.call_args.args[0]
            self.assertIn("id >= 0 AND id < 98304", snapshot)
            self.assertIn("id >= 98304", snapshot)


if __name__ == "__main__":
    unittest.main()
