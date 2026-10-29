import json
from datetime import timedelta
from unittest import main
from test_fb_group_pool import GroupPoolTests
from recover_fb_group_pool_run import recover
from features.fb_video_publish.core import DomainError


class RecoveryTests(GroupPoolTests):
    def skipped(self):
        original = self.gs.resolve_scope
        self.gs.resolve_scope = lambda *a, **kw: (_ for _ in ()).throw(DomainError("group_pool_source_unavailable", "failure", 503))
        # Configure/enable use scope too, so fail only the due tick.
        self.gs.resolve_scope = original
        self.mutate("/items", material_ids=["21", "22"])
        self.configure()
        self.mutate("/enable")
        self.now += timedelta(minutes=1)
        self.gs.resolve_scope = lambda *a, **kw: (_ for _ in ()).throw(DomainError("group_pool_source_unavailable", "failure", 503))
        self.scheduler.tick("test")
        self.gs.resolve_scope = original
        self.now += timedelta(minutes=2)
        with self.store.connect() as c:
            row = dict(c.execute("SELECT * FROM fb_group_pool_run").fetchone())
        return row

    def test_recovery_is_atomic_idempotent_and_preserves_failed_run(self):
        old = self.skipped()
        identity = self.pool.identity_resolver
        preview = recover(self.pool, identity, old["id"], old["schedule_revision"])
        self.assertEqual(2, preview["task_count"])
        self.assertEqual(0, self.count("fb_video_task"))
        result = recover(self.pool, identity, old["id"], old["schedule_revision"], apply=True)
        self.assertEqual(2, result["task_count"])
        again = recover(self.pool, identity, old["id"], old["schedule_revision"], apply=True)
        self.assertTrue(again["idempotent"])
        self.assertEqual(result["batch_id"], again["batch_id"])
        self.assertEqual(2, self.count("fb_video_task"))
        with self.store.connect() as c:
            self.assertEqual(old, dict(c.execute("SELECT * FROM fb_group_pool_run WHERE id=?", (old["id"],)).fetchone()))
        for task in self.tasks():
            self.assertEqual(old["id"], json.loads(task["snapshot_json"])["group_pool"]["recovery_of_run_id"])

    def test_recovery_refuses_other_day_revision_or_existing_write(self):
        old = self.skipped()
        with self.assertRaises(DomainError):
            recover(self.pool, self.pool.identity_resolver, old["id"], 999, apply=True)
        self.now += timedelta(days=1)
        with self.assertRaises(DomainError):
            recover(self.pool, self.pool.identity_resolver, old["id"], old["schedule_revision"], apply=True)
        self.now -= timedelta(days=1)
        with self.store.connect() as c:
            c.execute("UPDATE fb_group_pool_run SET batch_id='existing' WHERE id=?", (old["id"],))
        with self.assertRaises(DomainError):
            recover(self.pool, self.pool.identity_resolver, old["id"], old["schedule_revision"], apply=True)
        self.assertEqual(0, self.count("fb_video_task"))


if __name__ == "__main__":
    main()
