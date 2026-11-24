import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.support_health import LABELS, read_snapshot


class SupportClockTests(unittest.TestCase):
    def test_uses_time_after_read_and_preserves_stale_and_future_limits(self):
        sample = dt.datetime(2026, 11, 24, 7, 48, 19, tzinfo=dt.timezone.utc)
        value = {'at_utc': sample.isoformat(), 'checks': [
            {'unit': unit, 'lane': 'fb', 'ok': True} for unit in LABELS
        ], 'fb_short_links': {'available': True, 'overdue_pending': 0}}
        admin = {'is_admin': True}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fb.json'
            path.write_text(json.dumps(value), encoding='utf-8')
            now = sample - dt.timedelta(seconds=3)
            original = Path.read_text

            def read_and_advance(file, **kwargs):
                nonlocal now
                contents = original(file, **kwargs)
                now = sample + dt.timedelta(seconds=1)
                return contents

            with patch.object(Path, 'read_text', read_and_advance):
                self.assertEqual(read_snapshot(lambda: now, admin, ['fb'], path)['code'], 'ok')
            for seconds, code in ((150, 'ok'), (151, 'snapshot_stale'), (-1, 'snapshot_stale')):
                self.assertEqual(read_snapshot(lambda: sample + dt.timedelta(seconds=seconds),
                                               admin, ['fb'], path)['code'], code)
            self.assertIsNone(read_snapshot(lambda: now, {}, ['fb'], path))
            self.assertIsNone(read_snapshot(lambda: now, admin, ['youtube'], path))
