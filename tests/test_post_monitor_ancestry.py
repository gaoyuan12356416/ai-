"""Paged ancestry must retain successor suppression without parsing empty history."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from features.pipeline_dispatch.core import DispatchStore
from features.post_daily_report import accounting


class AncestryTests(unittest.TestCase):
    def test_successor_outside_page_and_cutoff_with_empty_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            now = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)
            store = DispatchStore(Path(folder) / 'dispatch.db', clock=lambda: now - dt.timedelta(days=1))
            for key in ('old', 'other', 'new', 'future', 'empty', 'invalid'):
                store.offer(dict(source_key='tt:auto:' + key, channel='tt', family='auto', stage='prepare'))
            with store.transaction() as conn:
                conn.execute("UPDATE pipeline_job SET source_meta_json=? WHERE source_key='tt:auto:new'",
                    (json.dumps({'ancestor_source_keys': ['tt:auto:old'], 'large': 'unused' * 100}),))
                conn.execute("UPDATE pipeline_job SET source_meta_json=?,created_at=? WHERE source_key='tt:auto:future'",
                    (json.dumps({'ancestor_source_keys': ['tt:auto:other']}), (now + dt.timedelta(hours=1)).isoformat()))
                conn.execute("UPDATE pipeline_job SET source_meta_json=? WHERE source_key='tt:auto:empty'",
                    ('{"ancestor_source_keys" : [ ], "large": "unused"}',))
                conn.execute("UPDATE pipeline_job SET source_meta_json=? WHERE source_key='tt:auto:invalid'",
                    ('{"ancestor_source_keys": malformed',))
            parsed = []
            original = accounting.object_value
            def record(raw, default=None):
                parsed.append(raw)
                return original(raw, default)
            with patch.object(accounting, 'object_value', side_effect=record):
                context = accounting.load_dispatch({'dispatch': store.path}, now,
                    source_keys={'tt:auto:old', 'tt:auto:other'})
            self.assertEqual(context['aliases'], {'tt:auto:old': 'tt:auto:new'})
            self.assertIsNone(accounting.effective_state(context, 'tt:auto:old', now))
            self.assertIsNotNone(accounting.effective_state(context, 'tt:auto:other', now))
            self.assertFalse(any(isinstance(raw, str) and 'unused' in raw for raw in parsed))


if __name__ == '__main__':
    unittest.main()
