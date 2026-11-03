import copy
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from features.fb_auto_posts.core import FBAutoPostStore
from scripts import reconcile_fb_legacy_receipts as op


class ConfirmedLegacyReceiptTests(unittest.TestCase):
    def setUp(self):
        self.backup_guard = patch.object(op, 'validate_backup_location')
        self.backup_guard.start()
        self.addCleanup(self.backup_guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'native.sqlite3'
        self.store = FBAutoPostStore(self.path)
        self.now = datetime(2026, 11, 3, 11, tzinfo=timezone.utc)
        self.created = '2026-11-02T02:34:26+00:00'
        self.last_attempt = '2026-11-02T02:36:26+00:00'
        with self.store.connect() as c:
            c.execute("INSERT INTO fb_auto_run(id,template_id,template_version,slot_key,trigger_type,status,config_json,created_at_utc) VALUES(1,1,8,'fixture','auto','attention_required','{}',?)", (self.created,))
            c.execute('CREATE TABLE pipeline_fb_claim(source_key TEXT, generation INTEGER)')
            c.execute('CREATE TABLE pipeline_fb_operation(source_key TEXT, operation_id TEXT)')
            for tid, page in [(tid, p[0]) for tid, p in op.TARGETS.items()] + [(29147, '479753138554083')]:
                c.execute("""INSERT INTO fb_auto_task(id,run_id,template_id,template_version,page_id,group_id,status,
                    material_id,content_id,media_url,source_media_url,prepared_media_url,short_url,message_text,
                    attempt_count,unknown_outcome,error_code,error_message,created_at_utc,started_at_utc,
                    completed_at_utc,planned_publish_at_utc,prepared_sha256,prepared_size_bytes,
                    prepared_duration_seconds,prepared_profile,prepared_at_utc,gpu_job_id)
                    VALUES(?,1,1,8,?,'group','unknown',?,'content','https://example.invalid/final',
                    'https://example.invalid/source','https://example.invalid/final',?,?,2,1,
                    'fb_graph_response_outcome_unknown','original failure',?,?,?,?,'sha',12345,'1005.548',
                    'frozen-profile',?,'original-gpu-job')""",
                    (tid, page, str(tid), 'https://example.invalid/' + str(tid), 'frozen text ' + str(tid),
                     self.created, self.created, self.last_attempt, self.created, self.created))
                FBAutoPostStore._upsert_ledger(c, tid, page, str(tid), 'unknown', '', 3, True, 'original', self.last_attempt)
                c.execute("INSERT INTO fb_auto_publish_attempt(task_id,sequence,result_kind,error_code,created_at_utc) VALUES(?,1,'unknown','original',?)", (tid, self.last_attempt))
        self.pages = {'at': self.now.isoformat(), 'read_only': True, 'graph_methods': ['GET'], 'items': []}
        self.objects = copy.deepcopy(self.pages)
        with closing(op.connect(self.path)) as c:
            self.original_unknown = op.snapshot(c, 29147)
            for tid, (page_id, video_id) in op.TARGETS.items():
                t = op.snapshot(c, tid)['task']
                row = {'id': video_id, 'created_time': self.created, 'length': 1005.548,
                       'owner_page_id': page_id, 'exact_description': True, 'exact_frozen_short_url': True,
                       'description_sha256': op.text_sha(t['message_text']),
                       'frozen_short_url_sha256': op.text_sha(t['short_url']),
                       'status': {'video_status': 'ready', 'publishing_phase': {
                           'status': 'complete', 'publish_status': 'published', 'publish_time': self.created}}}
                self.pages['items'].append({'task_id': tid, 'page_id': page_id, 'matches': [copy.deepcopy(row)],
                                           'queries': [{'pages': [{'http_status': 200, 'complete': True}]}]})
                self.objects['items'].append({'task_id': tid, 'page_id': page_id, 'queries': [
                    {'kind': 'object', 'http_status': 200, 'rows': [row]}]})

    def plan(self):
        return op.make_plan(self.path, self.pages, self.objects, self.now)

    def apply(self, plan, suffix='1'):
        return op.apply_plan(plan, plan['plan_sha256'], Path(self.tmp.name) / ('backup-' + suffix + '.sqlite3'), self.now)

    def test_apply_uses_original_identity_and_actual_publish_time_without_network(self):
        plan = self.plan()
        with patch('requests.Session.request', side_effect=AssertionError('no HTTP operation allowed')):
            result = self.apply(plan)
        self.assertEqual(result['platform_writes'], 0)
        with closing(op.connect(self.path)) as c:
            for item in plan['items']:
                current = op.snapshot(c, item['task_id'])
                self.assertEqual(current['attempts'], item['before']['attempts'])
                self.assertEqual(current['ledger']['definite_attempts'], 3)
                self.assertEqual(current['task']['completed_at_utc'], self.created)
                self.assertEqual(current['task']['status'], 'published')
                self.assertEqual(current['task']['graph_post_id'], item['proof']['video_id'])
                changed = {'status', 'graph_post_id', 'unknown_outcome', 'error_code', 'error_message', 'completed_at_utc'}
                for key, value in item['before']['task'].items():
                    if key not in changed:
                        self.assertEqual(current['task'][key], value, key)
            self.assertEqual(op.snapshot(c, 29147), self.original_unknown)
            self.assertEqual(c.execute('SELECT status FROM fb_auto_run WHERE id=1').fetchone()[0], 'attention_required')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM fb_legacy_receipt_reconciliation').fetchone()[0], 2)
        with closing(sqlite3.connect(result['backup_path'])) as c:
            self.assertEqual(c.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM fb_auto_task WHERE status='unknown'").fetchone()[0], 3)

    def test_same_plan_replay_is_idempotent(self):
        plan = self.plan()
        self.apply(plan)
        result = self.apply(plan, '2')
        self.assertEqual([i['status'] for i in result['items']], ['already_committed'] * 2)

    def test_original_graph_compact_timezone_uses_python39_compatible_parser_input(self):
        for evidence in (self.pages, self.objects):
            for item in evidence['items']:
                rows = item.get('matches') or item['queries'][0]['rows']
                for row in rows:
                    row['created_time'] = '2026-11-02T02:34:26+0000'
                    row['status']['publishing_phase']['publish_time'] = '2026-11-02T02:34:26+0000'

        def python39_fromisoformat(text):
            # New Python accepts the original Graph format, masking regressions.
            self.assertNotRegex(text, r'[+-]\d{4}$')
            return datetime.fromisoformat(text)

        with patch.object(op, 'datetime') as legacy_datetime:
            legacy_datetime.fromisoformat.side_effect = python39_fromisoformat
            plan = self.plan()
            self.assertEqual(op.parsed('2026-11-02T10:34:26+0800'), datetime(2026, 11, 2, 2, 34, 26, tzinfo=timezone.utc))
            self.assertEqual(op.parsed('2026-11-01T21:34:26-0500'), datetime(2026, 11, 2, 2, 34, 26, tzinfo=timezone.utc))
        self.assertEqual([i['proof']['published_at'] for i in plan['items']], [self.created] * 2)
        self.apply(plan)
        with closing(op.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT completed_at_utc FROM fb_auto_task WHERE id=29162').fetchone()[0], self.created)

    def test_second_task_drift_rolls_back_both(self):
        plan = self.plan()
        with self.store.connect() as c:
            c.execute('UPDATE fb_auto_task SET attempt_count=3 WHERE id=29303')
        with self.assertRaisesRegex(ValueError, 'compare_and_swap'):
            self.apply(plan)
        with closing(op.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT status FROM fb_auto_task WHERE id=29162').fetchone()[0], 'unknown')

    def test_attempt_drift_is_rejected(self):
        plan = self.plan()
        with self.store.connect() as c:
            c.execute("UPDATE fb_auto_publish_attempt SET trace_id='new-attempt-evidence' WHERE task_id=29162")
        with self.assertRaisesRegex(ValueError, 'compare_and_swap'):
            self.apply(plan)

    def test_video_already_bound_to_another_task_rejected(self):
        plan = self.plan()
        with self.store.connect() as c:
            c.execute('UPDATE fb_auto_task SET graph_post_id=? WHERE id=29147', (op.TARGETS[29162][1],))
        with self.assertRaisesRegex(ValueError, 'already_bound'):
            self.apply(plan)

    def test_wrong_page_description_duration_shortlink_or_status_rejected(self):
        changes = [('owner_page_id', 'different'), ('description_sha256', 'wrong'), ('length', 1000),
                   ('frozen_short_url_sha256', 'wrong'), ('exact_frozen_short_url', False),
                   ('status', {'video_status': 'ready', 'publishing_phase': {'publish_status': 'processing'}})]
        for key, value in changes:
            with self.subTest(key=key):
                modified = copy.deepcopy(self.objects)
                modified['items'][0]['queries'][0]['rows'][0][key] = value
                with self.assertRaises(ValueError):
                    op.make_plan(self.path, self.pages, modified, self.now)

    def test_ambiguous_page_match_incomplete_scan_and_old_get_rejected(self):
        for mode in ('duplicate', 'incomplete', 'stale'):
            with self.subTest(mode=mode):
                p = copy.deepcopy(self.pages)
                if mode == 'duplicate': p['items'][0]['matches'] *= 2
                if mode == 'incomplete': p['items'][0]['queries'][0]['pages'][0]['complete'] = False
                if mode == 'stale': p['at'] = (self.now - timedelta(days=1)).isoformat()
                with self.assertRaises(ValueError):
                    op.make_plan(self.path, p, self.objects, self.now)

    def test_claim_operation_hold_and_lease_guards(self):
        statements = ["INSERT INTO pipeline_fb_claim VALUES('fb:auto:29162',1)",
                      "INSERT INTO pipeline_fb_operation VALUES('fb:auto:29162','operation')",
                      "INSERT INTO pipeline_canonical_hold VALUES('fb:auto:29162','old','hold')",
                      "UPDATE fb_auto_task SET lease_owner='active-worker' WHERE id=29162"]
        for statement in statements:
            with self.subTest(statement=statement):
                plan = self.plan()
                with self.store.connect() as c: c.execute(statement)
                with self.assertRaises(ValueError): self.apply(plan, str(statements.index(statement)))
                with self.store.connect() as c:
                    c.execute('DELETE FROM pipeline_fb_claim')
                    c.execute('DELETE FROM pipeline_fb_operation')
                    c.execute('DELETE FROM pipeline_canonical_hold')
                    c.execute("UPDATE fb_auto_task SET lease_owner='' WHERE id=29162")

    def test_tampered_plan_and_outside_allowlist_rejected(self):
        plan = self.plan()
        plan['items'][0]['proof']['video_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'plan_hash'):
            self.apply(plan)
        plan = self.plan()
        plan['items'][0]['task_id'] = 29147
        plan['plan_sha256'] = op.sha({k: v for k, v in plan.items() if k != 'plan_sha256'})
        with self.assertRaisesRegex(ValueError, 'scope'):
            self.apply(plan)

    def test_normal_run_refresh_error_rolls_back_receipt(self):
        plan = self.plan()
        with patch.object(FBAutoPostStore, '_refresh_run', side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError): self.apply(plan)
        with closing(op.connect(self.path)) as c:
            self.assertEqual(op.snapshot(c, 29162), plan['items'][0]['before'])


if __name__ == '__main__':
    unittest.main()
