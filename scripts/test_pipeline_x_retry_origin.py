"""Audited retry keys retain Auto ancestry before the queue binding is saved."""
import json
import unittest

from features.pipeline_dispatch.adapters import LedgerAdapter, LedgerSpec
from scripts import test_x_auto_operator_retry as fixtures


class XRetryOriginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OperatorRetryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.apply(self.fixture.plan())
        self.fixture.mutate(self.fixture.auto, "ALTER TABLE x_auto_task ADD COLUMN created_at TEXT DEFAULT '2026-11-03T12:59:01Z'")
        self.fixture.mutate(self.fixture.auto, "CREATE TABLE x_auto_template(id INTEGER PRIMARY KEY,created_by_user_id TEXT,created_by_tenant_key TEXT)")
        self.fixture.mutate(self.fixture.auto, "INSERT INTO x_auto_template VALUES(4,'owner','tenant')")
        self.fixture.mutate(self.fixture.auto, "UPDATE x_auto_task SET status='preparing',execution_run_id=1826,material_id='7818211' WHERE id=2712")
        self.fixture.mutate(self.fixture.native, "ALTER TABLE x_post_queue ADD COLUMN material_id TEXT")
        self.fixture.mutate(self.fixture.native, "ALTER TABLE x_post_queue ADD COLUMN created_at TEXT DEFAULT '2026-11-04T08:55:00Z'")
        self.fixture.mutate(self.fixture.native, "INSERT INTO x_post_manual_run(id,trigger_source,external_task_key,template_ref,template_version,status) VALUES(1826,'auto_template','x-auto-task-2712-retry-1','x-auto-template-4',6,'running')")
        self.fixture.mutate(self.fixture.native, "INSERT INTO x_post_queue(id,manual_run_id,account_id,status,material_id) VALUES(5908,1826,46,'queued','7818211')")
        self.adapter = LedgerAdapter(LedgerSpec('x', 'queue', str(self.fixture.native), 'x_post_queue'),
                                     cross_paths={'x_auto': str(self.fixture.auto)})

    def origin(self):
        return self.adapter.read(5908)

    def test_exact_audited_retry_restores_owner_and_ancestor_without_mutation(self):
        before = self.fixture.read(self.fixture.auto, 'SELECT * FROM x_auto_task')
        result = self.origin()
        self.assertEqual(result['ancestor_source_keys'], ['x:auto:2712'])
        self.assertEqual(result['owner_user_id'], 'owner')
        self.assertEqual(result['owner_tenant_key'], 'tenant')
        self.assertEqual(self.fixture.read(self.fixture.auto, 'SELECT * FROM x_auto_task'), before)
        self.assertNotIn('account_snapshot_json', result)
        self.assertNotIn('identity_sha256', result)

    def test_missing_audit_and_forged_generation_do_not_gain_auto_ancestry(self):
        self.fixture.mutate(self.fixture.native, "UPDATE x_post_manual_run SET external_task_key='x-auto-task-2712-retry-2' WHERE id=1826")
        self.assertFalse(self.origin().get('ancestor_source_keys'))
        self.fixture.mutate(self.fixture.native, "UPDATE x_post_manual_run SET external_task_key='x-auto-task-2712-retry-1' WHERE id=1826")
        self.fixture.mutate(self.fixture.auto, "DROP TABLE x_auto_operator_retry")
        self.assertFalse(self.origin().get('ancestor_source_keys'))

    def test_invalid_audit_and_wrong_parent_or_material_fail_closed(self):
        changes = (
            (self.fixture.auto, "UPDATE x_auto_operator_retry SET identity_sha256='bad'", "UPDATE x_auto_operator_retry SET identity_sha256={hash}"),
            (self.fixture.auto, "UPDATE x_auto_task SET execution_run_id=999 WHERE id=2712", "UPDATE x_auto_task SET execution_run_id=1826 WHERE id=2712"),
            (self.fixture.native, "UPDATE x_post_queue SET material_id='other' WHERE id=5908", "UPDATE x_post_queue SET material_id='7818211' WHERE id=5908"),
            (self.fixture.native, "UPDATE x_post_queue SET account_id=49 WHERE id=5908", "UPDATE x_post_queue SET account_id=46 WHERE id=5908"),
            (self.fixture.native, "UPDATE x_post_manual_run SET template_version=5 WHERE id=1826", "UPDATE x_post_manual_run SET template_version=6 WHERE id=1826"),
        )
        saved_hash = self.fixture.read(self.fixture.auto, 'SELECT identity_sha256 FROM x_auto_operator_retry')[0]['identity_sha256']
        for path, change, restore in changes:
            with self.subTest(change=change):
                self.fixture.mutate(path, change)
                self.assertFalse(self.origin().get('ancestor_source_keys'))
                self.fixture.mutate(path, restore.replace('{hash}', "'" + saved_hash + "'"))

    def test_legacy_key_still_works_without_retry_table(self):
        self.fixture.mutate(self.fixture.native, "UPDATE x_post_manual_run SET external_task_key='x-auto-task-2712' WHERE id=1826")
        self.fixture.mutate(self.fixture.auto, "DROP TABLE x_auto_operator_retry")
        self.assertEqual(self.origin()['ancestor_source_keys'], ['x:auto:2712'])


if __name__ == '__main__':
    unittest.main()
